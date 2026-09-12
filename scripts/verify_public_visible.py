"""驗證：公校來源的機構是否真的出現在儀表板（排行榜）上。

這支腳本回答使用者的問題「為什麼只有幼兒園、沒有公校」，
檢查四件事：
  1. 公校決算書 15 冊是否都已抽取
  2. 名冊機構是否已載入，且垃圾名已被清除
  3. 公校來源的機構是否有評分紀錄（有評分才會出現在排行榜）
  4. 排行榜實際回傳內容裡，公校來源機構佔幾筆
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from guardian import store  # noqa: E402
from guardian.ingest.pdf_text import is_valid_institution_name  # noqa: E402
from guardian.tools import scoring  # noqa: E402

OUT = ROOT / "data" / "verify_public.txt"
EXTRACTED = ROOT / "data" / "extracted"

buf: list[str] = []
problems: list[str] = []


def w(line: str = "") -> None:
    buf.append(line)


def check(label: str, ok: bool, detail: str = "") -> None:
    w(f"  [{'OK  ' if ok else 'FAIL'}] {label}{('　' + detail) if detail else ''}")
    if not ok:
        problems.append(label)


def main() -> None:
    w("=" * 72)
    w("1. 公校決算書抽取完整性")
    w("=" * 72)
    vols = sorted(EXTRACTED.glob("*決算書*.json"))
    check("15 冊決算書全部已抽取", len(vols) == 15, f"目前 {len(vols)} 冊")
    for v in vols:
        d = json.loads(v.read_text(encoding="utf-8"))
        insts = d.get("institutions") or {}
        w(f"      {v.stem:<22} {d.get('total_pages'):>4} 頁"
          f"　命中 {len(d.get('relevant_pages') or []):>3} 頁"
          f"　名冊 {len(insts):>3} 間"
          f"　補助基準 {len(d.get('fee_standards') or []):>3} 筆")

    w()
    w("=" * 72)
    w("2. 名冊載入與垃圾名清理")
    w("=" * 72)
    roster = store.q(
        "SELECT inst_id, name, inst_type, is_public_affiliated, sources "
        "FROM institutions WHERE sources LIKE ?", ("%公校決算書%",))
    w(f"  公校決算書來源的機構：{len(roster)} 間")
    bad = [r["name"] for r in roster if not is_valid_institution_name(r["name"])]
    check("沒有殘留的誤抓機構名", not bad, f"殘留 {bad}" if bad else "")

    municipal = [r for r in roster if "市立" in r["name"]]
    affiliated = [r for r in roster if r["is_public_affiliated"]]
    w(f"  其中市立幼兒園 {len(municipal)} 間、學校附設 {len(affiliated)} 間")
    check("市立幼兒園已載入", len(municipal) >= 20, f"{len(municipal)} 間")

    w()
    w("=" * 72)
    w("3. 公校來源機構是否有評分")
    w("=" * 72)
    scored = store.q(
        "SELECT COUNT(DISTINCT s.inst_id) AS n FROM scores s "
        "JOIN institutions i ON i.inst_id = s.inst_id "
        "WHERE i.sources LIKE ?", ("%公校決算書%",))
    n_scored = scored[0]["n"] if scored else 0
    w(f"  已評分：{n_scored} / {len(roster)} 間")
    check("公校來源機構已全部評分", n_scored == len(roster) and len(roster) > 0,
          f"{n_scored}/{len(roster)}")

    w()
    w("=" * 72)
    w("4. 排行榜實際內容")
    w("=" * 72)
    lb = scoring.leaderboard("新北市", 200)
    items = lb.get("leaderboard") or []
    w(f"  排行榜共 {len(items)} 筆")
    roster_names = {r["name"] for r in roster}
    in_lb = [it for it in items if it["name"] in roster_names]
    w(f"  其中公校來源 {len(in_lb)} 筆")
    check("排行榜看得到公校來源機構", len(in_lb) > 0, f"{len(in_lb)} 筆")
    for it in in_lb[:15]:
        w(f"      #{it['rank']:<3} {it['name']:<22} 總分 {it['total']:>5}"
          f"　{it['level']}")

    w()
    w("=" * 72)
    w(f"結論：{'全部通過' if not problems else f'{len(problems)} 項待處理'}")
    for p in problems:
        w(f"  - {p}")
    w("=" * 72)


if __name__ == "__main__":
    main()
    OUT.write_text("\n".join(buf), encoding="utf-8")
    raise SystemExit(1 if problems else 0)
