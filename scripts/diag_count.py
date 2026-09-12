"""快速查詢：目前資料庫的機構數、財務筆數、收費基準筆數、評分數。"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from guardian import config  # noqa: E402

OUT = ROOT / "data" / "diag_count.txt"

conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=5)
lines = []
for label, sql in (
    ("institutions", "SELECT COUNT(*) FROM institutions"),
    ("  其中 市立", "SELECT COUNT(*) FROM institutions WHERE name LIKE '%市立%'"),
    ("  其中 附設", "SELECT COUNT(*) FROM institutions WHERE is_public_affiliated=1"),
    ("  公校決算書來源", "SELECT COUNT(*) FROM institutions WHERE sources LIKE '%公校決算書%'"),
    ("  示範資料", "SELECT COUNT(*) FROM institutions WHERE sources LIKE '%示範資料%'"),
    ("financials", "SELECT COUNT(*) FROM financials"),
    ("fee_standards", "SELECT COUNT(*) FROM fee_standards"),
    ("scores", "SELECT COUNT(DISTINCT inst_id) FROM scores"),
):
    try:
        n = conn.execute(sql).fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        n = f"ERR {exc}"
    lines.append(f"{label:<22}: {n}")

lines.append("")
lines.append("垃圾名是否還在：")
for r in conn.execute(
        "SELECT name FROM institutions WHERE name LIKE '%辦理%' OR name LIKE '%新建%' "
        "OR name LIKE '%育兒津%'"):
    lines.append(f"  !! {r[0]}")

conn.close()
OUT.write_text("\n".join(lines), encoding="utf-8")
