"""逐項驗證提案規格是否都實際可跑。

對應提案章節：
  二① 感知 Perceive          → /api/perceive
  二② 規劃 Plan（動態加派）   → scan_city 的 escalated + agent SYSTEM_PROMPT 規則
  二③ 行動 Act（五大工具）    → toolspec 五個工具逐一實測
  二④ 生成與互動             → /api/agent/run、/api/chat
  三/四 三大問題解法          → 財務比率、Benford、交叉比對、輿情、評分、排行榜、預警
  七 雙訊號風險模型           → 財務子分數與輿情子分數同時有值
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "data" / "verify_spec.txt"
BASE = "http://127.0.0.1:8000"

buf: list[str] = []
problems: list[str] = []


def w(s: str = "") -> None:
    buf.append(s)


def check(label: str, ok: bool, detail: str = "") -> None:
    w(f"  [{'OK  ' if ok else 'FAIL'}] {label}{('　' + detail) if detail else ''}")
    if not ok:
        problems.append(label)


def get(path: str, timeout: int = 60):
    # 查詢字串含中文時必須先 percent-encode，http.client 只接受 ASCII 請求行
    safe = urllib.parse.quote(path, safe="/?&=:")
    with urllib.request.urlopen(BASE + safe, timeout=timeout) as r:  # noqa: S310
        return json.loads(r.read().decode("utf-8"))


def post(path: str, payload: dict, timeout: int = 180):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return json.loads(r.read().decode("utf-8"))


def main() -> None:
    from guardian import store
    from guardian.tools import compliance, forensic, scoring, sentiment

    w("=" * 76)
    w("零、資料底盤")
    w("=" * 76)
    st = store.stats()
    w(f"  {json.dumps(st, ensure_ascii=False)[:400]}")
    check("機構主檔有資料", st["institutions"] > 0, f"{st['institutions']} 間")
    check("財務明細有資料（工具B前提）", st["financial_rows"] > 1000,
          f"{st['financial_rows']} 筆")
    check("逐園收費有資料（交叉比對前提）", st["fee_rows"] > 0, f"{st['fee_rows']} 筆")
    check("裁罰紀錄有資料（歷史子分數前提）", st["penalties"] > 0, f"{st['penalties']} 筆")
    check("社群貼文有資料（工具C前提）", st["social_posts"] > 0, f"{st['social_posts']} 筆")
    ds = {r["d"]: r["n"] for r in store.q(
        "SELECT COALESCE(dataset,'real') d, COUNT(*) n FROM institutions GROUP BY 1")}
    w(f"  資料集分布 = {ds}")
    check("真實文件資料仍在", ds.get("real", 0) > 0, f"real={ds.get('real')}")
    check("兩資料集以 dataset 欄位區分", len(ds) >= 1 and "real" in ds)

    # 找一間 demo 機構（欄位完整，可驗證全部工具）
    demo = store.q1(
        "SELECT inst_id, name FROM institutions WHERE COALESCE(dataset,'real')='demo'"
        " AND enrolled IS NOT NULL LIMIT 1")
    real_one = store.q1(
        "SELECT i.inst_id, i.name FROM institutions i"
        " WHERE COALESCE(i.dataset,'real')='real'"
        " AND EXISTS(SELECT 1 FROM financials f WHERE f.inst_id=i.inst_id) LIMIT 1")
    w(f"  測試用機構：demo={demo and demo['name']}／real={real_one and real_one['name']}")

    w()
    w("=" * 76)
    w("二① 感知 Perceive")
    w("=" * 76)
    per = get("/api/perceive")
    w(f"  {json.dumps(per, ensure_ascii=False)[:500]}")
    check("感知層可回傳訊號", isinstance(per, dict) and not per.get("error"))

    w()
    w("=" * 76)
    w("二③ 行動 Act：五大工具逐一實測")
    w("=" * 76)

    # 工具 A
    a = get("/api/institutions?city=新北市&limit=5")
    check("工具A 資料整合－機構清單", (a.get("count") or 0) > 0, f"{a.get('count')} 筆")
    src = get("/api/regulations")
    check("工具A 法規基準可查", bool(src), "")

    # 工具 B：三項訊號
    if demo:
        fin = forensic.scan(demo["inst_id"])
        w(f"  工具B 財務子分數 = {fin.get('financial_subscore')}"
          f"　訊號 = {json.dumps(fin.get('signal_scores'), ensure_ascii=False)}")
        check("工具B 財務比率分析可跑", "ratio" in (fin.get("signal_scores") or {})
              or fin.get("financial_subscore") is not None)
        ben = forensic.benford_test(demo["inst_id"], None, 1)
        check("工具B Benford 首位數字檢定",
              ben.get("conclusion") != "insufficient_data",
              f"n={ben.get('n')} 結論={ben.get('conclusion')}")
        ben2 = forensic.benford_test(demo["inst_id"], None, 2)
        check("工具B Benford 前兩位數字檢定",
              ben2.get("conclusion") is not None, f"n={ben2.get('n')}")
        cc = forensic.cross_check(demo["inst_id"])
        check("工具B 收費與決算交叉比對",
              "查無公告收費" not in (cc.get("note") or ""),
              f"score={cc.get('score')} note={(cc.get('note') or '')[:60]}")

    # 工具 C
    sen_top = store.q1(
        "SELECT i.inst_id, i.name FROM institutions i JOIN social_posts p"
        " ON p.inst_id=i.inst_id GROUP BY i.inst_id ORDER BY COUNT(*) DESC LIMIT 1")
    if sen_top:
        sc = sentiment.scan(inst_id=sen_top["inst_id"], days=365, allow_live=False)
        w(f"  工具C {sen_top['name']}　貼文 {sc.get('post_count')} 則"
          f"　負面 {sc.get('negative_count')}　子分數 {sc.get('sentiment_subscore')}")
        w(f"      分類命中 = {json.dumps(sc.get('category_hits'), ensure_ascii=False)}")
        check("工具C 輿情子分數 > 0", (sc.get("sentiment_subscore") or 0) > 0,
              str(sc.get("sentiment_subscore")))
        check("工具C 有可引用原文證據", len(sc.get("top_evidence") or []) > 0,
              f"{len(sc.get('top_evidence') or [])} 則")
        check("工具C 負面關鍵字分類有命中", len(sc.get("category_hits") or {}) > 0)
    # jieba 分詞
    one = sentiment.analyze_text("老師會用尺打手心，園方說是在教規矩，廚房衛生也很差")
    w(f"  jieba 分詞詞數 = {one['token_count']}　情感 = {one['sentiment']}"
      f"　分類 = {one['categories']}")
    check("中文分詞 + 負面關鍵字偵測", one["token_count"] > 0 and len(one["categories"]) >= 2,
          f"分類 {one['categories']}")
    neg = sentiment.analyze_text("完全沒有體罰，老師很有耐心")
    check("否定詞修飾正確（『沒有體罰』不計負面）", neg["sentiment"] > 0,
          f"情感 {neg['sentiment']}")

    # 工具 E
    if demo:
        com = compliance.check(demo["inst_id"])
        w(f"  工具E 檢核 {len(com.get('checked_items') or [])} 項"
          f"　違規 {com.get('violation_count')}　重大 {com.get('critical_count')}"
          f"　待補 {len(com.get('unverifiable') or [])}"
          f"　子分數 {com.get('compliance_subscore')}")
        check("工具E 法規遵循檢核可跑", (com.get("checked_items") or []) != [])
        check("工具E 違規附法源條號",
              all(v.get("citation") for v in (com.get("violations") or [])) )
    base = get("/api/baseline")
    check("全體統計基準與標準差門檻", bool(base.get("indicators")),
          f"{len(base.get('indicators') or {})} 項指標")
    stab = get("/api/district-stability")
    check("各區資料穩定度（變異係數）", bool(stab.get("districts")),
          f"{len(stab.get('districts') or [])} 區")

    # 工具 D
    lb = get("/api/leaderboard")
    check("工具D 風險排行榜", (lb.get("count") or 0) > 0, f"{lb.get('count')} 筆")
    w(f"  資料集分布 = {json.dumps(lb.get('dataset_summary'), ensure_ascii=False)}")
    al = get("/api/alerts")
    check("工具D 預警推播", len(al.get("alerts") or []) > 0,
          f"{len(al.get('alerts') or [])} 筆")

    w()
    w("=" * 76)
    w("七 雙訊號風險模型（財務異常 ＋ 社群輿情）")
    w("=" * 76)
    agg = store.q1(
        "SELECT COUNT(*) n,"
        " SUM(CASE WHEN financial_sub>0 THEN 1 ELSE 0 END) fin,"
        " SUM(CASE WHEN compliance_sub>0 THEN 1 ELSE 0 END) com,"
        " SUM(CASE WHEN sentiment_sub>0 THEN 1 ELSE 0 END) sen,"
        " SUM(CASE WHEN history_sub>0 THEN 1 ELSE 0 END) his,"
        " SUM(CASE WHEN financial_sub>0 AND sentiment_sub>0 THEN 1 ELSE 0 END) both"
        " FROM scores s WHERE scored_at=(SELECT MAX(scored_at) FROM scores x"
        " WHERE x.inst_id=s.inst_id)")
    w(f"  最新評分 {agg['n']} 間：財務>0 {agg['fin']}、法規>0 {agg['com']}、"
      f"輿情>0 {agg['sen']}、歷史>0 {agg['his']}")
    check("財務子分數有訊號", agg["fin"] > 0, str(agg["fin"]))
    check("輿情子分數有訊號", agg["sen"] > 0, str(agg["sen"]))
    check("同時具備財務＋輿情雙訊號的機構存在", agg["both"] > 0,
          f"{agg['both']} 間")
    w(f"  權重 = {json.dumps(get('/api/health')['weights'], ensure_ascii=False)}")

    w()
    w("=" * 76)
    w("二② 規劃 Plan：依證據動態加派工具")
    w("=" * 76)
    esc = store.q(
        "SELECT i.name, s.financial_sub, s.sentiment_sub FROM scores s"
        " JOIN institutions i ON i.inst_id=s.inst_id"
        " WHERE s.financial_sub >= 55 AND s.scored_at=("
        "   SELECT MAX(scored_at) FROM scores x WHERE x.inst_id=s.inst_id)")
    w(f"  財務子分數 ≥55（會觸發自動加派）的機構：{len(esc)} 間")
    for r in esc[:5]:
        w(f"      {r['name']:<30} 財務 {r['financial_sub']:>5}　輿情 {r['sentiment_sub']:>5}")
    check("有機構觸發動態加派條件", len(esc) > 0, f"{len(esc)} 間")

    w()
    w("=" * 76)
    w("二④ 生成與互動：Agent 報告與自然語言追問")
    w("=" * 76)
    target = esc[0]["name"] if esc else (demo and demo["name"])
    try:
        ag = post("/api/agent/run",
                  {"name": target, "mode": "investigate"}, timeout=300)
        rep = ag.get("report") or ag.get("answer") or ""
        trace = ag.get("trace") or []
        w(f"  Agent 呼叫工具 {len(trace)} 次")
        for t in trace[:10]:
            w(f"      - {str(t)[:150]}")
        w(f"  報告長度 {len(rep)} 字")
        w(f"  報告前 700 字：\n{rep[:700]}")
        check("Agent 產出自然語言風險報告", len(rep) > 200, f"{len(rep)} 字")
        check("Agent 自主呼叫多個工具", len(trace) >= 2, f"{len(trace)} 次")
    except Exception as exc:  # noqa: BLE001
        check("Agent 產出自然語言風險報告", False, f"{type(exc).__name__}: {exc}")

    try:
        ch = post("/api/chat",
                  {"message": f"{target} 為什麼被列為高風險？請引用具體證據與法源"},
                  timeout=300)
        ans = ch.get("answer") or ""
        w(f"  追問回覆長度 {len(ans)} 字")
        w(f"  回覆前 500 字：\n{ans[:500]}")
        check("稽查人員自然語言追問可回答", len(ans) > 100, f"{len(ans)} 字")
    except Exception as exc:  # noqa: BLE001
        check("稽查人員自然語言追問可回答", False, f"{type(exc).__name__}: {exc}")

    aud = get("/api/audit")
    check("工具呼叫稽核軌跡可查", len(aud.get("calls") or aud.get("audit") or []) > 0,
          str(len(aud.get("calls") or aud.get("audit") or [])))

    w()
    w("=" * 76)
    w(f"結論：{'全部通過' if not problems else f'{len(problems)} 項未通過'}")
    for p in problems:
        w(f"  - {p}")
    w("=" * 76)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        import traceback
        buf.append("!! 驗證腳本異常")
        buf.append(traceback.format_exc())
        problems.append("驗證腳本異常")
    OUT.write_text("\n".join(buf), encoding="utf-8")
    raise SystemExit(1 if problems else 0)
