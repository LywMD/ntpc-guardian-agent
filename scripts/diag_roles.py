"""檢查每一頁被歸成什麼 role，以及為什麼。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from guardian.ingest import loader  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="data/extracted 下的檔名片段")
    args = ap.parse_args()

    hits = list((ROOT / "data" / "extracted").glob(f"*{args.file}*.json"))
    if not hits:
        print("找不到檔案")
        return 1
    doc = json.loads(hits[0].read_text(encoding="utf-8"))
    main_inst = doc.get("institution") or ""
    print(f"文件：{hits[0].name}")
    print(f"主體：{main_inst}")
    print(f"文件年度推算：{loader._academic_year_to_ad(doc.get('file'))}\n")
    print(f"{'頁':>4} {'role':<11} {'年度':>6} {'關聯實體':<6} {'筆數':>5} "
          f"{'conf':>5}  statement / period")
    print("-" * 110)
    for e in doc.get("extracted", []):
        stmt = e.get("statement") or ""
        period = e.get("period") or ""
        role = loader._statement_role(stmt, period)
        year = (loader._academic_year_to_ad(period)
                or loader._academic_year_to_ad(doc.get("file")))
        related = loader._is_related_entity(e.get("institution"), main_inst)
        n = len(e.get("line_items", []))
        n_amt = sum(1 for it in e.get("line_items", [])
                    if loader._clean_amount(it.get("amount")) is not None)
        n_sub = sum(1 for it in e.get("line_items", [])
                    if loader._is_subtotal(it.get("subject") or ""))
        n_flow = sum(1 for it in e.get("line_items", [])
                     if loader._norm_flow(it) in ("income", "expense"))
        print(f"{e.get('page'):>4} {role:<11} {str(year):>6} {'排除' if related else '':<6} "
              f"{n:>5} {str(e.get('confidence')):>5}  {stmt[:44]} | {period[:34]}")
        print(f"     └ 有金額 {n_amt}／收支類 {n_flow}／小計列 {n_sub}"
              f"　cols={e.get('cols')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
