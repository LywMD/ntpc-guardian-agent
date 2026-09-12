"""重建完整資料集：真實文件 + 示範資料集並存，然後全市評分。

用途：真實文件（S3 財報／決算書）不含裁罰、逐園收費與社群貼文，
官方那三類開放資料在本機受政府憑證問題與 API 金鑰限制無法取得。
本腳本讓兩個資料集並存（以 institutions.dataset 區分），
確保四大子分數與五項工具都有資料可跑，同時統計母體不互相污染。
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "data" / "run_full_etl.txt"
lines: list[str] = []


def w(s: str = "") -> None:
    lines.append(s)


def main() -> int:
    try:
        from guardian import store
        from guardian.tools import etl, forensic, scoring

        w("=" * 72)
        w("步驟 1：ETL（真實資料 + 示範資料集並存）")
        w("=" * 72)
        res = etl.refresh(city="新北市", allow_live=False, include_demo=True)
        for k in ("ok", "source_mode", "institutions_upserted",
                  "duplicates_merged_by_fuzzy_match", "fee_rows",
                  "financial_rows", "penalty_rows", "social_posts"):
            w(f"  {k:<34}: {res.get(k)}")
        w("  notes:")
        for n in res.get("notes") or []:
            w(f"      - {n}")
        gaps = res.get("missing_layers") or []
        w(f"  仍缺的訊號層：{len(gaps)}")
        for g in gaps:
            w(f"      - {g['layer']}：{g['impact']}")

        w()
        w("=" * 72)
        w("步驟 2：資料庫各層筆數")
        w("=" * 72)
        st = store.stats()
        for k, v in st.items():
            w(f"  {k:<20}: {v}")
        w()
        for r in store.q(
                "SELECT COALESCE(dataset,'(未標記)') AS d, COUNT(*) AS n"
                " FROM institutions GROUP BY 1 ORDER BY n DESC"):
            w(f"  dataset={r['d']:<12} {r['n']} 間")

        w()
        w("=" * 72)
        w("步驟 3：全市評分")
        w("=" * 72)
        forensic.clear_peer_cache()
        scan = scoring.scan_city("新北市")
        w(f"  掃描 {scan['scanned']} 間")
        w(f"  風險分布 {scan['risk_distribution']}")
        w(f"  自動加派工具 {len(scan['escalated_institutions'])} 間："
          f"{scan['escalated_institutions'][:6]}")

        w()
        w("=" * 72)
        w("步驟 4：四大子分數是否都有訊號")
        w("=" * 72)
        agg = store.q1(
            "SELECT COUNT(*) n,"
            " SUM(CASE WHEN financial_sub  > 0 THEN 1 ELSE 0 END) fin,"
            " SUM(CASE WHEN compliance_sub > 0 THEN 1 ELSE 0 END) com,"
            " SUM(CASE WHEN sentiment_sub  > 0 THEN 1 ELSE 0 END) sen,"
            " SUM(CASE WHEN history_sub    > 0 THEN 1 ELSE 0 END) his"
            " FROM scores s WHERE scored_at = ("
            "   SELECT MAX(scored_at) FROM scores x WHERE x.inst_id = s.inst_id)")
        w(f"  最新評分機構數        : {agg['n']}")
        w(f"  財務子分數 > 0        : {agg['fin']}")
        w(f"  法規遵循子分數 > 0    : {agg['com']}")
        w(f"  社群輿情子分數 > 0    : {agg['sen']}  ← 雙訊號的另一半")
        w(f"  歷史紀錄子分數 > 0    : {agg['his']}")

        w()
        w("  輿情子分數最高的 8 間：")
        for r in store.q(
                "SELECT i.name, s.sentiment_sub, s.total, s.level,"
                " COALESCE(i.dataset,'real') AS d"
                " FROM scores s JOIN institutions i ON i.inst_id=s.inst_id"
                " WHERE s.scored_at = (SELECT MAX(scored_at) FROM scores x"
                "                      WHERE x.inst_id=s.inst_id)"
                " ORDER BY s.sentiment_sub DESC LIMIT 8"):
            w(f"      {r['name']:<30} 輿情 {r['sentiment_sub']:>5}"
              f"　總分 {r['total']:>5}　{r['level']:<6} [{r['d']}]")

        w()
        w("  預警數：" + str(len(scoring.alerts(200).get("alerts") or [])))
        return 0
    except Exception:  # noqa: BLE001
        w("!! 失敗")
        w(traceback.format_exc())
        return 1
    finally:
        OUT.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
