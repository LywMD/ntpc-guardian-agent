"""實測哪些 Bedrock 模型真的可以用 Converse + Tool Use 呼叫。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from botocore.exceptions import ClientError  # noqa: E402

from guardian import aws  # noqa: E402

CANDIDATES = [
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.anthropic.claude-sonnet-4-20250514-v1:0",
    "us.anthropic.claude-3-7-sonnet-20250219-v1:0",
    "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
    "us.amazon.nova-pro-v1:0",
    "amazon.nova-pro-v1:0",
    "us.amazon.nova-lite-v1:0",
]

TOOL_CFG = {
    "tools": [{
        "toolSpec": {
            "name": "ping",
            "description": "回報狀態",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {"msg": {"type": "string"}},
                "required": ["msg"],
            }},
        }
    }]
}


def main() -> int:
    br = aws.bedrock_runtime()
    ok: list[str] = []
    for model_id in CANDIDATES:
        try:
            resp = br.converse(
                modelId=model_id,
                messages=[{"role": "user", "content": [
                    {"text": "請呼叫 ping 工具，msg 帶 hello。"}]}],
                toolConfig=TOOL_CFG,
                inferenceConfig={"maxTokens": 200, "temperature": 0.0},
            )
            used_tool = any("toolUse" in c for c in resp["output"]["message"]["content"])
            print(f"[OK]   {model_id}  stopReason={resp['stopReason']}  toolUse={used_tool}")
            ok.append(model_id)
        except ClientError as exc:
            code = exc.response["Error"].get("Code")
            msg = exc.response["Error"].get("Message", "")[:110]
            print(f"[FAIL] {model_id}  {code}: {msg}")
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {model_id}  {type(exc).__name__}: {str(exc)[:110]}")

    print("\n可用模型：")
    for m in ok:
        print("  -", m)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
