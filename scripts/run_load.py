"""執行 loader.load_all 並把完整結果寫成 UTF-8 檔案。

不透過 PowerShell 重導向，避免終端編碼與行程中止導致看不到結果。
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "data" / "run_load.txt"


def main() -> int:
    lines: list[str] = []
    try:
        from guardian.ingest import loader

        res = loader.load_all(city="新北市", purge_seed_data=True)
        detail = res.pop("detail", {})

        lines.append("=" * 72)
        lines.append("載入結果")
        lines.append("=" * 72)
        for k, v in res.items():
            lines.append(f"  {k:<28}: {v}")

        lines.append("")
        lines.append("=" * 72)
        lines.append("非營利園財報")
        lines.append("=" * 72)
        for n in detail.get("nonprofit", []):
            if n.get("skipped"):
                lines.append(f"  [跳過] {n['skipped']}：{n.get('reason')}")
                continue
            lines.append(f"  {n.get('institution')}　財務 {n.get('financial_rows')} 筆"
                         f"　年度 {n.get('years')}")

        lines.append("")
        lines.append("=" * 72)
        lines.append("公校逐園分決算（第5冊：各市立幼兒園自己的財務報表）")
        lines.append("=" * 72)
        for u in detail.get("units", []):
            if u.get("skipped"):
                lines.append(f"  [跳過] {u.get('file')}：{u.get('skipped')}")
                continue
            lines.append(f"  {u.get('file')}　年度 {u.get('year')}"
                         f"　機構 {u.get('units_loaded')} 間"
                         f"　財務 {u.get('financial_rows')} 筆")
            for one in (u.get("units") or [])[:6]:
                lines.append(f"      {one['institution']:<24}"
                             f" 財務 {one['financial_rows']:>4} 筆"
                             f"　員工 {one.get('staff_count')}"
                             f"　主表 {one.get('primary_source')}")

        lines.append("")
        lines.append("=" * 72)
        lines.append("名稱清理")
        lines.append("=" * 72)
        for n in res.get("roster_names_rejected") or []:
            lines.append(f"  擋掉誤抓：{n}")
        for m in res.get("roster_name_variants_merged") or []:
            lines.append(f"  合併重複：{m['removed']} → {m['kept']}")

        lines.append("")
        lines.append("=" * 72)
        lines.append("公校決算書（逐冊）")
        lines.append("=" * 72)
        for p in detail.get("public", []):
            lines.append(f"  {p.get('file')}　年度 {p.get('year')}"
                         f"　名冊 {p.get('institutions_from_roster')} 間"
                         f"　補助基準 {p.get('fee_standard_rows')} 筆"
                         f"　命中 {p.get('relevant_pages')} 頁")
            if p.get("rejected_names"):
                lines.append(f"      擋掉誤抓名稱：{p['rejected_names']}")
        return 0
    except Exception:  # noqa: BLE001
        lines.append("!! 載入失敗")
        lines.append(traceback.format_exc())
        return 1
    finally:
        OUT.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
