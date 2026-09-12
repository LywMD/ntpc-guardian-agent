"""診斷載入後的財務明細：看每一頁被歸成什麼 role、合計怎麼算出來的。"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "verify_real.db"))
    ap.add_argument("--name")
    args = ap.parse_args()
    os.environ["GUARDIAN_DB"] = args.db

    from guardian import store

    print(f"DB: {args.db}\n")
    insts = store.q("SELECT inst_id, name FROM institutions"
                    " WHERE inst_id IN (SELECT DISTINCT inst_id FROM financials)"
                    " ORDER BY name")
    for i in insts:
        if args.name and args.name not in i["name"]:
            continue
        print("=" * 78)
        print(f"{i['name']}　{i['inst_id']}")
        print("=" * 78)
        rows = store.q(
            "SELECT year, role, flow, source, COUNT(*) n, SUM(amount) total"
            " FROM financials WHERE inst_id=?"
            " GROUP BY year, role, flow, source ORDER BY year, role, flow, source",
            (i["inst_id"],))
        for r in rows:
            print(f"  {r['year']}  {(r['role'] or 'NULL'):<11} {r['flow']:<8}"
                  f" {r['n']:>3} 筆  {r['total']:>16,.0f}   {(r['source'] or '')[:58]}")

        print("\n  --- 只算 primary（合計與比率的來源）---")
        agg = store.q(
            "SELECT year, flow, SUM(amount) total, COUNT(*) n FROM financials"
            " WHERE inst_id=? AND role='primary' GROUP BY year, flow ORDER BY year, flow",
            (i["inst_id"],))
        for r in agg:
            print(f"  {r['year']}  {r['flow']:<8} {r['n']:>3} 筆  {r['total']:>16,.0f}")

        print("\n  --- primary 明細 ---")
        for r in store.q(
            "SELECT year, flow, subject, amount FROM financials"
            " WHERE inst_id=? AND role='primary' ORDER BY year, flow, amount DESC",
                (i["inst_id"],)):
            print(f"  {r['year']}  {r['flow']:<8} {r['subject'][:26]:<28} {r['amount']:>16,.0f}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
