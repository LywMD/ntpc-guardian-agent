"""把「小小守護員」部署為 Amazon Bedrock Agent（Action Groups + Lambda）。

⚠ 這支腳本會在你的 AWS 帳號建立實體資源（IAM Role、Lambda、Bedrock Agent），
   屬於不易還原的操作，預設為 dry-run，只有加上 --apply 才會真的建立。

⚠ 尚未在本專案的環境實際執行過（工作坊帳號 WSParticipantRole 通常沒有
   iam:CreateRole 權限）。請先用 --dry-run 確認計畫，再視權限決定要不要跑。

本機模式（cli.py / api.py）已經是完整可用的 Agent：它用 Bedrock Converse API
的 tool use 迴圈自主規劃與呼叫工具。這支腳本的價值在於把同一組工具搬到
雲端託管的 Bedrock Agents，取得排程觸發、多使用者、Trace 主控台等能力。

用法
  python infra/deploy_bedrock_agent.py --dry-run
  python infra/deploy_bedrock_agent.py --apply
  python infra/deploy_bedrock_agent.py --apply --invoke "掃描新北市風險最高的機構"
  python infra/deploy_bedrock_agent.py --destroy --apply
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

from guardian import config, toolspec  # noqa: E402

AGENT_NAME = "guardian-risk-audit-agent"
LAMBDA_NAME = "guardian-agent-tools"
ROLE_AGENT = "guardian-bedrock-agent-role"
ROLE_LAMBDA = "guardian-agent-lambda-role"
RUNTIME = "python3.12"


def log(msg: str) -> None:
    print(f"  {msg}")


# ------------------------------------------------------------------ IAM
def _trust(service: str) -> str:
    return json.dumps({
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow",
                       "Principal": {"Service": service},
                       "Action": "sts:AssumeRole"}],
    })


def ensure_role(iam, name: str, service: str, policies: list[dict], apply: bool) -> str | None:
    try:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        log(f"IAM Role 已存在：{name}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NoSuchEntity":
            raise
        if not apply:
            log(f"[dry-run] 會建立 IAM Role {name}（信任 {service}）")
            return None
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=_trust(service),
                              Description="小小守護員風險稽查 Agent")["Role"]["Arn"]
        log(f"已建立 IAM Role：{name}")
        time.sleep(10)  # IAM 傳播
    for i, doc in enumerate(policies):
        if not apply:
            log(f"[dry-run] 會掛上 inline policy {name}-p{i}")
            continue
        iam.put_role_policy(RoleName=name, PolicyName=f"{name}-p{i}",
                            PolicyDocument=json.dumps(doc))
    return arn


LAMBDA_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {"Effect": "Allow",
         "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": "arn:aws:logs:*:*:*"},
        {"Effect": "Allow",
         "Action": ["bedrock:InvokeModel"], "Resource": "*"},
        {"Effect": "Allow",
         "Action": ["s3:GetObject", "s3:PutObject"],
         "Resource": f"arn:aws:s3:::{config.S3_BUCKET or 'guardian-placeholder'}/*"},
    ],
}
AGENT_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {"Effect": "Allow",
         "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
         "Resource": "*"},
    ],
}


# ------------------------------------------------------------------ Lambda
def build_zip() -> bytes:
    """把 guardian/ 與 handler 打包。依賴（boto3 除外）建議另做 Layer。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(ROOT / "infra" / "lambda_handler.py", "lambda_handler.py")
        for p in (ROOT / "guardian").rglob("*.py"):
            z.write(p, str(p.relative_to(ROOT)))
    return buf.getvalue()


def ensure_lambda(lam, role_arn: str | None, apply: bool) -> str | None:
    code = build_zip()
    log(f"部署包大小：{len(code) / 1024:.0f} KB")
    env = {"Variables": {"GUARDIAN_DB": "/tmp/guardian.db",
                         "AWS_REGION_OVERRIDE": config.AWS_REGION,
                         **({"GUARDIAN_S3_BUCKET": config.S3_BUCKET} if config.S3_BUCKET else {})}}
    try:
        lam.get_function(FunctionName=LAMBDA_NAME)
        if not apply:
            log(f"[dry-run] 會更新 Lambda {LAMBDA_NAME} 的程式碼")
            return None
        lam.update_function_code(FunctionName=LAMBDA_NAME, ZipFile=code)
        log(f"已更新 Lambda：{LAMBDA_NAME}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        if not apply or not role_arn:
            log(f"[dry-run] 會建立 Lambda {LAMBDA_NAME}（{RUNTIME}, 300s, 1024MB）")
            return None
        lam.create_function(
            FunctionName=LAMBDA_NAME, Runtime=RUNTIME, Role=role_arn,
            Handler="lambda_handler.lambda_handler", Code={"ZipFile": code},
            Timeout=300, MemorySize=1024, Environment=env,
            Description="小小守護員 Agent 四大工具")
        log(f"已建立 Lambda：{LAMBDA_NAME}")
    return lam.get_function(FunctionName=LAMBDA_NAME)["Configuration"]["FunctionArn"] if apply else None


# ------------------------------------------------------------------ Action Groups
def _function_schema(spec: dict) -> dict:
    """把 Converse toolSpec 轉成 Bedrock Agents 的 function schema。"""
    ts = spec["toolSpec"]
    js = ts["inputSchema"]["json"]
    params = {}
    for pname, pdef in js.get("properties", {}).items():
        ptype = pdef.get("type", "string")
        params[pname] = {
            "type": {"integer": "integer", "number": "number",
                     "boolean": "boolean"}.get(ptype, "string"),
            "description": pdef.get("description", pname)
                           + (f"（可選值：{pdef['enum']}）" if pdef.get("enum") else ""),
            "required": pname in js.get("required", []),
        }
    return {"name": ts["name"], "description": ts["description"][:1200], "parameters": params}


def ensure_agent(bedrock_agent, lambda_arn: str | None, agent_role_arn: str | None,
                 model_id: str, instruction: str, apply: bool) -> str | None:
    existing = None
    for a in bedrock_agent.list_agents(maxResults=100).get("agentSummaries", []):
        if a["agentName"] == AGENT_NAME:
            existing = a["agentId"]
    if existing:
        agent_id = existing
        log(f"Bedrock Agent 已存在：{agent_id}")
    else:
        if not apply or not agent_role_arn:
            log(f"[dry-run] 會建立 Bedrock Agent {AGENT_NAME}（模型 {model_id}）")
            log(f"[dry-run] 會建立 {len(toolspec.TOOL_SPECS)} 個 Action Group："
                f"{', '.join(t['toolSpec']['name'] for t in toolspec.TOOL_SPECS)}")
            return None
        agent_id = bedrock_agent.create_agent(
            agentName=AGENT_NAME, agentResourceRoleArn=agent_role_arn,
            foundationModel=model_id, instruction=instruction,
            idleSessionTTLInSeconds=1800,
            description="小小守護員：兒少機構風險稽查智能代理人")["agent"]["agentId"]
        log(f"已建立 Bedrock Agent：{agent_id}")
        time.sleep(8)

    if not apply:
        return agent_id

    existing_groups = {g["actionGroupName"]: g["actionGroupId"] for g in
                       bedrock_agent.list_agent_action_groups(agentId=agent_id,
                                                    agentVersion="DRAFT").get("actionGroupSummaries", [])}
    for spec in toolspec.TOOL_SPECS:
        name = spec["toolSpec"]["name"]
        fs = {"functions": [_function_schema(spec)]}
        if name in existing_groups:
            bedrock_agent.update_agent_action_group(
                agentId=agent_id, agentVersion="DRAFT",
                actionGroupId=existing_groups[name], actionGroupName=name,
                actionGroupExecutor={"lambda": lambda_arn},
                functionSchema=fs, actionGroupState="ENABLED")
            log(f"已更新 Action Group：{name}")
        else:
            bedrock_agent.create_agent_action_group(
                agentId=agent_id, agentVersion="DRAFT", actionGroupName=name,
                description=spec["toolSpec"]["description"][:200],
                actionGroupExecutor={"lambda": lambda_arn},
                functionSchema=fs, actionGroupState="ENABLED")
            log(f"已建立 Action Group：{name}")

    bedrock_agent.prepare_agent(agentId=agent_id)
    log("已送出 prepare_agent，等待 PREPARED…")
    for _ in range(40):
        status = bedrock_agent.get_agent(agentId=agent_id)["agent"]["agentStatus"]
        if status in ("PREPARED", "FAILED"):
            log(f"Agent 狀態：{status}")
            break
        time.sleep(5)

    aliases = {a["agentAliasName"]: a["agentAliasId"] for a in
               bedrock_agent.list_agent_aliases(agentId=agent_id).get("agentAliasSummaries", [])}
    if "prod" not in aliases:
        alias_id = bedrock_agent.create_agent_alias(agentId=agent_id,
                                          agentAliasName="prod")["agentAlias"]["agentAliasId"]
        log(f"已建立 alias prod：{alias_id}")
    return agent_id


def allow_bedrock_invoke(lam, lambda_arn: str, account: str, agent_id: str, apply: bool) -> None:
    if not apply:
        log("[dry-run] 會授權 bedrock.amazonaws.com 呼叫 Lambda")
        return
    try:
        lam.add_permission(
            FunctionName=LAMBDA_NAME, StatementId="bedrock-agent-invoke",
            Action="lambda:InvokeFunction", Principal="bedrock.amazonaws.com",
            SourceArn=f"arn:aws:bedrock:{config.AWS_REGION}:{account}:agent/{agent_id}")
        log("已授權 Bedrock 呼叫 Lambda")
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceConflictException":
            log("Lambda 呼叫權限已存在")
        else:
            raise


# ------------------------------------------------------------------ 主流程
def main() -> int:
    ap = argparse.ArgumentParser(description="部署小小守護員為 Bedrock Agent")
    ap.add_argument("--apply", action="store_true", help="真的建立／更新資源")
    ap.add_argument("--dry-run", action="store_true", help="只列出會做什麼（預設）")
    ap.add_argument("--destroy", action="store_true", help="刪除本腳本建立的資源")
    ap.add_argument("--invoke", help="部署後用這句話實測 Agent")
    args = ap.parse_args()
    apply = args.apply and not args.dry_run

    from guardian import aws as gaws

    sess = gaws.session()
    iam, lam = sess.client("iam"), sess.client("lambda")
    bedrock_agent = sess.client("bedrock-agent")
    account = gaws.whoami()["account"]

    print("=" * 72)
    print(f"{'實際部署' if apply else 'DRY-RUN（不會建立任何資源）'}"
          f"　帳號 {account}　區域 {config.AWS_REGION}")
    print("=" * 72)

    if args.destroy:
        if not apply:
            log("[dry-run] 會刪除 Bedrock Agent、Lambda 與兩個 IAM Role")
            return 0
        for a in bedrock_agent.list_agents(maxResults=100).get("agentSummaries", []):
            if a["agentName"] == AGENT_NAME:
                bedrock_agent.delete_agent(agentId=a["agentId"], skipResourceInUseCheck=True)
                log(f"已刪除 Agent {a['agentId']}")
        try:
            lam.delete_function(FunctionName=LAMBDA_NAME)
            log(f"已刪除 Lambda {LAMBDA_NAME}")
        except ClientError:
            pass
        for role in (ROLE_LAMBDA, ROLE_AGENT):
            try:
                for p in iam.list_role_policies(RoleName=role)["PolicyNames"]:
                    iam.delete_role_policy(RoleName=role, PolicyName=p)
                iam.delete_role(RoleName=role)
                log(f"已刪除 IAM Role {role}")
            except ClientError:
                pass
        return 0

    print("\n[1] IAM Roles")
    lambda_role = ensure_role(iam, ROLE_LAMBDA, "lambda.amazonaws.com", [LAMBDA_POLICY], apply)
    agent_role = ensure_role(iam, ROLE_AGENT, "bedrock.amazonaws.com", [AGENT_POLICY], apply)

    print("\n[2] Lambda（Action Group 執行器）")
    lambda_arn = ensure_lambda(lam, lambda_role, apply)

    print("\n[3] Bedrock Agent 與 Action Groups")
    from guardian.agent import SYSTEM_PROMPT

    model_id = gaws.resolve_model_id()
    agent_id = ensure_agent(bedrock_agent, lambda_arn, agent_role, model_id, SYSTEM_PROMPT, apply)

    if apply and agent_id and lambda_arn:
        print("\n[4] Lambda 呼叫權限")
        allow_bedrock_invoke(lam, lambda_arn, account, agent_id, apply)

    if args.invoke and apply and agent_id:
        print("\n[5] 實測呼叫")
        rt = sess.client("bedrock-agent-runtime")
        resp = rt.invoke_agent(agentId=agent_id, agentAliasId="prod",
                               sessionId=f"test-{int(time.time())}", inputText=args.invoke)
        for ev in resp["completion"]:
            if "chunk" in ev:
                sys.stdout.write(ev["chunk"]["bytes"].decode("utf-8"))
        print()

    print("\n" + "=" * 72)
    if not apply:
        print("這是 dry-run。確認上面的計畫沒問題後，加 --apply 才會真的建立資源。")
        print("注意：工作坊型帳號常缺 iam:CreateRole 權限，屆時請改用已存在的角色 ARN。")
    else:
        print(f"完成。Agent ID：{agent_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
