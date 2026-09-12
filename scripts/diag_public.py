"""診斷：公校決算書（data/extracted 內的 pdf_text 結果）抽取與載入狀況。

回答「為什麼儀表板只有幼兒園、沒有公校」——確認 15 份公校決算書
各自命中幾頁附設幼兒園、抓到幾個機構線索、是否有金額可用。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

EXTRACTED = ROOT / "data" / "extracted"
OUT = ROOT / "data" / "diag_public.txt"

buf: list[str] = []


def w(line: str = "") -> None:
    buf.append(line)


def main() -> None:
    files = sorted(EXTRACTED.glob("*.json"))
    w(f"data/extracted 共 {len(files)} 個 JSON")
    w()

    public_docs: list[tuple[Path, dict]] = []
    ocr_docs: list[tuple[Path, dict]] = []

    for fp in files:
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            w(f"  !! 無法解析 {fp.name}: {exc}")
            continue
        # pdf_text.parse_settlement 產出有 relevant_pages / table_row_count
        if "relevant_pages" in d or "table_row_count" in d:
            public_docs.append((fp, d))
        else:
            ocr_docs.append((fp, d))

    w(f"公校型（pdf_text）：{len(public_docs)} 份")
    w(f"OCR 型（pdf_ocr）  ：{len(ocr_docs)} 份")
    w()

    w("=" * 72)
    w("公校決算書逐份明細")
    w("=" * 72)
    for fp, d in public_docs:
        insts = d.get("institutions") or []
        if isinstance(insts, dict):
            inst_names = list(insts.keys())
        else:
            inst_names = [i if isinstance(i, str) else str(i) for i in insts]
        rows = d.get("rows") or d.get("table_rows") or []
        w(f"\n{fp.name}")
        w(f"  總頁數        : {d.get('total_pages')}")
        w(f"  命中幼兒園頁  : {len(d.get('relevant_pages') or [])}")
        w(f"  表格列數      : {d.get('table_row_count', len(rows))}")
        w(f"  機構線索      : {len(inst_names)} 個")
        for name in inst_names[:10]:
            w(f"      - {name}")
        # 找出有金額的列
        amt_rows = [r for r in rows if isinstance(r, dict) and r.get("amount") not in (None, "")]
        w(f"  有金額的列    : {len(amt_rows)}")
        for r in amt_rows[:5]:
            w(f"      {r.get('item')!r} = {r.get('amount')!r} (flow={r.get('flow')!r})")
        if rows and not amt_rows:
            w("  !! 有表格列但沒有任何 amount → 欄位對照未命中")
            sample = rows[0]
            if isinstance(sample, dict):
                w(f"     首列 keys = {sorted(sample.keys())}")
                w(f"     首列 = {json.dumps(sample, ensure_ascii=False)[:400]}")

    w()
    w("=" * 72)
    w("OCR 型（非營利園）對照")
    w("=" * 72)
    for fp, d in ocr_docs:
        n_amt = sum(
            1
            for e in d.get("extracted", [])
            for it in e.get("line_items", [])
            if it.get("amount") not in (None, "")
        )
        w(f"  {fp.name:<44} 機構={d.get('institution')!r} 有金額={n_amt}")


if __name__ == "__main__":
    main()
    OUT.write_text("\n".join(buf), encoding="utf-8")
