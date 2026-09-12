"""驗證實際執行中的伺服器：API 回傳內容是否含公校機構與資料覆蓋標示。"""

from __future__ import annotations

import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from guardian import config  # noqa: E402

OUT = ROOT / "data" / "verify_live.txt"
BASE = "http://127.0.0.1:8000"

buf: list[str] = []
problems: list[str] = []


def w(s: str = "") -> None:
    buf.append(s)


def check(label: str, ok: bool, detail: str = "") -> None:
    w(f"  [{'OK  ' if ok else 'FAIL'}] {label}{('　' + detail) if detail else ''}")
    if not ok:
        problems.append(label)


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=30) as r:  # noqa: S310
        return json.loads(r.read().decode("utf-8"))


def main() -> None:
    w("=" * 72)
    w("A. 伺服器與資料庫狀態")
    w("=" * 72)
    h = get("/api/health")
    w(f"  health = {json.dumps(h, ensure_ascii=False)[:300]}")

    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=10)
    n_inst = conn.execute("SELECT COUNT(*) FROM institutions").fetchone()[0]
    n_fin = conn.execute("SELECT COUNT(*) FROM financials").fetchone()[0]
    n_fee = conn.execute("SELECT COUNT(*) FROM fee_standards").fetchone()[0]
    n_roster = conn.execute(
        "SELECT COUNT(*) FROM institutions WHERE sources LIKE '%公校決算書%'").fetchone()[0]
    conn.close()
    w(f"  機構 {n_inst}　財務 {n_fin}　補助基準 {n_fee}　公校名冊來源 {n_roster}")
    check("補助／收費基準已從 15 冊決算書抽出", n_fee >= 100, f"{n_fee} 筆")
    check("公校名冊已載入", n_roster >= 20, f"{n_roster} 間")

    w()
    w("=" * 72)
    w("B. /api/leaderboard 是否含公校與資料覆蓋標示")
    w("=" * 72)
    lb = get("/api/leaderboard")
    items = lb.get("leaderboard") or []
    w(f"  回傳 {len(items)} 筆　coverage_summary="
      f"{json.dumps(lb.get('coverage_summary'), ensure_ascii=False)}")
    check("排行榜筆數涵蓋全部機構", len(items) == n_inst, f"{len(items)}/{n_inst}")
    check("回傳含 data_coverage 欄位",
          bool(items) and "data_coverage" in items[0])
    municipal = [it for it in items if "市立" in it["name"]]
    check("排行榜含市立（公校）幼兒園", len(municipal) >= 20, f"{len(municipal)} 間")
    roster_only = [it for it in items if it.get("roster_only")]
    check("僅有名冊者已標記 roster_only", len(roster_only) >= 20,
          f"{len(roster_only)} 間")

    w()
    w("  分數有實質意義的機構（證據完整／部分）：")
    for it in items:
        if not it.get("roster_only"):
            w(f"      #{it['rank']:<3} {it['name']:<24} {it['total']:>5}"
              f"　{it['level']:<6} 資料={it['data_coverage']}"
              f"{('（' + it['data_coverage_note'] + '）') if it['data_coverage_note'] else ''}")

    w()
    w("=" * 72)
    w("C. 前端頁面是否可取得")
    w("=" * 72)
    with urllib.request.urlopen(BASE + "/", timeout=30) as r:  # noqa: S310
        html = r.read().decode("utf-8")
    check("首頁回應 200 且含資料覆蓋欄位標題", "這個分數背後有多少資料" in html)
    check("首頁含 top_n=300 查詢", "top_n=300" in html)

    w()
    w("=" * 72)
    w(f"結論：{'全部通過' if not problems else f'{len(problems)} 項待處理'}")
    for p in problems:
        w(f"  - {p}")
    w("=" * 72)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        import traceback
        buf.append("!! 驗證過程出錯")
        buf.append(traceback.format_exc())
        problems.append("驗證腳本異常")
    OUT.write_text("\n".join(buf), encoding="utf-8")
    raise SystemExit(1 if problems else 0)
