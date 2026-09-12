"""確認 AWS 上目前有沒有「可以讓外部 IP 連進來」的部署。

要能讓那 4 個 IP 開啟程式，必須同時具備：
  1. 一個對外可達的運算資源（EC2 / ECS / App Runner / Lambda+API Gateway）
  2. 上面實際跑著這個應用
  3. Security Group / resource policy 放行那 4 個位址
  4. 一個公開位址（Elastic IP / ALB DNS / API Gateway endpoint）

這支腳本只做唯讀查詢，回報「還缺哪幾項」。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from botocore.exceptions import ClientError  # noqa: E402

from guardian import aws as gaws  # noqa: E402

OUT = ROOT / "data" / "check_deployment.txt"
buf: list[str] = []

REGIONS = ["us-west-2", "us-east-1"]


def w(s: str = "") -> None:
    buf.append(s)


def main() -> None:
    try:
        me = gaws.whoami()
        w(f"帳號 {me.get('account')}　角色 {me.get('arn', '').split('/')[-2:]}")
    except Exception as exc:  # noqa: BLE001
        w(f"[失敗] 無法取得 AWS 身分：{exc}")
        OUT.write_text("\n".join(buf), encoding="utf-8")
        return

    found_compute = 0
    found_public = 0

    for region in REGIONS:
        w("")
        w("=" * 72)
        w(f"區域 {region}")
        w("=" * 72)

        # EC2 執行中的機器
        try:
            ec2 = gaws.session().client("ec2", region_name=region)
            res = ec2.describe_instances(Filters=[
                {"Name": "instance-state-name", "Values": ["running", "stopped"]}])
            insts = [i for r in res.get("Reservations", []) for i in r.get("Instances", [])]
            if insts:
                w(f"  EC2 執行個體 {len(insts)} 台：")
                for i in insts:
                    pub = i.get("PublicIpAddress")
                    w(f"      {i['InstanceId']}　{i['State']['Name']}"
                      f"　公開IP {pub or '(無)'}"
                      f"　SG {[g['GroupId'] for g in i.get('SecurityGroups', [])]}")
                    found_compute += 1
                    if pub:
                        found_public += 1
            else:
                w("  EC2 執行個體：無")
        except ClientError as exc:
            w(f"  EC2 查詢略過：{exc.response['Error'].get('Code')}")
        except Exception as exc:  # noqa: BLE001
            w(f"  EC2 查詢略過：{type(exc).__name__}")

        # 負載平衡器
        try:
            elb = gaws.session().client("elbv2", region_name=region)
            lbs = elb.describe_load_balancers().get("LoadBalancers", [])
            if lbs:
                w(f"  ALB/NLB {len(lbs)} 個：")
                for lb in lbs:
                    w(f"      {lb['LoadBalancerName']}　{lb.get('Scheme')}"
                      f"　DNS {lb.get('DNSName')}")
                    found_public += 1
            else:
                w("  ALB/NLB：無")
        except ClientError as exc:
            w(f"  ELB 查詢略過：{exc.response['Error'].get('Code')}")
        except Exception as exc:  # noqa: BLE001
            w(f"  ELB 查詢略過：{type(exc).__name__}")

        # API Gateway
        try:
            agw = gaws.session().client("apigatewayv2", region_name=region)
            apis = agw.get_apis().get("Items", [])
            if apis:
                w(f"  API Gateway (HTTP API) {len(apis)} 個：")
                for a in apis:
                    w(f"      {a.get('Name')}　{a.get('ApiEndpoint')}")
                    found_public += 1
            else:
                w("  API Gateway (HTTP API)：無")
        except ClientError as exc:
            w(f"  API Gateway 查詢略過：{exc.response['Error'].get('Code')}")
        except Exception as exc:  # noqa: BLE001
            w(f"  API Gateway 查詢略過：{type(exc).__name__}")

        # App Runner
        try:
            ar = gaws.session().client("apprunner", region_name=region)
            svcs = ar.list_services().get("ServiceSummaryList", [])
            if svcs:
                w(f"  App Runner {len(svcs)} 個：")
                for s in svcs:
                    w(f"      {s.get('ServiceName')}　{s.get('ServiceUrl')}")
                    found_public += 1
            else:
                w("  App Runner：無")
        except ClientError as exc:
            w(f"  App Runner 查詢略過：{exc.response['Error'].get('Code')}")
        except Exception as exc:  # noqa: BLE001
            w(f"  App Runner 查詢略過：{type(exc).__name__}")

    w("")
    w("=" * 72)
    w("結論")
    w("=" * 72)
    w(f"  可對外服務的運算資源：{found_compute} 台 EC2")
    w(f"  對外可達的公開端點：{found_public} 個")
    if found_public == 0:
        w("")
        w("  → 目前 AWS 上沒有任何對外端點，所以那 4 個 IP 現在【還無法】連線。")
        w("     程式目前只在本機執行，綁 127.0.0.1，外部網路連不到。")
        w("")
        w("  要讓那 4 個 IP 能開啟，還缺這幾步：")
        w("     1. 開一台 EC2（或 App Runner / ECS）並部署本專案")
        w("     2. 在該機器上以 python cli.py serve --host 0.0.0.0 啟動")
        w("     3. 用 infra/network_access.py --security-group <SG> --apply 放行 4 個 IP")
        w("     4. 取得公開位址（Elastic IP 或 ALB DNS）給使用者")
        w("     5. 建議加 HTTPS：ACM 憑證 + ALB，只對外開 443")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        import traceback
        buf.append(traceback.format_exc())
    OUT.write_text("\n".join(buf), encoding="utf-8")
