"""工具 B：鑑識會計異常偵測工具。

三項訊號
  1. 財務比率分析      師生比、人事費占比、收支結構、年增率（與同類型同縣市同業比較）
  2. Benford's Law     決算逐筆金額的首位／前兩位數字分佈檢定（卡方 + MAD）
  3. 交叉比對          公告收費 × 在園人數 推估預期收入 vs 決算申報收入；平均薪資合理性

三項加權後產出「財務異常子分數」（0–100），並附上可引用的證據明細。
定位為低成本初篩，用來排定複核順序，不等於認定違規。
"""
from __future__ import annotations

import math
from functools import lru_cache
from typing import Any

from .. import config, regulations, store

CURRENT_YEAR = 2024
PREV_YEAR = 2023

# Benford 理論分佈
BENFORD_D1 = {d: math.log10(1 + 1 / d) for d in range(1, 10)}
BENFORD_D2 = {d: math.log10(1 + 1 / d) for d in range(10, 100)}

# 業界平均月薪參考區間（教保服務人員），用於人事費合理性檢查
SALARY_LOW = 30000.0
SALARY_FLOOR = 26000.0


# ---------------------------------------------------------------- 統計工具
def _chi2_sf(x: float, k: int) -> float:
    """卡方分佈右尾機率。優先用 scipy，否則用正規化不完全 Gamma 級數展開。"""
    if x <= 0:
        return 1.0
    try:
        from scipy.stats import chi2  # type: ignore

        return float(chi2.sf(x, k))
    except Exception:  # noqa: BLE001
        pass
    a, xx = k / 2.0, x / 2.0
    if xx < a + 1:  # 下不完全 gamma 級數
        term, total, n = 1.0 / a, 1.0 / a, 0
        while n < 500:
            n += 1
            term *= xx / (a + n)
            total += term
            if abs(term) < abs(total) * 1e-14:
                break
        lower = total * math.exp(-xx + a * math.log(xx) - math.lgamma(a))
        return max(0.0, min(1.0, 1.0 - lower))
    # 連分數（Lentz）求上不完全 gamma
    tiny = 1e-300
    b, c, d = xx + 1 - a, 1 / tiny, 1 / (xx + 1 - a)
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = tiny if abs(d) < tiny else d
        c = b + an / c
        c = tiny if abs(c) < tiny else c
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-14:
            break
    return max(0.0, min(1.0, h * math.exp(-xx + a * math.log(xx) - math.lgamma(a))))


def _mean_std(values: list[float]) -> tuple[float, float]:
    vals = [v for v in values if v is not None and math.isfinite(v)]
    if not vals:
        return 0.0, 0.0
    m = sum(vals) / len(vals)
    if len(vals) < 2:
        return m, 0.0
    var = sum((v - m) ** 2 for v in vals) / (len(vals) - 1)
    return m, math.sqrt(var)


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


# ---------------------------------------------------------------- 基礎指標
# 只有 primary（該年度的權威收支表）能用來算合計與比率。
# 真實財報同一年度會有收支餘絀表、功能別、各學年比較表、費用明細等多張報表，
# 全部加總會讓支出變成收入的好幾倍；合計／小計列也必須排除以免重複計算。
TOTAL_ROLES = ("primary",)
# Benford 要的是「逐筆自然發生的金額」，所以明細表要納入、彙總列要排除。
BENFORD_ROLES = ("primary", "detail")


def _role_filter(roles: tuple[str, ...]) -> tuple[str, tuple]:
    """產生 role 條件。role 為 NULL 代表舊資料（示範資料集），一律視為 primary。"""
    marks = ",".join("?" * len(roles))
    return f" AND (role IS NULL OR role IN ({marks}))", roles


def _totals(inst_id: str, year: int) -> dict[str, float]:
    cond, params = _role_filter(TOTAL_ROLES)
    rows = store.q(
        "SELECT flow, subject, SUM(amount) amt FROM financials"
        " WHERE inst_id=? AND year=?" + cond + " GROUP BY flow, subject",
        (inst_id, year) + params)
    out: dict[str, float] = {"income_total": 0.0, "expense_total": 0.0}
    for r in rows:
        key = f"{r['flow']}:{r['subject']}"
        out[key] = float(r["amt"] or 0)
        out["income_total" if r["flow"] == "income" else "expense_total"] += float(r["amt"] or 0)
    return out


def metrics(inst_id: str, year: int = CURRENT_YEAR) -> dict[str, Any]:
    """單一機構單一年度的核心財務比率。"""
    inst = store.get_institution(inst_id)
    if not inst:
        return {}
    t = _totals(inst_id, year)
    enrolled = inst.get("enrolled") or 0
    staff = inst.get("staff_count") or 0
    income = t["income_total"]
    expense = t["expense_total"]
    hr = t.get("expense:人事費", 0.0)

    e2 = inst.get("enrolled_2y") or 0
    e35 = inst.get("enrolled_3to5") or 0
    s2 = inst.get("staff_2y") or 0
    s35 = inst.get("staff_3to5") or 0

    m: dict[str, Any] = {
        "year": year,
        "enrolled": enrolled,
        "staff_count": staff,
        # 全園合併師生比：僅供趨勢觀察，不得用於合規判定（詳見 ratio_analysis 註解）
        "blended_ratio": round(enrolled / staff, 2) if staff else None,
        "student_teacher_ratio": round(enrolled / staff, 2) if staff else None,
        # 依幼照法第16條分年齡層計算，這兩個才是合規判定用的指標
        "enrolled_2y": e2,
        "enrolled_3to5": e35,
        "ratio_2y": round(e2 / s2, 2) if (e2 and s2) else None,
        "ratio_3to5": round(e35 / s35, 2) if (e35 and s35) else None,
        "income_total": round(income, 0),
        "expense_total": round(expense, 0),
        "hr_expense": round(hr, 0),
        "hr_expense_pct": round(hr / expense, 4) if expense else None,
        "surplus_pct": round((income - expense) / income, 4) if income else None,
        "avg_monthly_salary": round(hr / staff / 12, 0) if staff and hr else None,
    }
    for subject, _ in (("學費收入", 0), ("雜費收入", 0), ("代收代辦費收入", 0),
                       ("政府補助收入", 0), ("其他收入", 0)):
        m[f"share_{subject}"] = round(t.get(f"income:{subject}", 0.0) / income, 4) if income else None
    return m


@lru_cache(maxsize=64)
def _peer_stats(inst_type: str, city: str, year: int) -> dict[str, tuple[float, float, int]]:
    """同類型、同縣市的同業平均與標準差。"""
    peers = store.q(
        "SELECT inst_id FROM institutions WHERE inst_type=? AND city=?", (inst_type, city))
    collected: dict[str, list[float]] = {}
    for p in peers:
        m = metrics(p["inst_id"], year)
        for k, v in m.items():
            if isinstance(v, (int, float)) and k not in ("year",):
                collected.setdefault(k, []).append(float(v))
    out: dict[str, tuple[float, float, int]] = {}
    for k, vals in collected.items():
        mean, std = _mean_std(vals)
        out[k] = (mean, std, len(vals))
    return out


def clear_peer_cache() -> None:
    _peer_stats.cache_clear()
    citywide_baseline.cache_clear()


# ---------------------------------------------------------------- 1. 財務比率
def ratio_analysis(inst_id: str, year: int = CURRENT_YEAR) -> dict[str, Any]:
    inst = store.get_institution(inst_id)
    if not inst:
        return {"error": f"找不到機構 {inst_id}"}
    m = metrics(inst_id, year)
    prev = metrics(inst_id, PREV_YEAR)
    peers = _peer_stats(inst["inst_type"], inst["city"], year)

    findings: list[dict[str, Any]] = []
    penalty = 0.0

    # (a) 師生比：依幼照法第16條「分年齡層」計算
    #
    #     2歲專班（2歲以上未滿3歲）實質 1:8、每班上限16人、且不得與其他年齡混齡；
    #     3歲以上至入國民小學前實質 1:15、每班上限30人。
    #     兩者法定基準不同又不得混齡，所以絕對不能用全園平均判定合規——
    #     這是新北市教育局／城鄉發展局訪談明確指出的盲點。
    #     全園合併值仍會算出來，但只標為趨勢觀察用。
    band_specs = []
    if inst["inst_type"] == "托嬰中心":
        band_specs.append(("未滿2歲（托嬰）", m.get("enrolled"), m.get("staff_count"),
                           regulations.band_for("托嬰中心", "age_under_2", inst["city"]),
                           "student_teacher_ratio"))
    else:
        band_specs.append(("2歲專班", m.get("enrolled_2y"), inst.get("staff_2y"),
                           regulations.band_for("幼兒園", "age_2_to_3", inst["city"]),
                           "ratio_2y"))
        band_specs.append(("3歲以上", m.get("enrolled_3to5"), inst.get("staff_3to5"),
                           regulations.band_for("幼兒園", "age_3_to_6", inst["city"]),
                           "ratio_3to5"))

    band_results: list[dict[str, Any]] = []
    for label, children, staff_n, band, metric_key in band_specs:
        if not children or not staff_n:
            if children:
                band_results.append({"band": label, "children": children,
                                     "staff": staff_n, "ratio": None,
                                     "legal_limit": band.staff_threshold,
                                     "status": "資料不足"})
            continue
        ratio = round(children / staff_n, 2)
        legal_band = float(band.staff_threshold)
        over_legal = ratio / legal_band - 1
        peer_mean, peer_std, n = peers.get(metric_key, (0, 0, 0))
        z = (ratio - peer_mean) / peer_std if peer_std else 0.0
        entry = {"band": label, "children": children, "staff": staff_n, "ratio": ratio,
                 "legal_limit": legal_band, "over_legal_pct": round(over_legal, 4),
                 "peer_mean": round(peer_mean, 2), "peer_n": n, "z_score": round(z, 2),
                 "status": "超過法定基準" if over_legal > 0 else "符合法定基準"}
        band_results.append(entry)

        if over_legal > 0:
            sev = _clamp(over_legal * 220, 0, 90)
            penalty += sev
            findings.append({
                "indicator": f"師生比－{label}",
                "value": ratio,
                "legal_limit": legal_band,
                "peer_mean": round(peer_mean, 2),
                "z_score": round(z, 2),
                "severity": round(sev, 1),
                "note": (f"{label}收托 {children} 人、配置 {staff_n} 名教保服務人員，"
                         f"實際 1:{ratio}，超過法定基準 1:{legal_band:.0f} 約 {over_legal:.0%}"
                         f"（{band.citation_staff.cite()}）"),
                "citation": band.citation_staff.to_dict(),
            })
        elif z >= config.SIGMA_BANDS["abnormal"]:
            penalty += 15
            findings.append({
                "indicator": f"師生比－{label}", "value": ratio, "legal_limit": legal_band,
                "peer_mean": round(peer_mean, 2), "z_score": round(z, 2), "severity": 15.0,
                "note": (f"{label} 1:{ratio} 未違反法定基準，但高於同業平均 1:{peer_mean:.1f} 達 "
                         f"{z:.1f} 個標準差（n={n}），列入觀察"),
            })

    # 合併值失真提示：全園平均看起來合格，但某個年齡層已超標
    blended = m.get("blended_ratio")
    over_bands = [b for b in band_results if b.get("over_legal_pct", 0) > 0]
    if blended and over_bands and inst["inst_type"] != "托嬰中心":
        findings.append({
            "indicator": "指標合理性－合併師生比失真",
            "value": blended,
            "severity": 0.0,   # 不重複計分，僅提示計算方式
            "note": (f"全園合併師生比 1:{blended} 看似落在常態範圍，但分年齡層後 "
                     + "、".join(f"{b['band']} 1:{b['ratio']}（法定 1:{b['legal_limit']:.0f}）"
                                 for b in over_bands)
                     + " 已超標。合規判定一律以分年齡層結果為準。"),
        })

    # (b) 人事費占比：偏低可能反映師資配置不足或帳務灌水
    hr_pct = m.get("hr_expense_pct")
    if hr_pct:
        peer_mean, peer_std, n = peers.get("hr_expense_pct", (0, 0, 0))
        z = (hr_pct - peer_mean) / peer_std if peer_std else 0.0
        if z < -1.5 or hr_pct < 0.45:
            sev = _clamp(abs(z) * 22 + max(0.0, (0.45 - hr_pct) * 160), 0, 90)
            penalty += sev
            findings.append({
                "indicator": "人事費用占比",
                "value": round(hr_pct, 4),
                "peer_mean": round(peer_mean, 4),
                "z_score": round(z, 2),
                "severity": round(sev, 1),
                "note": f"人事費僅占總支出 {hr_pct:.1%}，同業平均 {peer_mean:.1%}；"
                        "占比異常偏低可能反映師資配置不足或帳務灌水",
            })

    # (c) 平均薪資合理性
    salary = m.get("avg_monthly_salary")
    if salary:
        if salary < SALARY_FLOOR:
            penalty += 30
            findings.append({"indicator": "平均月薪推估", "value": salary,
                             "severity": 30.0,
                             "note": f"以人事費 ÷ 教保人員數 ÷ 12 推估平均月薪 {salary:,.0f} 元，"
                                     f"低於基本工資水準，疑有人員數或人事費申報不實"})
        elif salary < SALARY_LOW:
            penalty += 12
            findings.append({"indicator": "平均月薪推估", "value": salary, "severity": 12.0,
                             "note": f"推估平均月薪 {salary:,.0f} 元，明顯低於同業行情，建議查核在職名冊"})

    # (d) 收支結構：各項收入占比是否偏離同業
    for subject in ("學費收入", "雜費收入", "代收代辦費收入", "政府補助收入", "其他收入"):
        key = f"share_{subject}"
        v = m.get(key)
        if v is None:
            continue
        peer_mean, peer_std, n = peers.get(key, (0, 0, 0))
        if peer_std and abs(v - peer_mean) / peer_std > 2:
            z = (v - peer_mean) / peer_std
            penalty += 12
            findings.append({"indicator": f"收支結構－{subject}占比", "value": round(v, 4),
                             "peer_mean": round(peer_mean, 4), "z_score": round(z, 2),
                             "severity": 12.0,
                             "note": f"{subject}占總收入 {v:.1%}，偏離同業平均 {peer_mean:.1%} 逾 2 個標準差"})

    # (e) 年增率異常：單年變動超過同業標準差 2 倍
    yoy: list[dict[str, Any]] = []
    for key, label in (("income_total", "總收入"), ("hr_expense_pct", "人事費占比"),
                       ("student_teacher_ratio", "師生比")):
        cur, old = m.get(key), prev.get(key)
        if not cur or not old:
            continue
        change = (cur - old) / old
        peer_mean, peer_std, n = peers.get(key, (0, 0, 0))
        band = (peer_std / peer_mean * 2) if peer_mean else 0.0
        flagged = bool(band and abs(change) > band)
        yoy.append({"indicator": label, "prev": old, "current": cur,
                    "change_pct": round(change, 4),
                    "peer_2sd_band": round(band, 4), "flagged": flagged})
        if flagged:
            penalty += 14
            findings.append({"indicator": f"年增率異常－{label}", "value": round(change, 4),
                             "peer_2sd_band": round(band, 4), "severity": 14.0,
                             "note": f"{label} 一年變動 {change:+.1%}，超過同業標準差 2 倍（±{band:.1%}），列為待複核"})

    return {
        "inst_id": inst_id,
        "institution": inst["name"],
        "year": year,
        "metrics": m,
        "peer_group": f"{inst['city']}{inst['inst_type']}（n={peers.get('income_total', (0,0,0))[2]}）",
        "ratio_by_age_band": band_results,
        "ratio_note": (
            "師生比依幼兒教育及照顧法第16條分年齡層計算：2歲專班實質 1:8（每班上限16人、"
            "不得混齡）、3歲以上實質 1:15（每班上限30人）。全園合併值僅供趨勢觀察，"
            "不作為合規判定依據。"
        ),
        "baseline": baseline_comparison(inst_id, year),
        "yoy": yoy,
        "findings": findings,
        "score": round(_clamp(penalty), 1),
    }


# ---------------------------------------------------------------- 全體統計基準
BASELINE_INDICATORS = [
    ("ratio_2y", "師生比－2歲專班", "低優"),
    ("ratio_3to5", "師生比－3歲以上", "低優"),
    ("blended_ratio", "師生比－全園合併（僅觀察）", "低優"),
    ("hr_expense_pct", "人事費用占總支出比", "高優"),
    ("avg_monthly_salary", "推估平均月薪", "高優"),
    ("surplus_pct", "結餘率", "中性"),
    ("share_學費收入", "學費收入占比", "中性"),
    ("share_政府補助收入", "政府補助收入占比", "中性"),
]


@lru_cache(maxsize=32)
def citywide_baseline(city: str, year: int = CURRENT_YEAR) -> dict[str, Any]:
    """全體統計基準。

    教育局／城鄉發展局訪談結論：指標合理性應「參考全體統計數據並設定標準差
    作為判斷異常的依據」。因此這裡以全市所有機構為母體算出每項指標的
    平均值、標準差與分位數，並換算成 1σ／2σ／3σ 三段門檻。
    """
    rows = store.q("SELECT inst_id, inst_type, district FROM institutions WHERE city=?", (city,))
    collected: dict[str, list[float]] = {}
    for r in rows:
        m = metrics(r["inst_id"], year)
        for key, _, _ in BASELINE_INDICATORS:
            v = m.get(key)
            if isinstance(v, (int, float)):
                collected.setdefault(key, []).append(float(v))

    out: dict[str, Any] = {}
    for key, label, direction in BASELINE_INDICATORS:
        vals = sorted(collected.get(key, []))
        if len(vals) < 3:
            continue
        mean, std = _mean_std(vals)

        def pct(p: float) -> float:
            idx = min(len(vals) - 1, max(0, int(round(p * (len(vals) - 1)))))
            return round(vals[idx], 4)

        out[key] = {
            "label": label,
            "direction": direction,
            "n": len(vals),
            "mean": round(mean, 4),
            "std": round(std, 4),
            "cv": round(std / mean, 4) if mean else None,
            "min": round(vals[0], 4),
            "p25": pct(0.25), "median": pct(0.5), "p75": pct(0.75),
            "max": round(vals[-1], 4),
            "thresholds": {
                f"{name}（{k}σ）": {
                    "upper": round(mean + k * std, 4),
                    "lower": round(mean - k * std, 4),
                }
                for name, k in (("觀察", config.SIGMA_BANDS["observe"]),
                                ("異常", config.SIGMA_BANDS["abnormal"]),
                                ("重大偏離", config.SIGMA_BANDS["critical"]))
            },
        }
    return {
        "city": city, "year": year, "population": len(rows),
        "sigma_bands": config.SIGMA_BANDS,
        "indicators": out,
        "note": ("以全市所有機構為母體之全體統計；異常判定門檻為平均值 ± k 個標準差，"
                 f"k 依序為 {config.SIGMA_BANDS['observe']}（觀察）／"
                 f"{config.SIGMA_BANDS['abnormal']}（異常）／"
                 f"{config.SIGMA_BANDS['critical']}（重大偏離）。"),
    }


def sigma_level(value: float, mean: float, std: float) -> tuple[float, str]:
    """回傳 (z 分數, 分級標籤)。"""
    if not std:
        return 0.0, "無法判定（母體標準差為 0）"
    z = (value - mean) / std
    a = abs(z)
    if a >= config.SIGMA_BANDS["critical"]:
        return z, "重大偏離"
    if a >= config.SIGMA_BANDS["abnormal"]:
        return z, "異常"
    if a >= config.SIGMA_BANDS["observe"]:
        return z, "觀察"
    return z, "常態範圍"


def baseline_comparison(inst_id: str, year: int = CURRENT_YEAR) -> dict[str, Any]:
    """把單一機構的指標放到全市全體統計上定位。"""
    inst = store.get_institution(inst_id)
    if not inst:
        return {}
    base = citywide_baseline(inst["city"], year)
    m = metrics(inst_id, year)
    rows = []
    for key, stat in base.get("indicators", {}).items():
        v = m.get(key)
        if not isinstance(v, (int, float)):
            continue
        z, level = sigma_level(float(v), stat["mean"], stat["std"])
        rows.append({
            "indicator": stat["label"], "key": key, "value": round(float(v), 4),
            "citywide_mean": stat["mean"], "citywide_std": stat["std"],
            "n": stat["n"], "z_score": round(z, 2), "level": level,
        })
    rows.sort(key=lambda r: -abs(r["z_score"]))
    return {
        "population": base.get("population"),
        "sigma_bands": config.SIGMA_BANDS,
        "comparisons": rows,
        "beyond_2sd": [r["indicator"] for r in rows
                       if abs(r["z_score"]) >= config.SIGMA_BANDS["abnormal"]],
    }


def district_stability(city: str = "新北市", year: int = CURRENT_YEAR) -> dict[str, Any]:
    """區域穩定度分析。

    訪談結論：不同區域可能存在數據波動，但理想上應維持在穩定範圍內。
    因此除了比較各區平均值與全市平均，另以變異係數（CV）衡量各區內部的離散程度，
    超過門檻即代表該區的資料本身不穩定，用它當比較基準要格外小心。
    """
    base = citywide_baseline(city, year)
    rows = store.q("SELECT inst_id, district FROM institutions WHERE city=?", (city,))
    by_district: dict[str, dict[str, list[float]]] = {}
    for r in rows:
        m = metrics(r["inst_id"], year)
        d = r["district"] or "未分區"
        for key, _, _ in BASELINE_INDICATORS:
            v = m.get(key)
            if isinstance(v, (int, float)):
                by_district.setdefault(d, {}).setdefault(key, []).append(float(v))

    districts: list[dict[str, Any]] = []
    for d, metrics_map in sorted(by_district.items()):
        entry: dict[str, Any] = {"district": d, "indicators": {}}
        unstable: list[str] = []
        n_inst = len(metrics_map.get("blended_ratio", []))
        underpowered = n_inst < config.DISTRICT_MIN_N

        for key, vals in metrics_map.items():
            stat = base["indicators"].get(key)
            if not stat or len(vals) < 2:
                continue
            mean, std = _mean_std(vals)
            z_of_mean, level = sigma_level(mean, stat["mean"], stat["std"])

            # 變異係數在「平均值接近 0」時會失真（例如結餘率），
            # 此時改用「區內標準差 ÷ 全市標準差」作為離散度指標。
            city_std = stat["std"] or 0.0
            cv_reliable = bool(mean) and abs(mean) > (city_std * 0.5)
            if cv_reliable:
                dispersion = std / mean
                measure = "變異係數（區內std/區內mean）"
                threshold = config.DISTRICT_CV_STABLE_MAX
            else:
                dispersion = (std / city_std) if city_std else 0.0
                measure = "離散比（區內std/全市std）"
                threshold = config.DISTRICT_DISPERSION_RATIO_MAX

            stable = abs(dispersion) <= threshold
            if not stable and not underpowered:
                unstable.append(stat["label"])
            entry["indicators"][stat["label"]] = {
                "n": len(vals),
                "district_mean": round(mean, 4),
                "district_std": round(std, 4),
                "dispersion": round(dispersion, 4),
                "dispersion_measure": measure,
                "dispersion_threshold": threshold,
                "citywide_mean": stat["mean"],
                "mean_z_vs_city": round(z_of_mean, 2),
                "mean_level": level,
                "within_stable_range": stable,
            }

        entry["n_institutions"] = n_inst
        entry["underpowered"] = underpowered
        entry["unstable_indicators"] = unstable
        if underpowered:
            entry["assessment"] = (
                f"樣本過小（{n_inst} 間 < {config.DISTRICT_MIN_N} 間），"
                "統計量不穩定，不宜單獨作為比較基準；請改用全市全體統計")
        elif unstable:
            entry["assessment"] = f"區內資料離散偏大：{'、'.join(unstable)}"
        else:
            entry["assessment"] = "區內資料穩定，可作為比較基準"
        districts.append(entry)

    underpowered_districts = [d["district"] for d in districts if d["underpowered"]]
    return {
        "city": city, "year": year,
        "min_institutions_for_baseline": config.DISTRICT_MIN_N,
        "cv_stable_threshold": config.DISTRICT_CV_STABLE_MAX,
        "dispersion_ratio_threshold": config.DISTRICT_DISPERSION_RATIO_MAX,
        "citywide_baseline": {k: {"mean": v["mean"], "std": v["std"], "n": v["n"]}
                              for k, v in base.get("indicators", {}).items()},
        "districts": districts,
        "underpowered_districts": underpowered_districts,
        "recommendation": (
            f"目前 {len(underpowered_districts)}/{len(districts)} 個區的機構數低於 "
            f"{config.DISTRICT_MIN_N} 間，區級平均值本身不穩定。"
            "建議異常判定以全市全體統計為主基準，區級數據僅作為輔助說明；"
            "待各區樣本累積足夠後再改以區級基準比較。"
            if underpowered_districts else
            "各區樣本量足夠，可用區級基準比較。"),
        "note": ("各區平均值以全市全體統計的標準差定位（mean_z_vs_city）。"
                 f"離散度門檻：變異係數 {config.DISTRICT_CV_STABLE_MAX}；"
                 f"平均值接近 0 的指標改用離散比 {config.DISTRICT_DISPERSION_RATIO_MAX}。"),
    }


# ---------------------------------------------------------------- 2. Benford
def benford_test(inst_id: str, year: int | None = None, digits: int = 1) -> dict[str, Any]:
    """對決算逐筆金額做首位／前兩位數字分佈檢定。"""
    cond, role_params = _role_filter(BENFORD_ROLES)
    sql = "SELECT amount FROM financials WHERE inst_id=? AND amount >= 10" + cond
    params: tuple = (inst_id,) + role_params
    if year:
        sql += " AND year=?"
        params += (year,)
    amounts = [abs(float(r["amount"])) for r in store.q(sql, params)]
    n = len(amounts)
    if n < 25:
        return {"inst_id": inst_id, "n": n, "score": 0.0, "conclusion": "insufficient_data",
                "note": f"僅 {n} 筆金額，樣本不足（建議 ≥ 25 筆），不做 Benford 判讀"}

    expected_map = BENFORD_D1 if digits == 1 else BENFORD_D2
    observed: dict[int, int] = {d: 0 for d in expected_map}
    for a in amounts:
        s = f"{a:.10f}".replace(".", "").lstrip("0")
        if len(s) < digits:
            continue
        try:
            d = int(s[:digits])
        except ValueError:
            continue
        if d in observed:
            observed[d] += 1

    total = sum(observed.values())
    if total < 25:
        return {"inst_id": inst_id, "n": total, "score": 0.0, "conclusion": "insufficient_data"}

    chi2_stat = 0.0
    abs_dev = 0.0
    table = []
    for d, exp_p in expected_map.items():
        exp_c = exp_p * total
        obs_c = observed[d]
        if exp_c > 0:
            chi2_stat += (obs_c - exp_c) ** 2 / exp_c
        obs_p = obs_c / total
        abs_dev += abs(obs_p - exp_p)
        table.append({"digit": d, "observed": obs_c,
                      "observed_pct": round(obs_p, 4), "expected_pct": round(exp_p, 4),
                      "diff_pct": round(obs_p - exp_p, 4)})
    dof = len(expected_map) - 1
    p_value = _chi2_sf(chi2_stat, dof)
    mad = abs_dev / len(expected_map)

    # Nigrini 的 MAD 判讀基準（首位數字）
    if digits == 1:
        if mad <= 0.006:
            conclusion, base = "close_conformity", 0.0
        elif mad <= 0.012:
            conclusion, base = "acceptable_conformity", 25.0
        elif mad <= 0.015:
            conclusion, base = "marginal_conformity", 55.0
        else:
            conclusion, base = "nonconformity", 78.0
        extra = _clamp((mad - 0.015) * 1200, 0, 22) if mad > 0.015 else 0.0
    else:
        if mad <= 0.0012:
            conclusion, base = "close_conformity", 0.0
        elif mad <= 0.0018:
            conclusion, base = "acceptable_conformity", 25.0
        elif mad <= 0.0022:
            conclusion, base = "marginal_conformity", 55.0
        else:
            conclusion, base = "nonconformity", 78.0
        extra = _clamp((mad - 0.0022) * 9000, 0, 22) if mad > 0.0022 else 0.0

    score = _clamp(base + extra + (8.0 if p_value < 0.01 else 0.0))

    top_dev = sorted(table, key=lambda r: -abs(r["diff_pct"]))[:3]
    return {
        "inst_id": inst_id,
        "year": year or "all",
        "digits": digits,
        "n": total,
        "chi_square": round(chi2_stat, 2),
        "dof": dof,
        "p_value": round(p_value, 6),
        "MAD": round(mad, 6),
        "conclusion": conclusion,
        "score": round(score, 1),
        "distribution": table,
        "largest_deviations": top_dev,
        "note": (
            f"{total} 筆金額首位數字分佈的 MAD={mad:.4f}（Nigrini 基準：≤0.006 高度符合、>0.015 不符合），"
            f"卡方={chi2_stat:.1f}, p={p_value:.4g}。"
            + ("分佈明顯偏離自然規律，數字有人為調整的可能，建議優先複核原始憑證。"
               if score >= 55 else "分佈與自然規律無明顯衝突。")
            + " 此為初篩指標，不足以單獨認定違規。"
        ),
    }


# ---------------------------------------------------------------- 3. 交叉比對
def cross_check(inst_id: str, year: int = CURRENT_YEAR) -> dict[str, Any]:
    inst = store.get_institution(inst_id)
    if not inst:
        return {"error": f"找不到機構 {inst_id}"}
    fee_rows = store.q(
        "SELECT item, amount, period FROM fees WHERE inst_id=? AND year<=? ORDER BY year DESC",
        (inst_id, year))
    if not fee_rows:
        return {"inst_id": inst_id, "score": 0.0, "note": "查無公告收費資料，無法交叉比對"}

    seen: dict[str, dict[str, Any]] = {}
    for r in fee_rows:
        seen.setdefault(r["item"], r)
    per_semester = sum(float(r["amount"]) for r in seen.values()
                       if (r["period"] or "每學期") == "每學期")
    per_month = sum(float(r["amount"]) for r in seen.values() if r["period"] == "每月")
    enrolled = inst.get("enrolled") or 0
    expected = (per_semester * 2 + per_month * 12) * enrolled

    t = _totals(inst_id, year)
    reported = sum(t.get(f"income:{s}", 0.0)
                   for s in ("學費收入", "雜費收入", "代收代辦費收入"))

    findings: list[dict[str, Any]] = []
    score = 0.0
    gap = (expected - reported) / expected if expected else 0.0
    if expected > 0:
        if abs(gap) >= config.REVENUE_GAP_HIGH:
            score += _clamp(abs(gap) * 200, 0, 85)
            findings.append({
                "indicator": "收入落差異常",
                "expected_revenue": round(expected, 0),
                "reported_revenue": round(reported, 0),
                "gap_pct": round(gap, 4),
                "severity": "高",
                "note": f"以公告收費（每學期合計 {per_semester:,.0f} 元）× 在園幼兒 {enrolled} 人 × 2 學期"
                        f"推估預期學雜費收入 {expected:,.0f} 元，決算申報 {reported:,.0f} 元，"
                        f"差異 {gap:+.1%}，超過 {config.REVENUE_GAP_HIGH:.0%} 門檻",
            })
        elif abs(gap) >= config.REVENUE_GAP_WARN:
            score += _clamp(abs(gap) * 150, 0, 45)
            findings.append({
                "indicator": "收入落差待確認",
                "expected_revenue": round(expected, 0),
                "reported_revenue": round(reported, 0),
                "gap_pct": round(gap, 4),
                "severity": "中",
                "note": f"預期 {expected:,.0f} 元 vs 申報 {reported:,.0f} 元，差異 {gap:+.1%}，"
                        "可能來自減免、中途入退園或申報不完整，建議調閱收費收據核對",
            })

    # 人事費 vs 教保人員數的合理性
    m = metrics(inst_id, year)
    salary = m.get("avg_monthly_salary")
    if salary and salary < SALARY_LOW:
        score += 25 if salary < SALARY_FLOOR else 12
        findings.append({
            "indicator": "人事費與人員數不相稱",
            "avg_monthly_salary": salary,
            "staff_count": m.get("staff_count"),
            "severity": "高" if salary < SALARY_FLOOR else "中",
            "note": f"申報人事費 {m.get('hr_expense'):,.0f} 元 ÷ {m.get('staff_count')} 人 ÷ 12 個月 = "
                    f"平均月薪 {salary:,.0f} 元，與同業行情不符",
        })

    return {
        "inst_id": inst_id,
        "institution": inst["name"],
        "year": year,
        "fee_schedule": [{"item": k, "amount": v["amount"], "period": v["period"]}
                         for k, v in seen.items()],
        "enrolled": enrolled,
        "expected_revenue": round(expected, 0),
        "reported_revenue": round(reported, 0),
        "gap_pct": round(gap, 4),
        "findings": findings,
        "score": round(_clamp(score), 1),
    }


# ---------------------------------------------------------------- 整合
def scan(inst_id: str, year: int = CURRENT_YEAR, include_two_digit: bool = False) -> dict[str, Any]:
    """跑完三項訊號並加權為財務異常子分數。"""
    inst = store.get_institution(inst_id)
    if not inst:
        return {"error": f"找不到機構 {inst_id}"}

    r = ratio_analysis(inst_id, year)
    b = benford_test(inst_id, None, 1)
    b2 = benford_test(inst_id, None, 2) if include_two_digit else None
    c = cross_check(inst_id, year)

    w = config.FORENSIC_WEIGHTS
    sub = (r.get("score", 0) * w["ratio"] + b.get("score", 0) * w["benford"]
           + c.get("score", 0) * w["cross"])

    evidence: list[str] = []
    for f in r.get("findings", []):
        evidence.append(f"[財務比率] {f['indicator']}：{f['note']}")
    if b.get("score", 0) >= 55:
        evidence.append(f"[Benford's Law] {b['note']}")
    for f in c.get("findings", []):
        evidence.append(f"[交叉比對] {f['indicator']}：{f['note']}")
    if not evidence:
        evidence.append("三項鑑識會計訊號均未達異常門檻。")

    return {
        "inst_id": inst_id,
        "institution": inst["name"],
        "inst_type": inst["inst_type"],
        "district": inst["district"],
        "year": year,
        "financial_subscore": round(_clamp(sub), 1),
        "weights": w,
        "signals": {
            "ratio_analysis": r,
            "benford_first_digit": b,
            **({"benford_first_two_digits": b2} if b2 else {}),
            "cross_check": c,
        },
        "signal_scores": {"ratio": r.get("score", 0), "benford": b.get("score", 0),
                          "cross_check": c.get("score", 0)},
        "evidence": evidence,
    }
