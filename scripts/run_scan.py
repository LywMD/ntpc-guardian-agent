"""執行全市風險掃描並把結果寫成 UTF-8 檔案。"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "data" / "run_scan.txt"


def main() -> int:
    lines: list[str] = []
    try:
        from guardian.tools import scoring

        res = scoring.scan_city("新北市")
        lines.append(f"掃描機構數 : {res['scanned']}")
        lines.append(f"掃描時間   : {res['scanned_at']}")
        lines.append(f"風險分布   : {res['risk_distribution']}")
        lines.append(f"加派工具   : {res['escalated_institutions']}")
        lines.append("")
        lines.append("前 20 名")
        lines.append("-" * 72)
        for r in res["top_20"]:
            lines.append(f"  {r['rank']:<3} {r['name']:<24} {r['total']:>6}  {r['level']}")
        return 0
    except Exception:  # noqa: BLE001
        lines.append("!! 掃描失敗")
        lines.append(traceback.format_exc())
        return 1
    finally:
        OUT.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
