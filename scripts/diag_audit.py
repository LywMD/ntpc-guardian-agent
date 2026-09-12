"""查看 Agent 最近的工具呼叫軌跡，判斷是否卡住或正在多輪呼叫。"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from guardian import config  # noqa: E402

OUT = ROOT / "data" / "diag_audit.txt"
buf: list[str] = []

conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=10)
conn.row_factory = sqlite3.Row

cols = {r["name"] for r in conn.execute("PRAGMA table_info(audit_log)")}
buf.append(f"audit_log 欄位 = {sorted(cols)}")
buf.append("")

n = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
buf.append(f"總筆數 = {n}")
buf.append("")
buf.append("最近 25 筆：")
for r in conn.execute(
        "SELECT * FROM audit_log ORDER BY rowid DESC LIMIT 25"):
    d = dict(r)
    args = str(d.get("args"))[:110]
    res = str(d.get("result"))[:110]
    buf.append(f"  {d.get('created_at') or d.get('ts')}  step={d.get('step')}  "
               f"{d.get('tool') or d.get('tool_name')}  {d.get('latency_ms')}ms")
    buf.append(f"      args={args}")
    buf.append(f"      res ={res}")

conn.close()
OUT.write_text("\n".join(buf), encoding="utf-8")
