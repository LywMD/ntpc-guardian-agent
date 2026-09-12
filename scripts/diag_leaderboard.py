"""驗證 leaderboard 的 data_coverage 欄位：SQL 可執行、分類正確。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from guardian.tools import scoring  # noqa: E402

OUT = ROOT / "data" / "diag_leaderboard.txt"
buf: list[str] = []

d = scoring.leaderboard("新北市", 200)
buf.append(f"count = {d['count']}")
buf.append(f"coverage_summary = {json.dumps(d.get('coverage_summary'), ensure_ascii=False)}")
buf.append("")
buf.append(f"{'#':<4}{'機構':<26}{'總分':>6}  {'等級':<8}{'資料':<6} 說明")
buf.append("-" * 92)
for r in d["leaderboard"]:
    buf.append(
        f"{r['rank']:<4}{r['name'].replace('新北市',''):<26}"
        f"{r['total']:>6}  {r['level']:<8}{r['data_coverage']:<6} {r['data_coverage_note']}")

OUT.write_text("\n".join(buf), encoding="utf-8")
