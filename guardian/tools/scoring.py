"""工具 D：風險評分計算工具。

初期：規則式加權評分（透明、可解釋、教育局可自行調權重）
  總分 = 財務異常子分數 × W1 + 輿情異常子分數 × W2 + 歷史紀錄子分數 × W3

後期：以歷史裁罰紀錄為 label 訓練監督式模型（Logistic Regression）
  train_supervised() 已可執行，但樣本量不足時會明確告知，不會假裝模型可用。

另負責：分數落地、與前次分數比較、觸發預警、產出風險排行榜。
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

from .. import config, regulations, store
from . import compliance, forensic, sentiment

RATING_PENALTY = {"優等": 0.0, "甲等": 5.0, "乙等": 25.0, "丙等": 55.0, None: 10.0, "": 10.0}


def history_subscore(inst_id: str) -> dict[str, Any]:
    """歷史紀錄子分數：裁罰次數／嚴重度／時效衰減 + 評鑑等第。"""
    inst = store.get_institution(inst_id) or {}
    pens = store.q(
        "SELECT date, reason, law, amount, severity FROM penalties WHERE inst_id=? ORDER BY date DESC",
        (inst_id,))
    now = datetime.now(timezone.utc)
    score = 0.0
    detail = []
    for p in pens:
        try:
            d = datetime.fromisoformat((p["date"] or "")[:10]).replace(tzinfo=timezone.utc)
            age_years = max(0.0, (now - d).days / 365.25)
        except ValueError:
            age_years = 3.0
        decay = math.exp(-age_years / 2.5)          # 約 2.5 年半衰
        contrib = (p["severity"] or 1) * 15.0 * decay
        score += contrib
        detail.append({"date": p["date"], "reason": p["reason"], "severity": p["severity"],
                       "amount": p["amount"], "decayed_contribution": round(contrib, 1)})

    rating = inst.get("rating")
    rating_pen = RATING_PENALTY.get(rating, 10.0)
    score += rating_pen

    return {
        "inst_id": inst_id,
        "penalty_count": len(pens),
        "rating": rating,
        "rating_penalty": rating_pen,
        "history_subscore": round(min(100.0, score), 1),
        "penalties": detail,
        "note": (f"近年共 {len(pens)} 筆裁罰（依時間衰減加權），最近一次評鑑 {rating or '無資料'}"
                 if pens else f"查無裁罰紀錄，最近一次評鑑 {rating or '無資料'}"),
    }


def score_institution(
    inst_id: str,
    year: int = forensic.CURRENT_YEAR,
    sentiment_days: int = 180,
    allow_live_social: bool = False,
    persist: bool = True,
) -> dict[str, Any]:
    """整合三大子分數，產出可解釋的風險總分。"""
    inst = store.get_institution(inst_id)
    if not inst:
        return {"error": f"找不到機構 {inst_id}"}

    fin = forensic.scan(inst_id, year)
    sen = sentiment.scan(inst_id=inst_id, days=sentiment_days, allow_live=allow_live_social)
    his = history_subscore(inst_id)
    com = compliance.check(inst_id)

    f_sub = float(fin.get("financial_subscore", 0.0))
    s_sub = float(sen.get("sentiment_subscore", 0.0))
    h_sub = float(his.get("history_subscore", 0.0))
    c_sub = float(com.get("compliance_subscore", 0.0))

    w = config.SCORE_WEIGHTS
    total = (f_sub * w["financial"] + c_sub * w["compliance"]
             + s_sub * w["sentiment"] + h_sub * w["history"])
    total = round(min(100.0, max(0.0, total)), 1)
    level, action = config.risk_band(total)
    level, action, floor_reason = config.apply_level_floor(
        level, int(com.get("critical_count", 0)))

    prev = store.q1(
        "SELECT total, scored_at FROM scores WHERE inst_id=? ORDER BY scored_at DESC LIMIT 1",
        (inst_id,))
    delta = round(total - float(prev["total"]), 1) if prev else None

    contributions = {
        "財務異常": round(f_sub * w["financial"], 1),
        "法規遵循": round(c_sub * w["compliance"], 1),
        "社群輿情": round(s_sub * w["sentiment"], 1),
        "歷史紀錄": round(h_sub * w["history"], 1),
    }
    top_driver = max(contributions, key=contributions.get)

    # 法規遵循的重大違規可直接對照罰則，證據力最強，優先列在證據清單最前面
    evidence = [e for e in com.get("evidence", []) if "未發現違規" not in e]
    evidence += list(fin.get("evidence", []))
    for e in sen.get("top_evidence", [])[:3]:
        evidence.append(f"[社群輿情] {e['platform']} {e['posted_at']}"
                        f"（{'、'.join(e['categories']) or '一般負評'}）：{e['quote']}")
    if his["penalty_count"]:
        for p in his["penalties"][:3]:
            evidence.append(f"[歷史裁罰] {p['date']} {p['reason']}（罰款 {p['amount']:,.0f} 元）")

    result = {
        "inst_id": inst_id,
        "institution": inst["name"],
        "inst_type": inst["inst_type"],
        "district": inst["district"],
        "address": inst["address"],
        "lat": inst["lat"], "lng": inst["lng"],
        "scored_at": store.now_iso(),
        "total_score": total,
        "risk_level": level,
        "recommended_action": action,
        "level_floor_reason": floor_reason,
        "directly_actionable": [
            {"indicator": v["indicator"],
             "citation": f"{v['citation']['law']}{v['citation']['article']}",
             "penalty": (f"{v['penalty']['law']}{v['penalty']['article']}"
                         if v.get("penalty") else None)}
            for v in com.get("violations", []) if v["severity"] == 3
        ],
        "subscores": {
            "financial_anomaly": round(f_sub, 1),
            "regulatory_compliance": round(c_sub, 1),
            "social_sentiment": round(s_sub, 1),
            "history_record": round(h_sub, 1),
        },
        "weights": w,
        "weighted_contributions": contributions,
        "primary_driver": top_driver,
        "delta_vs_previous": delta,
        "previous_score": float(prev["total"]) if prev else None,
        "evidence": evidence,
        "signal_scores": fin.get("signal_scores", {}),
        "sentiment_summary": {
            "post_count": sen.get("post_count", 0),
            "negative_count": sen.get("negative_count", 0),
            "negative_last_30d": sen.get("negative_last_30d", 0),
            "category_hits": sen.get("category_hits", {}),
        },
        "history_summary": {"penalty_count": his["penalty_count"], "rating": his["rating"]},
        "compliance_summary": {
            "violation_count": com.get("violation_count", 0),
            "critical_count": com.get("critical_count", 0),
            "top_violations": [v["indicator"] for v in com.get("violations", [])[:5]],
            "unverifiable_count": len(com.get("unverifiable", [])),
        },
    }

    if persist:
        detail = {"forensic": fin, "compliance": com, "sentiment": sen, "history": his,
                  "contributions": contributions}
        store.conn().execute(
            "INSERT INTO scores (inst_id,scored_at,total,financial_sub,sentiment_sub,"
            "history_sub,compliance_sub,level,detail) VALUES (?,?,?,?,?,?,?,?,?)",
            (inst_id, result["scored_at"], total, f_sub, s_sub, h_sub, c_sub, level,
             json.dumps(detail, ensure_ascii=False, default=str)))
        store.conn().commit()
        _maybe_alert(inst_id, inst["name"], total, delta, sen, com)

    return result


def _maybe_alert(inst_id: str, name: str, total: float, delta: float | None,
                 sen: dict[str, Any], com: dict[str, Any] | None = None) -> None:
    alerts: list[tuple[str, str]] = []
    if total >= config.ALERT_TOTAL_THRESHOLD:
        alerts.append(("threshold",
                       f"{name} 風險分數 {total} 已達預警門檻 {config.ALERT_TOTAL_THRESHOLD:.0f}，"
                       f"建議儘速安排稽查"))
    if delta is not None and delta >= config.ALERT_DELTA_THRESHOLD:
        alerts.append(("spike", f"{name} 風險分數較前次上升 {delta:+.1f} 分，請確認變動原因"))
    if sen.get("category_hits", {}).get("身體不當對待"):
        alerts.append(("sentiment",
                       f"{name} 社群出現「身體不當對待」相關指控 "
                       f"{sen['category_hits']['身體不當對待']} 則，建議立即介入查證"))
    for v in (com or {}).get("violations", []):
        if v["severity"] < 3:
            continue
        alerts.append(("compliance",
                       f"{name} 法規遵循重大違規－{v['indicator']}：{v['detail']}"
                       f"（{v['citation']['law']}{v['citation']['article']}）"))
    for kind, msg in alerts:
        store.conn().execute(
            "INSERT INTO alerts (inst_id,created_at,kind,message,total,delta)"
            " VALUES (?,?,?,?,?,?)",
            (inst_id, store.now_iso(), kind, msg, total, delta))
    if alerts:
        store.conn().commit()


def scan_city(
    city: str = "新北市",
    limit: int = 500,
    escalate_threshold: float = 55.0,
    year: int = forensic.CURRENT_YEAR,
) -> dict[str, Any]:
    """全市批次評分。

    escalate_threshold：財務異常子分數達此值時，自動把輿情觀察窗拉長並加做
    前兩位數字 Benford 檢定 —— 這是 Agent「依證據加派工具」策略的批次版實作。
    """
    forensic.clear_peer_cache()
    rows = store.q("SELECT inst_id, name FROM institutions WHERE city=? LIMIT ?", (city, limit))
    results = []
    escalated = []
    for r in rows:
        fin_quick = forensic.scan(r["inst_id"], year)
        deep = fin_quick.get("financial_subscore", 0) >= escalate_threshold
        res = score_institution(
            r["inst_id"], year=year,
            sentiment_days=365 if deep else 180,
            allow_live_social=False, persist=True)
        if deep:
            res["escalated"] = True
            res["escalation_reason"] = (
                f"財務異常子分數 {fin_quick['financial_subscore']} ≥ {escalate_threshold}，"
                "自動擴大輿情搜尋範圍至 365 天並加做前兩位數字 Benford 檢定")
            res["benford_two_digit"] = forensic.benford_test(r["inst_id"], None, 2)
            escalated.append(r["name"])
        results.append(res)

    results.sort(key=lambda x: -x.get("total_score", 0))
    band_count: dict[str, int] = {}
    for r in results:
        band_count[r["risk_level"]] = band_count.get(r["risk_level"], 0) + 1

    return {
        "city": city,
        "scanned": len(results),
        "scanned_at": store.now_iso(),
        "risk_distribution": band_count,
        "escalated_institutions": escalated,
        "top_20": [
            {"rank": i + 1, "inst_id": r["inst_id"], "name": r["institution"],
             "district": r["district"], "total": r["total_score"], "level": r["risk_level"],
             "subscores": r["subscores"], "primary_driver": r["primary_driver"],
             "escalated": r.get("escalated", False)}
            for i, r in enumerate(results[:20])
        ],
    }


def leaderboard(city: str | None = None, top_n: int = 20, min_score: float = 0.0) -> dict[str, Any]:
    """風險排行榜：每間機構取最新一次評分。

    除了分數，另外回傳 data_coverage 標示這個分數背後有多少證據。
    為什麼需要：公校決算書只提供機構名冊（全市層級決算沒有逐園財務），
    這些機構沒有財務與人數欄位，各項檢核全部落在 unverifiable，
    算出來的總分會很低。若只看分數會被讀成「正常、可略過」，
    但實際上是「沒有資料、還沒被稽查過」——這兩件事必須在畫面上分得開。
    """
    rows = store.q(
        """
        SELECT s.inst_id, i.name, i.inst_type, i.district, i.address, i.lat, i.lng,
               s.total, s.financial_sub, s.compliance_sub, s.sentiment_sub,
               s.history_sub, s.level, s.scored_at,
               (SELECT COUNT(*) FROM financials f WHERE f.inst_id = i.inst_id)
                   AS financial_rows,
               (SELECT COUNT(*) FROM social_posts p WHERE p.inst_id = i.inst_id)
                   AS social_rows,
               i.enrolled, i.staff_count, i.sources,
               COALESCE(i.dataset, 'real') AS dataset
        FROM scores s
        JOIN institutions i ON i.inst_id = s.inst_id
        JOIN (SELECT inst_id, MAX(scored_at) mx FROM scores GROUP BY inst_id) latest
             ON latest.inst_id = s.inst_id AND latest.mx = s.scored_at
        WHERE (? IS NULL OR i.city = ?) AND s.total >= ?
        ORDER BY s.total DESC LIMIT ?
        """, (city, city, min_score, top_n))
    for i, r in enumerate(rows):
        r["rank"] = i + 1
        r["total"] = round(r["total"], 1)
        has_fin = (r.get("financial_rows") or 0) > 0
        has_head = r.get("enrolled") is not None and r.get("staff_count") is not None
        present = sum((has_fin, has_head, (r.get("social_rows") or 0) > 0))
        if present == 0:
            coverage, note = "無資料", "僅有機構名冊，無財務與人數欄位，分數不具判讀意義"
        elif present == 3:
            coverage, note = "完整", ""
        else:
            missing = [n for n, ok in (("財務", has_fin), ("人數/人員", has_head),
                                       ("輿情", (r.get("social_rows") or 0) > 0)) if not ok]
            coverage, note = "部分", "缺" + "、".join(missing)
        r["data_coverage"] = coverage
        r["data_coverage_note"] = note
        r["roster_only"] = coverage == "無資料"
    return {"city": city or "全部", "count": len(rows), "leaderboard": rows,
            "coverage_summary": {
                k: sum(1 for r in rows if r["data_coverage"] == k)
                for k in ("完整", "部分", "無資料")},
            "dataset_summary": {
                k: sum(1 for r in rows if r["dataset"] == k)
                for k in ("real", "demo")}}


def alerts(limit: int = 30, include_acked: bool = False) -> dict[str, Any]:
    sql = ("SELECT a.*, i.name, i.district FROM alerts a"
           " LEFT JOIN institutions i ON i.inst_id=a.inst_id")
    if not include_acked:
        sql += " WHERE a.acked=0"
    sql += " ORDER BY a.created_at DESC LIMIT ?"
    return {"alerts": store.q(sql, (limit,))}


def score_history(inst_id: str, limit: int = 12) -> dict[str, Any]:
    rows = store.q(
        "SELECT scored_at,total,financial_sub,sentiment_sub,history_sub,level FROM scores"
        " WHERE inst_id=? ORDER BY scored_at DESC LIMIT ?", (inst_id, limit))
    return {"inst_id": inst_id, "history": rows}


# ------------------------------------------------------------- 監督式模型（後期）
def _features(inst_id: str, year: int = forensic.CURRENT_YEAR) -> list[float] | None:
    """特徵刻意排除裁罰相關欄位，避免與 label 洩漏。

    師生比特徵改為「各年齡層對法定基準的最大超標倍數」，而不是全園平均——
    全園平均會把 2 歲專班的缺口稀釋掉，等於把最有預測力的訊號抹平。
    """
    m = forensic.metrics(inst_id, year)
    if not m or not m.get("income_total"):
        return None
    b = forensic.benford_test(inst_id, None, 1)
    c = forensic.cross_check(inst_id, year)
    inst = store.get_institution(inst_id) or {}
    city = inst.get("city") or ""
    inst_type = inst.get("inst_type", "幼兒園")
    sen = sentiment.scan(inst_id=inst_id, days=365, allow_live=False)
    com = compliance.check(inst_id)

    # 分年齡層取最嚴重的超標倍數（1.0 = 剛好合規）
    worst = 1.0
    if inst_type == "托嬰中心":
        band = regulations.band_for("托嬰中心", "age_under_2", city)
        if m.get("enrolled") and inst.get("staff_count"):
            worst = max(worst, (m["enrolled"] / inst["staff_count"]) / band.staff_threshold)
    else:
        for key, band_key in (("ratio_2y", "age_2_to_3"), ("ratio_3to5", "age_3_to_6")):
            v = m.get(key)
            if v:
                band = regulations.band_for("幼兒園", band_key, city)
                worst = max(worst, v / band.staff_threshold)

    return [
        worst,
        m.get("hr_expense_pct") or 0.55,
        (m.get("avg_monthly_salary") or 35000) / 35000,
        b.get("MAD", 0.0) * 50,
        abs(c.get("gap_pct", 0.0)),
        sen.get("sentiment_subscore", 0.0) / 100.0,
        com.get("compliance_subscore", 0.0) / 100.0,
        float(com.get("critical_count", 0)),
    ]


FEATURE_NAMES = ["年齡層師生比最大超標倍數", "人事費占比", "推估月薪/35k",
                 "Benford MAD×50", "收入落差絕對值", "輿情子分數/100",
                 "法規遵循子分數/100", "重大違規項數"]


def train_supervised(city: str = "新北市", epochs: int = 4000, lr: float = 0.25) -> dict[str, Any]:
    """以歷史裁罰紀錄為 label 訓練 Logistic Regression（純 Python，無額外依賴）。"""
    rows = store.q("SELECT inst_id FROM institutions WHERE city=?", (city,))
    X: list[list[float]] = []
    y: list[float] = []
    ids: list[str] = []
    for r in rows:
        f = _features(r["inst_id"])
        if f is None:
            continue
        label = 1.0 if store.q1(
            "SELECT COUNT(*) n FROM penalties WHERE inst_id=?", (r["inst_id"],))["n"] else 0.0
        X.append(f)
        y.append(label)
        ids.append(r["inst_id"])

    pos, neg = int(sum(y)), int(len(y) - sum(y))
    if pos < 8 or neg < 8:
        return {
            "trained": False,
            "samples": len(y), "positive": pos, "negative": neg,
            "reason": f"標籤樣本不足（正例 {pos}、負例 {neg}，各需 ≥8）。"
                      "現階段仍以規則式加權評分為準，待累積足夠裁罰資料後再啟用模型。",
        }

    # 標準化
    d = len(X[0])
    mean = [sum(x[j] for x in X) / len(X) for j in range(d)]
    std = []
    for j in range(d):
        v = sum((x[j] - mean[j]) ** 2 for x in X) / max(1, len(X) - 1)
        std.append(math.sqrt(v) or 1.0)
    Xs = [[(x[j] - mean[j]) / std[j] for j in range(d)] for x in X]

    wv = [0.0] * d
    b = 0.0
    n = len(Xs)
    l2 = 0.01
    for _ in range(epochs):
        gw = [0.0] * d
        gb = 0.0
        for xi, yi in zip(Xs, y):
            z = b + sum(wv[j] * xi[j] for j in range(d))
            p = 1 / (1 + math.exp(-max(-30, min(30, z))))
            err = p - yi
            for j in range(d):
                gw[j] += err * xi[j]
            gb += err
        for j in range(d):
            wv[j] -= lr * (gw[j] / n + l2 * wv[j])
        b -= lr * gb / n

    def _p(xi: list[float]) -> float:
        z = b + sum(wv[j] * xi[j] for j in range(d))
        return 1 / (1 + math.exp(-max(-30, min(30, z))))

    probs = [_p(xi) for xi in Xs]
    # AUC（Mann–Whitney）
    pos_p = [p for p, yi in zip(probs, y) if yi == 1]
    neg_p = [p for p, yi in zip(probs, y) if yi == 0]
    auc = (sum((1.0 if a > c else 0.5 if a == c else 0.0) for a in pos_p for c in neg_p)
           / (len(pos_p) * len(neg_p))) if pos_p and neg_p else float("nan")
    acc = sum(1 for p, yi in zip(probs, y) if (p >= 0.5) == (yi == 1)) / n

    store.conn().execute(
        "INSERT INTO audit_log (ts,session,step,tool,tool_input,tool_output,latency_ms)"
        " VALUES (?,?,?,?,?,?,?)",
        (store.now_iso(), "train", 0, "train_supervised", city,
         json.dumps({"auc": auc, "acc": acc}, default=str), 0))
    store.conn().commit()

    return {
        "trained": True,
        "model": "LogisticRegression(pure-python, L2)",
        "samples": n, "positive": pos, "negative": neg,
        "train_auc": round(auc, 3), "train_accuracy": round(acc, 3),
        "coefficients": {name: round(c, 3) for name, c in zip(FEATURE_NAMES, wv)},
        "intercept": round(b, 3),
        "caveat": "樣本量小且為訓練集內指標，僅作為導入監督式模型的可行性驗證；"
                  "上線前需以時序切分驗證，並改用 XGBoost 等模型比較。",
    }
