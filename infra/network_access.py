"""設定 AWS 端的來源 IP 白名單（Security Group / API Gateway resource policy）。

搭配應用層的 guardian.netguard 形成兩層防護。

用法
  # 只看要套用什麼，不動任何資源（預設）
  python infra/network_access.py --dry-run

  # 產生 Security Group 規則（需指定 SG）
  python infra/network_access.py --security-group sg-0123456789abcdef0 --apply

  # 產生 API Gateway resource policy（輸出 JSON，貼到 Console 或用 CLI 套用）
  python infra/network_access.py --api-gateway-policy

  # 收回不在白名單內的既有規則（先 dry-run 確認）
  python infra/network_access.py --security-group sg-xxx --prune --dry-run

設計說明
  1. 只開必要的埠，預設 443（HTTPS）與 8000（直連 uvicorn 示範用）。
     實務上建議只開 443，由 ALB 終結 TLS 後轉發到內網的 8000。
  2. 每個位址寫成 /32，不擴大到整個網段——這批是固定式商用 IP，
     寫成 /24 會把同一個 ISP 網段的其他用戶一起放進來。
  3. 動作是幂等的：已存在的相同規則不會重複新增（AWS 會回
     InvalidPermission.Duplicate，這裡會辨識並視為成功）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from botocore.exceptions import ClientError  # noqa: E402

from guardian import aws as gaws  # noqa: E402
from guardian import config  # noqa: E402

# 只放實際要對外服務的埠。SSH（22）刻意不列入：
# 遠端管理應走 SSM Session Manager，不需要對外開 22。
DEFAULT_PORTS = [443, 8000]


def hr(title: str) -> None:
    print("\n" + "=" * 74)
    print(title)
    print("=" * 74)


def allowlist() -> list[str]:
    """取得要開放的來源位址（排除本機 loopback，那不需要寫進 SG）。"""
    out = []
    for ip in config.ALLOWED_IPS:
        s = ip.strip()
        if not s or s.startswith("127.") or s == "::1":
            continue
        out.append(s if "/" in s else f"{s}/32")
    return out


def build_sg_permissions(sources: list[str], ports: list[int]) -> list[dict[str, Any]]:
    """組出 authorize_security_group_ingress 需要的 IpPermissions。"""
    perms = []
    for port in ports:
        perms.append({
            "IpProtocol": "tcp",
            "FromPort": port,
            "ToPort": port,
            "IpRanges": [
                {"CidrIp": s,
                 "Description": "小小守護員 稽查人員指定來源"}
                for s in sources if ":" not in s
            ],
            "Ipv6Ranges": [
                {"CidrIpv6": s,
                 "Description": "小小守護員 稽查人員指定來源"}
                for s in sources if ":" in s
            ],
        })
    # 去掉空的 range，避免 API 報錯
    for p in perms:
        if not p["IpRanges"]:
            p.pop("IpRanges")
        if not p["Ipv6Ranges"]:
            p.pop("Ipv6Ranges")
    return perms


def show_plan(sources: list[str], ports: list[int]) -> None:
    hr("將允許的來源與埠")
    print(f"  來源 {len(sources)} 個：")
    for s in sources:
        print(f"      {s}")
    print(f"  埠：{', '.join(str(p) for p in ports)}")
    print("\n  對應的 AWS CLI 指令（若不想用本腳本套用）：")
    for port in ports:
        for s in sources:
            flag = "--ip-permissions" if ":" in s else "--cidr"
            if ":" in s:
                print(f"    aws ec2 authorize-security-group-ingress "
                      f"--group-id <SG_ID> --ip-permissions "
                      f"'IpProtocol=tcp,FromPort={port},ToPort={port},"
                      f"Ipv6Ranges=[{{CidrIpv6={s}}}]'")
            else:
                print(f"    aws ec2 authorize-security-group-ingress "
                      f"--group-id <SG_ID> --protocol tcp --port {port} {flag} {s}")


def apply_sg(group_id: str, sources: list[str], ports: list[int],
             dry_run: bool) -> int:
    hr(f"Security Group {group_id}")
    ec2 = gaws.session().client("ec2")

    try:
        sg = ec2.describe_security_groups(GroupIds=[group_id])["SecurityGroups"][0]
    except ClientError as exc:
        print(f"  [失敗] 讀不到 Security Group：{exc.response['Error'].get('Code')}")
        print("         請確認 SG ID 與區域正確，且憑證有 ec2:DescribeSecurityGroups 權限")
        return 1

    print(f"  名稱 {sg.get('GroupName')}　VPC {sg.get('VpcId')}")
    print("  現有 inbound 規則：")
    existing: set[tuple[int, str]] = set()
    for perm in sg.get("IpPermissions", []):
        proto = perm.get("IpProtocol")
        fp, tp = perm.get("FromPort"), perm.get("ToPort")
        for r in perm.get("IpRanges", []):
            existing.add((fp or -1, r["CidrIp"]))
            print(f"      {proto} {fp}-{tp} ← {r['CidrIp']}"
                  f"  {r.get('Description', '')}")
        for r in perm.get("Ipv6Ranges", []):
            existing.add((fp or -1, r["CidrIpv6"]))
            print(f"      {proto} {fp}-{tp} ← {r['CidrIpv6']}")
    if not existing:
        print("      （無）")

    # 找出開放給任何人的規則，這是最該注意的風險
    wide_open = [(p, c) for (p, c) in existing if c in ("0.0.0.0/0", "::/0")]
    if wide_open:
        print("\n  !! 偵測到對全網開放的規則：")
        for port, cidr in wide_open:
            print(f"      埠 {port} ← {cidr}")
        print("     本服務沒有身分驗證，這等於完全公開。"
              "建議用 --prune 收回，或手動移除。")

    to_add = [(port, s) for port in ports for s in sources
              if (port, s) not in existing]
    print(f"\n  需新增 {len(to_add)} 條規則：")
    for port, s in to_add:
        print(f"      tcp {port} ← {s}")
    if not to_add:
        print("      （白名單規則都已存在，無需變動）")

    if dry_run:
        print("\n  [dry-run] 未實際變更任何資源")
        return 0
    if not to_add:
        return 0

    perms = build_sg_permissions([s for _, s in to_add],
                                 sorted({p for p, _ in to_add}))
    # 逐條套用，避免其中一條重複就整批失敗
    ok = 0
    for port in sorted({p for p, _ in to_add}):
        srcs = [s for p, s in to_add if p == port]
        try:
            ec2.authorize_security_group_ingress(
                GroupId=group_id,
                IpPermissions=build_sg_permissions(srcs, [port]))
            ok += len(srcs)
            print(f"  [成功] 埠 {port} 新增 {len(srcs)} 個來源")
        except ClientError as exc:
            code = exc.response["Error"].get("Code")
            if code == "InvalidPermission.Duplicate":
                print(f"  [略過] 埠 {port} 規則已存在")
                ok += len(srcs)
            else:
                print(f"  [失敗] 埠 {port}：{code} {exc.response['Error'].get('Message')}")
    print(f"\n  完成，共 {ok} 條規則就位")
    return 0


def prune_sg(group_id: str, sources: list[str], ports: list[int],
             dry_run: bool) -> int:
    """收回不在白名單內的 inbound 規則。"""
    hr(f"收回多餘規則 {group_id}")
    ec2 = gaws.session().client("ec2")
    try:
        sg = ec2.describe_security_groups(GroupIds=[group_id])["SecurityGroups"][0]
    except ClientError as exc:
        print(f"  [失敗] {exc.response['Error'].get('Code')}")
        return 1

    keep = {(p, s) for p in ports for s in sources}
    revoke: list[dict[str, Any]] = []
    for perm in sg.get("IpPermissions", []):
        fp = perm.get("FromPort")
        for r in perm.get("IpRanges", []):
            if (fp, r["CidrIp"]) not in keep:
                revoke.append({"IpProtocol": perm.get("IpProtocol"),
                               "FromPort": fp, "ToPort": perm.get("ToPort"),
                               "IpRanges": [{"CidrIp": r["CidrIp"]}]})
        for r in perm.get("Ipv6Ranges", []):
            if (fp, r["CidrIpv6"]) not in keep:
                revoke.append({"IpProtocol": perm.get("IpProtocol"),
                               "FromPort": fp, "ToPort": perm.get("ToPort"),
                               "Ipv6Ranges": [{"CidrIpv6": r["CidrIpv6"]}]})

    if not revoke:
        print("  沒有需要收回的規則")
        return 0
    print(f"  將收回 {len(revoke)} 條：")
    for p in revoke:
        src = (p.get("IpRanges") or p.get("Ipv6Ranges"))[0]
        print(f"      {p['IpProtocol']} {p['FromPort']} ← {list(src.values())[0]}")
    if dry_run:
        print("\n  [dry-run] 未實際變更。確認無誤後加 --apply")
        return 0
    try:
        ec2.revoke_security_group_ingress(GroupId=group_id, IpPermissions=revoke)
        print("  [成功] 已收回")
    except ClientError as exc:
        print(f"  [失敗] {exc.response['Error'].get('Code')}")
        return 1
    return 0


def api_gateway_policy(sources: list[str]) -> dict[str, Any]:
    """API Gateway 的 resource policy：只允許指定來源呼叫。

    這是給「Lambda + API Gateway」部署路線用的；
    EC2／ALB 路線用 Security Group 即可。
    """
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": "*",
                "Action": "execute-api:Invoke",
                "Resource": "execute-api:/*/*/*",
            },
            {
                "Effect": "Deny",
                "Principal": "*",
                "Action": "execute-api:Invoke",
                "Resource": "execute-api:/*/*/*",
                "Condition": {
                    "NotIpAddress": {"aws:SourceIp": sources}
                },
            },
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="設定 AWS 來源 IP 白名單（預設 dry-run，不動資源）")
    ap.add_argument("--security-group", help="要套用的 Security Group ID")
    ap.add_argument("--ports", nargs="*", type=int, default=DEFAULT_PORTS)
    ap.add_argument("--apply", action="store_true", help="實際套用變更")
    ap.add_argument("--dry-run", action="store_true", default=None)
    ap.add_argument("--prune", action="store_true",
                    help="收回不在白名單內的既有 inbound 規則")
    ap.add_argument("--api-gateway-policy", action="store_true",
                    help="輸出 API Gateway resource policy JSON")
    args = ap.parse_args()

    dry_run = not args.apply if args.dry_run is None else args.dry_run
    sources = allowlist()

    hr("身分")
    try:
        me = gaws.whoami()
        for k, v in me.items():
            print(f"  {k:8}: {v}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [失敗] 無法取得 AWS 身分：{exc}")
        print("  臨時憑證可能已過期，請更新 ~/.aws/credentials 後重試")
        return 1

    show_plan(sources, args.ports)

    if args.api_gateway_policy:
        hr("API Gateway resource policy")
        policy = api_gateway_policy(sources)
        text = json.dumps(policy, ensure_ascii=False, indent=2)
        print(text)
        out = Path("data") / "api_gateway_resource_policy.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"\n  已存到 {out}")

    rc = 0
    if args.security_group:
        if args.prune:
            rc = prune_sg(args.security_group, sources, args.ports, dry_run)
        else:
            rc = apply_sg(args.security_group, sources, args.ports, dry_run)
    elif not args.api_gateway_policy:
        hr("下一步")
        print("  未指定 --security-group，只顯示了計畫。")
        print("  EC2／ALB 路線： --security-group sg-xxxx --apply")
        print("  API Gateway 路線： --api-gateway-policy")

    hr("務必一併確認的事")
    print("  1. 本服務沒有身分驗證。IP 白名單只限制連線來源，"
          "無法辨識使用者、無法做權限分級與操作稽核。")
    print("  2. 這 4 個位址若由 ISP 動態配發，位址變動後就會連不上；"
          "固定式商用線路才適合長期綁定。")
    print("  3. 同一出口 IP 後面的所有人（同辦公室／同 NAT）都會被視為合法來源。")
    print("  4. 建議加上 HTTPS（ACM + ALB 或 CloudFront），"
          "否則財務與投訴內容以明文在網路上傳輸。")
    print("  5. 應用層白名單需一併啟用：python cli.py serve --host 0.0.0.0")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
