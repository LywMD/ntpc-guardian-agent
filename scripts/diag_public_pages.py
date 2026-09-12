"""診斷：檢查公校決算書「命中幼兒園」頁面的實際內容。

要回答的問題：這份全市決算書裡有沒有「逐園」的財務數字？
若有 → 應該抽出來算各園財務異常分數。
若沒有 → 公校附幼只能有名冊，財務分數必須標為 unverifiable。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "data" / "diag_public_pages.txt"
buf: list[str] = []


def w(line: str = "") -> None:
    buf.append(line)


def main() -> None:
    fp = ROOT / "data" / "extracted" / "112年決算書第一冊.json"
    d = json.loads(fp.read_text(encoding="utf-8"))

    w(f"檔案 = {fp.name}")
    w(f"最上層 keys = {sorted(d.keys())}")
    w(f"總頁數 = {d.get('total_pages')}")
    rel = d.get("relevant_pages") or []
    w(f"命中頁 = {len(rel)} 頁：{rel[:60]}")
    w()

    # relevant_pages 的結構
    if rel and isinstance(rel[0], dict):
        w("relevant_pages 是 dict，首筆 keys = " + str(sorted(rel[0].keys())))
        w(json.dumps(rel[0], ensure_ascii=False, indent=1)[:2000])
    w()

    rows = d.get("table_rows") or []
    w("=" * 72)
    w(f"table_rows 共 {len(rows)} 列（全部列出，判斷是全市層級還是逐園層級）")
    w("=" * 72)
    for r in rows:
        w(json.dumps(r, ensure_ascii=False))
    w()

    fees = d.get("fee_standards") or []
    w("=" * 72)
    w(f"fee_standards 共 {len(fees)} 筆（前 40 筆）")
    w("=" * 72)
    for f in fees[:40]:
        w(json.dumps(f, ensure_ascii=False))
    w()

    insts = d.get("institutions") or {}
    w("=" * 72)
    w(f"機構名冊 {len(insts)} 間，及其出現頁碼")
    w("=" * 72)
    for name, pages in insts.items():
        w(f"  {name}  → 頁 {pages}")
    w()

    rej = d.get("rejected_name_fragments") or {}
    w("=" * 72)
    w(f"被過濾掉的疑似誤抓名稱 {len(rej)} 個")
    w("=" * 72)
    for name, pages in list(rej.items())[:40]:
        w(f"  {name}  → 頁 {pages}")


if __name__ == "__main__":
    main()
    OUT.write_text("\n".join(buf), encoding="utf-8")
