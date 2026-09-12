"""測試目前的 AWS 憑證有沒有權限建立對外服務所需的資源。

全部用 DryRun / 唯讀方式測試，不會真的建立任何東西。
WSParticipantRole 是工作坊角色，通常對建立資源有限制，
先確認可行性再決定部署路線，避免做到一半才發現權限不足。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from botocore.exceptions import ClientError  # noqa: E402

from guardian import aws as gaws  # noqa: E402

OUT = ROOT / "data" / "check_deploy_permission.txt"
buf: list[str] = []


def w(s: str = "") -> None:
    buf.append(s)


def probe(label: str, fn) -> bool:
    """執行 DryRun 呼叫。DryRunOperation = 有權限；其餘視為無權限。"""
    try:
        fn()
        w(f"  [可以] {label}（呼叫成功）")
        return True
    except ClientError as exc:
        code = exc.response["Error"].get("Code", "")
        msg = exc.response["Error"].get("Message", "")[:120]
        if code == "DryRunOperation":
            w(f"  [可以] {label}")
            return True
        if code in ("UnauthorizedOperation", "AccessDenied", "AccessDeniedException"):
            w(f"  [不行] {label} — 權限不足")
            return False
        w(f"  [不明] {label} — {code}: {msg}")
        return False
    except Exception as exc:  # noqa: BLE001
        w(f"  [不明] {label} — {type(exc).__name__}")
        return False


def main() -> None:
    region = "us-west-2"
    try:
        me = gaws.whoami()
        w(f"帳號 {me.get('account')}")
        w(f"角色 {me.get('arn')}")
        w(f"區域 {region}")
    except Exception as exc:  # noqa: BLE001
        w(f"[失敗] 無法取得身分：{exc}")
        OUT.write_text("\n".join(buf), encoding="utf-8")
        return

    ec2 = gaws.session().client("ec2", region_name=region)

    w("")
    w("=" * 72)
    w("建立對外服務所需的權限")
    w("=" * 72)

    # 取一個 subnet 與 AMI 供 DryRun 使用
    subnet_id = None
    vpc_id = None
    try:
        subs = ec2.describe_subnets(MaxResults=5).get("Subnets", [])
        if subs:
            subnet_id = subs[0]["SubnetId"]
            vpc_id = subs[0]["VpcId"]
            w(f"  找到 subnet {subnet_id}（VPC {vpc_id}）")
        else:
            w("  找不到任何 subnet")
    except ClientError as exc:
        w(f"  無法列出 subnet：{exc.response['Error'].get('Code')}")

    # AMI：優先用 SSM 公開參數（最可靠），失敗才退回 describe_images
    ami = None
    try:
        ssm = gaws.session().client("ssm", region_name=region)
        ami = ssm.get_parameter(
            Name="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
        )["Parameter"]["Value"]
        w(f"  找到 AMI（SSM 公開參數）{ami}")
    except Exception as exc:  # noqa: BLE001
        w(f"  SSM 取 AMI 失敗：{type(exc).__name__}")
    if not ami:
        try:
            imgs = ec2.describe_images(
                Owners=["amazon"],
                Filters=[{"Name": "name", "Values": ["al2023-ami-*x86_64"]},
                         {"Name": "state", "Values": ["available"]}]).get("Images", [])
            w(f"  describe_images 回傳 {len(imgs)} 筆")
            if imgs:
                ami = sorted(imgs, key=lambda x: x.get("CreationDate", ""))[-1]["ImageId"]
                w(f"  找到 AMI {ami}")
        except ClientError as exc:
            w(f"  無法列出 AMI：{exc.response['Error'].get('Code')}")

    # 取一個真實存在的 Security Group 來測試修改權限
    real_sg = None
    try:
        sgs = ec2.describe_security_groups(MaxResults=5).get("SecurityGroups", [])
        if sgs:
            real_sg = sgs[0]["GroupId"]
            w(f"  找到現有 Security Group {real_sg}（{sgs[0].get('GroupName')}）")
        else:
            w("  帳號內沒有現成的 Security Group")
    except ClientError as exc:
        w(f"  無法列出 Security Group：{exc.response['Error'].get('Code')}")

    can = {}
    if vpc_id:
        can["建立 Security Group"] = probe(
            "ec2:CreateSecurityGroup",
            lambda: ec2.create_security_group(
                GroupName="guardian-dryrun-test", Description="dry run",
                VpcId=vpc_id, DryRun=True))
    else:
        w("  [略過] ec2:CreateSecurityGroup（沒有可用的 VPC）")
        can["建立 Security Group"] = False

    if real_sg:
        can["修改 Security Group 規則"] = probe(
            "ec2:AuthorizeSecurityGroupIngress（放行那 4 個 IP 需要）",
            lambda: ec2.authorize_security_group_ingress(
                GroupId=real_sg, DryRun=True,
                IpPermissions=[{"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
                                "IpRanges": [{"CidrIp": "60.250.71.45/32"}]}]))
    else:
        w("  [略過] ec2:AuthorizeSecurityGroupIngress（沒有可測試的 SG）")
        can["修改 Security Group 規則"] = False

    if ami and subnet_id:
        can["啟動 EC2"] = probe(
            "ec2:RunInstances（部署程式需要）",
            lambda: ec2.run_instances(
                ImageId=ami, InstanceType="t3.small", MinCount=1, MaxCount=1,
                SubnetId=subnet_id, DryRun=True))
    else:
        w(f"  [略過] ec2:RunInstances（AMI={ami} subnet={subnet_id}）")
        can["啟動 EC2"] = False

    can["配置 Elastic IP"] = probe(
        "ec2:AllocateAddress（固定公開位址需要）",
        lambda: ec2.allocate_address(Domain="vpc", DryRun=True))

    w("")
    w("=" * 72)
    w("結論")
    w("=" * 72)
    ok = [k for k, v in can.items() if v]
    no = [k for k, v in can.items() if not v]
    for k in ok:
        w(f"  可以：{k}")
    for k in no:
        w(f"  不行：{k}")

    w("")
    if can.get("啟動 EC2"):
        w("  → 可以走 EC2 部署路線，那 4 個 IP 就能連上。")
    else:
        w("  → 目前這組憑證無法啟動 EC2，因此無法用這個帳號把服務對外開放。")
        w("     WSParticipantRole 是工作坊角色，通常限制建立運算資源。")
        w("")
        w("     可行的替代做法：")
        w("     A. 換一組有權限的 AWS 憑證（正式帳號或 IAM 使用者）再部署")
        w("     B. 先在本機或公司內網主機上跑，用固定對外 IP + 防火牆放行那 4 個位址")
        w("        （程式已支援：python cli.py serve --host 0.0.0.0）")
        w("     C. 只做示範時，用畫面共享而不對外開放服務")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        import traceback
        buf.append(traceback.format_exc())
    OUT.write_text("\n".join(buf), encoding="utf-8")
