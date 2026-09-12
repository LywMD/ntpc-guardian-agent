"""Bedrock Agents Action Group 的 Lambda 進入點。

同一份工具實作（guardian/tools/*）在兩種模式下共用：
  - 本機 / API 模式：guardian.toolspec.execute() 直接呼叫
  - 雲端模式：Bedrock Agent 透過 Action Group 呼叫本 Lambda

打包時需把 guardian/ 目錄與依賴一起放進部署包或 Lambda Layer，
並把 GUARDIAN_DB 指向 /tmp 或改接 RDS/DynamoDB（見 README 的正式化路線）。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

os.environ.setdefault("GUARDIAN_DB", "/tmp/guardian.db")

from guardian import toolspec  # noqa: E402

log = logging.getLogger()
log.setLevel(logging.INFO)


def _coerce(params: list[dict[str, Any]]) -> dict[str, Any]:
    """Bedrock Agent 傳來的 parameters 陣列 → dict，並還原型別。"""
    out: dict[str, Any] = {}
    for p in params or []:
        name, value, ptype = p.get("name"), p.get("value"), (p.get("type") or "string").lower()
        if name is None:
            continue
        if ptype == "integer":
            try:
                value = int(value)
            except (TypeError, ValueError):
                pass
        elif ptype == "number":
            try:
                value = float(value)
            except (TypeError, ValueError):
                pass
        elif ptype == "boolean":
            value = str(value).strip().lower() in ("true", "1", "yes")
        out[name] = value
    return out


def lambda_handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    log.info("event: %s", json.dumps(event, ensure_ascii=False)[:2000])

    action_group = event.get("actionGroup", "")
    function = event.get("function") or event.get("apiPath", "").strip("/")
    tool_name = function or action_group

    args = _coerce(event.get("parameters", []))
    # requestBody 形式（OpenAPI schema 的 Action Group）
    props = (event.get("requestBody", {}).get("content", {})
             .get("application/json", {}).get("properties", []))
    if props:
        args.update(_coerce(props))

    if tool_name not in toolspec.DISPATCH:
        body = {"error": f"未知的工具 {tool_name}，可用：{list(toolspec.DISPATCH)}"}
    else:
        body = toolspec.execute(tool_name, args, session_id=event.get("sessionId", "lambda"))["result"]

    text = json.dumps(body, ensure_ascii=False, default=str)
    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": action_group,
            "function": function,
            "functionResponse": {
                "responseBody": {"TEXT": {"body": text[:24000]}}
            },
        },
        "sessionAttributes": event.get("sessionAttributes", {}),
        "promptSessionAttributes": event.get("promptSessionAttributes", {}),
    }
