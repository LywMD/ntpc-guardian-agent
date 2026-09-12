"""說明示範資料的來源與目前在系統中的影響範圍。

要回答的問題：那 40 筆機構是哪來的、哪些數字是編的、
以及目前排行榜上有多少是合成資料。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from guardian import seed, store  # noqa: E402
from guardian.tools import scoring  # noqa: E402

OUT = ROOT / "data" / "explain_demo.txt"
buf: list[str] = []


def w(s: str = "") -> None:
    buf.append(s)


def main() -> None:
    w("=" * 74)
    w("一、產生器本身")
    w("=" * 74)
    w(f"  程式檔          : guardian/seed.py")
    w(f"  函式            : seed.build(seed=20260912)")
    w(f"  亂數種子        : 固定 20260912（所以每次產生的結果完全一樣）")
    w(f"  機構名稱字庫    : {len(seed._NAME1)} 個詞")
    w(f"      {seed._NAME1}")
    w(f"  名稱前綴        : {seed._PREFIX}")
    w(f"  行政區          : {len(seed.DISTRICTS)} 區 {list(seed.DISTRICTS)}")
    w(f"  機構名組法      : 新北市 + 區 + 前綴 + 字庫詞 + (幼兒園|托嬰中心)")
    w(f"  機構數          : len(PROFILE_PLAN) = {len(seed.PROFILE_PLAN)}")

    w()
    w("=" * 74)
    w("二、40 筆各自被設計成什麼樣態（刻意埋入的異常）")
    w("=" * 74)
    from collections import Counter
    plan = Counter(seed.PROFILE_PLAN)
    meaning = {
        "clean": "正常，無刻意異常",
        "gap": "收入落差（公告收費推估 vs 決算申報不符）",
        "benford": "財報數字分佈不自然（刻意湊整千、首位均勻）",
        "staff": "師資配置／人事費占比異常偏低",
        "buzz": "社群負評集中",
        "ratio2y": "全園平均合格但 2 歲專班單獨算即違規",
        "compl": "多項法規遵循缺失（超收、未辦團保、車齡逾限…）",
        "multi": "複合型（財務＋輿情＋裁罰同時異常）",
    }
    for k, n in plan.most_common():
        w(f"  {k:<9} {n:>2} 間　{meaning.get(k, '')}")

    w()
    w("=" * 74)
    w("三、哪些欄位是編的")
    w("=" * 74)
    d = seed.build()
    inst = d["institutions"][0]
    w(f"  範例機構：{inst['name']}")
    for key in ("address", "lat", "lng", "capacity", "enrolled", "rating",
                "established", "staff_count", "enrolled_2y", "enrolled_3to5"):
        w(f"      {key:<16} = {inst.get(key)!r}")
    w("  以上全部由亂數產生：")
    w("      地址   = 區名 + 字庫詞 + '路' + 亂數號 + 亂數樓（實際不存在）")
    w("      座標   = 該區中心點 ± 0.012 度亂數位移")
    w("      評鑑   = 依機率抽 優等/甲等/乙等/丙等")
    w("      人數   = 依核定人數乘上亂數比例")

    w()
    w(f"  各表筆數（本次產生）：")
    for k, v in d.items():
        w(f"      {k:<14} {len(v)} 筆")

    w()
    w("  社群貼文文字來源：硬寫的樣板句")
    for t in seed.NEG_TEMPLATES[:3]:
        w(f"      負評樣板：{t}")
    w(f"      （負評 {len(seed.NEG_TEMPLATES)} 句、中性 {len(seed.MILD_TEMPLATES)} 句、"
      f"正評 {len(seed.POS_TEMPLATES)} 句，隨機挑選後掛到機構上）")
    w("  貼文網址：https://example.org/... ← 假網址，點不開")

    w()
    w("  裁罰事由來源：硬寫清單")
    for reason, law, amount, sev in seed.PENALTY_REASONS[:3]:
        w(f"      {reason}（{law}，{amount:,} 元，嚴重度 {sev}）")

    w()
    w("=" * 74)
    w("四、目前資料庫的實際組成")
    w("=" * 74)
    rows = store.q(
        "SELECT COALESCE(dataset,'real') AS d, COUNT(*) AS n"
        " FROM institutions GROUP BY 1 ORDER BY n DESC")
    for r in rows:
        w(f"  dataset={r['d']:<6} {r['n']} 間")

    w()
    w("  各訊號層的資料來源：")
    for table, label in (("financials", "財務明細"), ("fees", "逐園收費公告"),
                         ("penalties", "裁罰紀錄"), ("social_posts", "社群貼文")):
        real = store.q1(
            f"SELECT COUNT(*) n FROM {table} t JOIN institutions i"
            f" ON i.inst_id=t.inst_id WHERE COALESCE(i.dataset,'real')='real'")
        demo = store.q1(
            f"SELECT COUNT(*) n FROM {table} t JOIN institutions i"
            f" ON i.inst_id=t.inst_id WHERE COALESCE(i.dataset,'real')='demo'")
        w(f"      {label:<12} 真實 {real['n']:>6} 筆　示範 {demo['n']:>6} 筆")

    w()
    w("=" * 74)
    w("五、目前排行榜前 15 名是真實還是示範")
    w("=" * 74)
    lb = scoring.leaderboard("新北市", 15)
    w(f"  {'#':<4}{'機構':<28}{'總分':>6}  {'等級':<8}來源")
    w("  " + "-" * 66)
    for r in lb["leaderboard"]:
        tag = "真實文件" if r["dataset"] == "real" else "示範資料(合成)"
        w(f"  {r['rank']:<4}{r['name'].replace('新北市',''):<28}"
          f"{r['total']:>6}  {r['level']:<8}{tag}")
    ds = lb.get("dataset_summary") or {}
    w(f"\n  前 15 名裡：真實 {sum(1 for r in lb['leaderboard'] if r['dataset']=='real')} 間、"
      f"示範 {sum(1 for r in lb['leaderboard'] if r['dataset']=='demo')} 間")

    w()
    w("=" * 74)
    w("六、結論")
    w("=" * 74)
    w("  這 40 筆機構完全是虛構的，名稱、地址、座標、人數、財務、")
    w("  裁罰、社群貼文全部由 guardian/seed.py 以固定亂數種子生成。")
    w("  現實中沒有任何一間幼兒園叫這些名字。")
    w("")
    w("  為什麼會在系統裡：AWS 上那 21 份真實文件只含機構名冊與財務數字，")
    w("  不含裁罰紀錄、逐園收費公告與社群討論。少了這三層，")
    w("  輿情子分數與歷史紀錄子分數會全部是 0，")
    w("  「財務＋輿情雙訊號」等於只剩單訊號，收費交叉比對也無法執行。")
    w("  示範資料的作用是讓五項工具與四個子分數都有東西可跑、可驗證。")
    w("")
    w("  對外報告時必須注意：排行榜上標記「示範資料」的機構不得")
    w("  當成真實稽查結果引用。真正可引用的是標記「真實文件」的那批。")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        import traceback
        buf.append(traceback.format_exc())
    OUT.write_text("\n".join(buf), encoding="utf-8")
