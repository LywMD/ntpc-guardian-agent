"""盤點 AWS 帳號裡實際存在的資料資源（唯讀）。

只做 list / describe / head，不下載大檔、不修改任何資源。
Secrets Manager 與 SSM SecureString 僅列出名稱，絕不讀取值。

用法
  python scripts/discover_aws_data.py
  python scripts/discover_aws_data.py --regions us-west-2 us-east-1 ap-northeast-1
  python scripts/discover_aws_data.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from botocore.config import Config  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

from guardian import aws as gaws  # noqa: E402

CFG = Config(retries={"max_attempts": 3, "mode": "standard"},
             connect_timeout=8, read_timeout=25)

# 看起來像我們要的資料的關鍵字（檔名／表名命中就標記）
HINTS = ["幼", "kinder", "preschool", "ece", "園", "托", "childcare", "nursery",
         "財報", "決算", "財務", "financial", "settle", "budget", "決", "收費", "fee",
         "非營利", "nonprofit", "non-profit", "公校", "公立", "public", "school",
         "評鑑", "rating", "裁罰", "penalty", "新北", "ntpc", "taipei"]

FINDINGS: dict[str, Any] = {}
ERRORS: list[str] = []


def hint(text: str) -> bool:
    low = (text or "").lower()
    return any(h.lower() in low for h in HINTS)


def hr(title: str) -> None:
    print("\n" + "=" * 74)
    print(title)
    print("=" * 74)


def warn(service: str, exc: Exception) -> None:
    code = ""
    if isinstance(exc, ClientError):
        code = exc.response["Error"].get("Code", "")
    msg = f"{service}: {code or type(exc).__name__}"
    ERRORS.append(msg)
    note = "（權限不足或服務未啟用）" if code in (
        "AccessDenied", "AccessDeniedException", "UnauthorizedOperation",
        "AuthorizationError", "InvalidClientTokenId") else ""
    print(f"  [略過] {msg} {note}")


def client(service: str, region: str | None = None):
    return gaws.session().client(service, region_name=region, config=CFG)


# ------------------------------------------------------------------ S3
def scan_s3(max_keys_per_bucket: int) -> None:
    hr("S3（最可能放財報 PDF / CSV 的地方）")
    try:
        s3 = client("s3")
        buckets = s3.list_buckets().get("Buckets", [])
    except Exception as exc:  # noqa: BLE001
        warn("s3:ListBuckets", exc)
        return

    if not buckets:
        print("  帳號內沒有任何 S3 bucket")
        FINDINGS["s3"] = []
        return

    print(f"  共 {len(buckets)} 個 bucket")
    out = []
    for b in buckets:
        name = b["Name"]
        entry: dict[str, Any] = {"bucket": name,
                                 "created": str(b.get("CreationDate", "")),
                                 "objects": [], "total_objects": 0,
                                 "total_bytes": 0, "prefixes": [], "filetypes": {}}
        try:
            loc = s3.get_bucket_location(Bucket=name).get("LocationConstraint") or "us-east-1"
            entry["region"] = loc
        except Exception:  # noqa: BLE001
            entry["region"] = "?"

        try:
            bs3 = client("s3", entry["region"]) if entry["region"] != "?" else s3
            paginator = bs3.get_paginator("list_objects_v2")
            types: dict[str, int] = defaultdict(int)
            prefixes: dict[str, int] = defaultdict(int)
            for page in paginator.paginate(Bucket=name,
                                           PaginationConfig={"MaxItems": max_keys_per_bucket}):
                for o in page.get("Contents", []):
                    entry["total_objects"] += 1
                    entry["total_bytes"] += o.get("Size", 0)
                    key = o["Key"]
                    ext = key.rsplit(".", 1)[-1].lower() if "." in key.rsplit("/", 1)[-1] else "(無)"
                    types[ext] += 1
                    top = key.split("/")[0] if "/" in key else "(根目錄)"
                    prefixes[top] += 1
                    if len(entry["objects"]) < 400:
                        entry["objects"].append({
                            "key": key, "size": o.get("Size", 0),
                            "modified": str(o.get("LastModified", ""))[:19],
                            "interesting": hint(key),
                        })
            entry["filetypes"] = dict(sorted(types.items(), key=lambda kv: -kv[1]))
            entry["prefixes"] = dict(sorted(prefixes.items(), key=lambda kv: -kv[1]))
        except Exception as exc:  # noqa: BLE001
            warn(f"s3:ListObjects({name})", exc)

        marked = [o for o in entry["objects"] if o["interesting"]]
        entry["interesting_count"] = len(marked)
        out.append(entry)

        size_mb = entry["total_bytes"] / 1024 / 1024
        flag = "  ★ 命中關鍵字" if marked else ""
        print(f"\n  [{entry['region']}] {name}　{entry['total_objects']} 物件"
              f"／{size_mb:,.1f} MB{flag}")
        if entry["prefixes"]:
            print("     頂層目錄：" + "、".join(
                f"{k}({v})" for k, v in list(entry["prefixes"].items())[:8]))
        if entry["filetypes"]:
            print("     副檔名：  " + "、".join(
                f"{k}({v})" for k, v in list(entry["filetypes"].items())[:8]))
        for o in (marked or entry["objects"])[:12]:
            mark = "★" if o["interesting"] else " "
            print(f"     {mark} {o['key']}　{o['size']:,} B　{o['modified']}")
        shown = len(marked or entry["objects"])
        if entry["total_objects"] > shown:
            print(f"     …另有 {entry['total_objects'] - shown} 個物件")

    FINDINGS["s3"] = out


# ------------------------------------------------------------------ DynamoDB
def scan_dynamodb(regions: list[str], sample: int) -> None:
    hr("DynamoDB")
    out = []
    for region in regions:
        try:
            ddb = client("dynamodb", region)
            tables = ddb.list_tables().get("TableNames", [])
        except Exception as exc:  # noqa: BLE001
            warn(f"dynamodb:ListTables({region})", exc)
            continue
        if not tables:
            print(f"  [{region}] 無資料表")
            continue
        print(f"  [{region}] {len(tables)} 個資料表")
        for t in tables:
            entry: dict[str, Any] = {"region": region, "table": t}
            try:
                d = ddb.describe_table(TableName=t)["Table"]
                entry["item_count"] = d.get("ItemCount")
                entry["size_bytes"] = d.get("TableSizeBytes")
                entry["keys"] = [k["AttributeName"] for k in d.get("KeySchema", [])]
                entry["attributes"] = [a["AttributeName"]
                                       for a in d.get("AttributeDefinitions", [])]
            except Exception as exc:  # noqa: BLE001
                warn(f"dynamodb:DescribeTable({t})", exc)
            if sample > 0:
                try:
                    items = ddb.scan(TableName=t, Limit=sample).get("Items", [])
                    entry["sample_attributes"] = sorted(
                        {k for it in items for k in it})
                    entry["sample_count"] = len(items)
                except Exception as exc:  # noqa: BLE001
                    warn(f"dynamodb:Scan({t})", exc)
            flag = "  ★" if hint(t) else ""
            print(f"     {t}　{entry.get('item_count')} 筆／"
                  f"{(entry.get('size_bytes') or 0)/1024:,.0f} KB{flag}")
            if entry.get("sample_attributes"):
                print("        欄位：" + "、".join(entry["sample_attributes"][:14]))
            out.append(entry)
    FINDINGS["dynamodb"] = out


# ------------------------------------------------------------------ Glue / Athena
def scan_glue(regions: list[str]) -> None:
    hr("Glue Data Catalog / Athena（若資料已建表，這裡最好用）")
    out = []
    for region in regions:
        try:
            glue = client("glue", region)
            dbs = glue.get_databases().get("DatabaseList", [])
        except Exception as exc:  # noqa: BLE001
            warn(f"glue:GetDatabases({region})", exc)
            continue
        if not dbs:
            print(f"  [{region}] 無 Glue database")
            continue
        for db in dbs:
            dbname = db["Name"]
            print(f"  [{region}] database: {dbname}")
            try:
                tables = glue.get_tables(DatabaseName=dbname).get("TableList", [])
            except Exception as exc:  # noqa: BLE001
                warn(f"glue:GetTables({dbname})", exc)
                continue
            for t in tables:
                cols = [c["Name"] for c in
                        t.get("StorageDescriptor", {}).get("Columns", [])]
                loc = t.get("StorageDescriptor", {}).get("Location", "")
                entry = {"region": region, "database": dbname, "table": t["Name"],
                         "columns": cols, "location": loc,
                         "rows": t.get("Parameters", {}).get("recordCount")}
                out.append(entry)
                flag = "  ★" if hint(t["Name"]) or hint(loc) else ""
                print(f"     表 {t['Name']}　{len(cols)} 欄{flag}")
                if cols:
                    print("        欄位：" + "、".join(cols[:16]))
                if loc:
                    print(f"        位置：{loc}")
    FINDINGS["glue"] = out

    for region in regions:
        try:
            ath = client("athena", region)
            wgs = [w["Name"] for w in ath.list_work_groups().get("WorkGroupList", [])]
            if wgs:
                print(f"  [{region}] Athena workgroups：{', '.join(wgs)}")
        except Exception as exc:  # noqa: BLE001
            warn(f"athena:ListWorkGroups({region})", exc)


# ------------------------------------------------------------------ RDS / Redshift
def scan_databases(regions: list[str]) -> None:
    hr("RDS / Aurora / Redshift")
    out = []
    for region in regions:
        try:
            rds = client("rds", region)
            insts = rds.describe_db_instances().get("DBInstances", [])
            clusters = rds.describe_db_clusters().get("DBClusters", [])
        except Exception as exc:  # noqa: BLE001
            warn(f"rds:Describe({region})", exc)
            insts, clusters = [], []
        for i in insts:
            e = {"region": region, "kind": "rds-instance",
                 "id": i["DBInstanceIdentifier"], "engine": i.get("Engine"),
                 "status": i.get("DBInstanceStatus"),
                 "endpoint": (i.get("Endpoint") or {}).get("Address"),
                 "db_name": i.get("DBName"),
                 "publicly_accessible": i.get("PubliclyAccessible")}
            out.append(e)
            print(f"  [{region}] RDS {e['id']}　{e['engine']}　{e['status']}"
                  f"　DB={e['db_name']}")
        for c in clusters:
            e = {"region": region, "kind": "aurora-cluster",
                 "id": c["DBClusterIdentifier"], "engine": c.get("Engine"),
                 "status": c.get("Status"), "endpoint": c.get("Endpoint"),
                 "db_name": c.get("DatabaseName")}
            out.append(e)
            print(f"  [{region}] Aurora {e['id']}　{e['engine']}　{e['status']}")
        try:
            rs = client("redshift", region)
            for c in rs.describe_clusters().get("Clusters", []):
                e = {"region": region, "kind": "redshift",
                     "id": c["ClusterIdentifier"], "status": c.get("ClusterStatus"),
                     "db_name": c.get("DBName")}
                out.append(e)
                print(f"  [{region}] Redshift {e['id']}　{e['status']}")
        except Exception as exc:  # noqa: BLE001
            warn(f"redshift:DescribeClusters({region})", exc)
    if not out:
        print("  查無 RDS / Aurora / Redshift 資源")
    FINDINGS["databases"] = out


# ------------------------------------------------------------------ Bedrock KB
def scan_bedrock_kb(regions: list[str]) -> None:
    hr("Bedrock Knowledge Bases（若資料已做過向量化）")
    out = []
    for region in regions:
        try:
            ba = client("bedrock-agent", region)
            kbs = ba.list_knowledge_bases(maxResults=50).get("knowledgeBaseSummaries", [])
        except Exception as exc:  # noqa: BLE001
            warn(f"bedrock-agent:ListKnowledgeBases({region})", exc)
            continue
        for kb in kbs:
            kbid = kb["knowledgeBaseId"]
            entry = {"region": region, "id": kbid, "name": kb.get("name"),
                     "status": kb.get("status"), "data_sources": []}
            try:
                for ds in ba.list_data_sources(knowledgeBaseId=kbid).get(
                        "dataSourceSummaries", []):
                    d = ba.get_data_source(knowledgeBaseId=kbid,
                                           dataSourceId=ds["dataSourceId"])["dataSource"]
                    entry["data_sources"].append({
                        "name": d.get("name"),
                        "config": d.get("dataSourceConfiguration", {}),
                    })
            except Exception as exc:  # noqa: BLE001
                warn(f"bedrock-agent:ListDataSources({kbid})", exc)
            out.append(entry)
            print(f"  [{region}] KB {entry['name']}（{kbid}）{entry['status']}")
            for ds in entry["data_sources"]:
                print(f"     資料來源 {ds['name']}：{json.dumps(ds['config'], ensure_ascii=False)[:200]}")
        # 順便看有沒有既有的 Bedrock Agent
        try:
            agents = ba.list_agents(maxResults=50).get("agentSummaries", [])
            for a in agents:
                print(f"  [{region}] Bedrock Agent {a['agentName']}（{a['agentId']}）"
                      f"{a.get('agentStatus')}")
        except Exception:  # noqa: BLE001
            pass
    if not out:
        print("  查無 Knowledge Base")
    FINDINGS["bedrock_kb"] = out


# ------------------------------------------------------------------ 其他線索
def scan_misc(regions: list[str]) -> None:
    hr("其他線索（Lambda / CloudFormation / Secrets 名稱 / SSM 名稱）")
    misc: dict[str, Any] = {"lambda": [], "cloudformation": [],
                            "secrets": [], "ssm": []}
    for region in regions:
        try:
            lam = client("lambda", region)
            fns = lam.list_functions(MaxItems=50).get("Functions", [])
            for f in fns:
                misc["lambda"].append({"region": region, "name": f["FunctionName"],
                                       "runtime": f.get("Runtime")})
                print(f"  [{region}] Lambda {f['FunctionName']}（{f.get('Runtime')}）")
        except Exception as exc:  # noqa: BLE001
            warn(f"lambda:ListFunctions({region})", exc)
        try:
            cf = client("cloudformation", region)
            for s in cf.list_stacks(StackStatusFilter=[
                    "CREATE_COMPLETE", "UPDATE_COMPLETE", "IMPORT_COMPLETE"]
                    ).get("StackSummaries", [])[:30]:
                misc["cloudformation"].append({"region": region,
                                               "name": s["StackName"]})
                print(f"  [{region}] CFN stack {s['StackName']}")
        except Exception as exc:  # noqa: BLE001
            warn(f"cloudformation:ListStacks({region})", exc)
        try:
            sm = client("secretsmanager", region)
            for s in sm.list_secrets(MaxResults=50).get("SecretList", []):
                misc["secrets"].append({"region": region, "name": s["Name"]})
                print(f"  [{region}] Secret（僅名稱）{s['Name']}")
        except Exception as exc:  # noqa: BLE001
            warn(f"secretsmanager:ListSecrets({region})", exc)
        try:
            ssm = client("ssm", region)
            for p in ssm.describe_parameters(MaxResults=50).get("Parameters", []):
                misc["ssm"].append({"region": region, "name": p["Name"],
                                    "type": p.get("Type")})
                print(f"  [{region}] SSM 參數（僅名稱）{p['Name']}")
        except Exception as exc:  # noqa: BLE001
            warn(f"ssm:DescribeParameters({region})", exc)
    FINDINGS["misc"] = misc


# ------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser(description="盤點 AWS 帳號內的資料資源（唯讀）")
    ap.add_argument("--regions", nargs="*",
                    default=["us-west-2", "us-east-1", "ap-northeast-1", "ap-southeast-1"])
    ap.add_argument("--max-keys", type=int, default=5000,
                    help="每個 bucket 最多列多少物件")
    ap.add_argument("--ddb-sample", type=int, default=3,
                    help="每個 DynamoDB 表取幾筆看欄位（0 = 不取）")
    ap.add_argument("--json", help="把完整結果寫成 JSON")
    args = ap.parse_args()

    hr("身分與可用區域")
    try:
        me = gaws.whoami()
        for k, v in me.items():
            print(f"  {k:8}: {v}")
        FINDINGS["identity"] = me
    except Exception as exc:  # noqa: BLE001
        print(f"  [失敗] 無法取得身分：{exc}")
        print("  請確認 ~/.aws/credentials 有效（臨時憑證會過期）")
        return 1
    print(f"  掃描區域: {', '.join(args.regions)}")

    scan_s3(args.max_keys)
    scan_glue(args.regions)
    scan_dynamodb(args.regions, args.ddb_sample)
    scan_databases(args.regions)
    scan_bedrock_kb(args.regions)
    scan_misc(args.regions)

    hr("盤點結論")
    n_buckets = len(FINDINGS.get("s3", []))
    n_objs = sum(b["total_objects"] for b in FINDINGS.get("s3", []))
    n_interesting = sum(b.get("interesting_count", 0) for b in FINDINGS.get("s3", []))
    print(f"  S3               : {n_buckets} bucket／{n_objs} 物件"
          f"（{n_interesting} 個檔名命中幼兒園／財報關鍵字）")
    print(f"  Glue 表          : {len(FINDINGS.get('glue', []))}")
    print(f"  DynamoDB 表      : {len(FINDINGS.get('dynamodb', []))}")
    print(f"  RDS/Redshift     : {len(FINDINGS.get('databases', []))}")
    print(f"  Bedrock KB       : {len(FINDINGS.get('bedrock_kb', []))}")
    if ERRORS:
        print(f"\n  略過 {len(ERRORS)} 項（權限不足或服務未啟用）：")
        for e in sorted(set(ERRORS))[:20]:
            print(f"    - {e}")

    if n_objs == 0 and not FINDINGS.get("glue") and not FINDINGS.get("dynamodb") \
            and not FINDINGS.get("databases"):
        print("\n  ⚠ 這個帳號目前找不到任何可讀的資料資源。")
        print("    可能原因：資料在別的帳號／別的區域，或 WSParticipantRole 沒有讀取權限。")
        print("    請確認：(1) 資料放在哪個服務與區域　(2) 目前這組憑證是不是同一個帳號")

    if args.json:
        Path(args.json).write_text(
            json.dumps(FINDINGS, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        print(f"\n  完整結果已寫入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
