"""驗證機構名稱過濾器：確認決算書跨行誤抓的片段會被擋掉，正常名稱會通過。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from guardian.ingest.pdf_text import is_valid_institution_name  # noqa: E402

OUT = ROOT / "data" / "diag_namefilter.txt"

SHOULD_REJECT = [
    "及設備辦理非營利幼兒園",
    "中心新建非營利幼兒園",
    "學前助及育兒津非營利幼兒園",
]

SHOULD_PASS = [
    "新北市立板橋幼兒園",
    "新北市立烏來幼兒園",
    "忠孝國中附設幼兒園",
    "北港國民小學附設幼兒園",
    "安溪非營利幼兒園",
    "新北市北大非營利幼兒園",
]

buf: list[str] = []
fails = 0

buf.append("應被擋掉的誤抓片段")
buf.append("-" * 60)
for n in SHOULD_REJECT:
    ok = is_valid_institution_name(n)
    mark = "OK  擋掉" if not ok else "FAIL 沒擋住"
    if ok:
        fails += 1
    buf.append(f"  {mark}  {n}")

buf.append("")
buf.append("應通過的正常名稱")
buf.append("-" * 60)
for n in SHOULD_PASS:
    ok = is_valid_institution_name(n)
    mark = "OK  通過" if ok else "FAIL 被誤擋"
    if not ok:
        fails += 1
    buf.append(f"  {mark}  {n}")

buf.append("")
buf.append(f"結果：{'全部正確' if fails == 0 else f'{fails} 項不符預期'}")

OUT.write_text("\n".join(buf), encoding="utf-8")
print("\n".join(buf))
raise SystemExit(1 if fails else 0)
