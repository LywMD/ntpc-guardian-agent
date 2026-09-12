"""端到端煙霧測試：驗證四大工具、評分、感知層與 API 是否都真的可用。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 用獨立的資料庫。這支測試驗證的是規則引擎在「固定示範資料集」上的行為，
# 必須與載入真實資料的 data/guardian.db 隔離，否則兩者會互相干擾。
if not os.environ.get("GUARDIAN_DB"):
    os.environ["GUARDIAN_DB"] = str(ROOT / "data" / "smoke_test.db")

from guardian import agent as agent_mod  # noqa: E402
from guardian import config, regulations, store  # noqa: E402
from guardian.tools import compliance, etl, forensic, scoring, sentiment  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'[PASS]' if cond else '[FAIL]'} {name}{'  ' + detail if detail else ''}")
    if not cond:
        FAILED.append(name)


def main() -> int:
    print("=" * 72)
    print("小小守護員　端到端煙霧測試")
    print("=" * 72)

    print("\n[1] 工具 A：資料整合 ETL")
    print(f"  測試資料庫：{store.stats()['db_path']}")
    if store.stats()["institutions"] == 0:
        etl.refresh("新北市", allow_live=False, use_real=False)
    st = store.stats()
    check("機構主檔已建立", st["institutions"] >= 30, f"{st['institutions']} 間")
    check("決算逐筆明細足夠做 Benford", st["financial_rows"] >= 1000, f"{st['financial_rows']} 筆")
    check("收費公告已整合", st["fee_rows"] > 0, f"{st['fee_rows']} 筆")
    check("社群貼文已整合", st["social_posts"] > 0, f"{st['social_posts']} 筆")
    check("資料來源模式有記錄", st["data_source_mode"] in ("live", "seed"),
          st["data_source_mode"])

    # 重跑 ETL 不應該讓資料重複累加（統一 ID + 去重條件要真的有效）
    before = (st["institutions"], st["fee_rows"], st["financial_rows"],
              st["penalties"], st["social_posts"])
    etl.refresh("新北市", allow_live=False, use_real=False)
    st2 = store.stats()
    after = (st2["institutions"], st2["fee_rows"], st2["financial_rows"],
             st2["penalties"], st2["social_posts"])
    check("ETL 具備幂等性（重跑不重複累加）", before == after, f"{before} → {after}")

    hits = store.find_institutions("青草地", limit=1)
    check("模糊比對可用關鍵字找到機構", bool(hits))
    if not hits:
        return 1
    iid = hits[0]["inst_id"]
    check("統一機構 ID 格式正確", iid.startswith("INST-"), iid)

    print("\n[2] 工具 B：鑑識會計")
    f = forensic.scan(iid, include_two_digit=True)
    check("財務異常子分數在 0-100", 0 <= f["financial_subscore"] <= 100,
          str(f["financial_subscore"]))
    ra = f["signals"]["ratio_analysis"]
    check("師生比有算出來", ra["metrics"].get("student_teacher_ratio") is not None,
          f"1:{ra['metrics'].get('student_teacher_ratio')}")
    check("人事費占比有算出來", ra["metrics"].get("hr_expense_pct") is not None)
    check("有同業母體可比較", "n=" in ra["peer_group"], ra["peer_group"])
    check("有偵測到比率異常", len(ra["findings"]) > 0, f"{len(ra['findings'])} 項")
    check("有年增率比較", len(ra["yoy"]) > 0, f"{len(ra['yoy'])} 項")

    b1 = f["signals"]["benford_first_digit"]
    check("Benford 首位樣本數足夠", b1["n"] >= 25, f"n={b1['n']}")
    check("Benford 有卡方與 p 值", "chi_square" in b1 and "p_value" in b1,
          f"χ²={b1['chi_square']}, p={b1['p_value']}")
    check("Benford 有 MAD 與結論", b1["MAD"] > 0 and b1["conclusion"] != "",
          f"MAD={b1['MAD']} → {b1['conclusion']}")
    check("Benford 分佈表為 9 個首位數字", len(b1["distribution"]) == 9)
    b2 = f["signals"].get("benford_first_two_digits")
    check("前兩位數字檢定可執行", bool(b2) and len(b2["distribution"]) == 90)

    c = f["signals"]["cross_check"]
    check("交叉比對有推估預期收入", c["expected_revenue"] > 0,
          f"{c['expected_revenue']:,.0f}")
    check("交叉比對有落差百分比", "gap_pct" in c, f"{c['gap_pct']:+.1%}")
    check("財務證據可引用", len(f["evidence"]) > 0, f"{len(f['evidence'])} 條")

    # 卡方檢定備援實作與 scipy 一致性
    try:
        from scipy.stats import chi2 as _chi2

        mine = forensic._chi2_sf(15.51, 8)
        theirs = float(_chi2.sf(15.51, 8))
        check("卡方備援實作與 scipy 一致", abs(mine - theirs) < 1e-9,
              f"{mine:.10f} vs {theirs:.10f}")
    except ImportError:
        print("  [SKIP] scipy 未安裝，跳過卡方一致性比對")

    print("\n[3] 工具 C：NLP 輿情")
    a = sentiment.analyze_text("老師會用尺打手心，非常不當管教，環境衛生也很差")
    check("負面關鍵字有命中", len(a["neg_keywords"]) >= 2, str(a["categories"]))
    check("情感判為負面", a["label"] == "負面", f"sentiment={a['sentiment']}")
    neg = sentiment.analyze_text("園方說明從來沒有體罰，家長都很放心")
    check("否定詞可避免誤判（沒有體罰）", "體罰" not in
          [h["keyword"] for h in neg["neg_keywords"]], f"label={neg['label']}")

    s = sentiment.scan(inst_id=iid, days=365, allow_live=False)
    check("輿情子分數在 0-100", 0 <= s["sentiment_subscore"] <= 100,
          str(s["sentiment_subscore"]))
    check("有分類命中統計", isinstance(s["category_hits"], dict))
    check("有可引用原文", len(s["top_evidence"]) > 0, f"{len(s['top_evidence'])} 則")
    check("有分數拆解可解釋", "score_breakdown" in s,
          json.dumps(s.get("score_breakdown", {}), ensure_ascii=False))

    print("\n[3b] 法規基準模組（regulations.py）")
    b2 = regulations.BAND_2Y
    b35 = regulations.BAND_3TO5
    check("2歲專班法定基準為 1:8", b2.staff_threshold == 8, f"1:{b2.staff_threshold}")
    check("2歲專班每班上限 16 人", b2.class_size_limit == 16)
    check("2歲專班標記不得混齡", b2.no_mixed_age is True)
    check("3歲以上法定基準為 1:15", b35.staff_threshold == 15, f"1:{b35.staff_threshold}")
    check("3歲以上每班上限 30 人", b35.class_size_limit == 30)
    check("托嬰中心法定基準為 1:5",
          regulations.BAND_INFANT.staff_threshold == 5)
    # min_staff = max(班級數, ceil(人數/門檻))
    check("18 人 2歲專班應置 3 人", b2.min_staff(18) == 3, str(b2.min_staff(18)))
    check("班級數多於比例需求時以班級數為準", b2.min_staff(8, classes=3) == 3,
          str(b2.min_staff(8, classes=3)))
    check("16 人 3歲以上班級應置 2 人", b35.min_staff(16) == 2, str(b35.min_staff(16)))
    check("每個年齡層規則都有法源引用",
          all(bd.citation_staff.pcode for bd in regulations.BANDS_BY_KEY.values()))
    check("助理教保員上限為三分之一",
          abs(regulations.ASSISTANT_MAX_SHARE - 1 / 3) < 1e-9)
    check("護理人員 201 人以上須專任",
          regulations.required_nurse_types(201)[0] == ["專任"],
          str(regulations.required_nurse_types(201)[0]))
    check("護理人員 200 人可兼任",
          "兼任" in regulations.required_nurse_types(200)[0])

    # 地方自治法規只能從嚴
    regulations.LOCAL_OVERRIDES["測試市"] = {"age_2_to_3": {"staff_threshold": 7}}
    strict = regulations.band_for("幼兒園", "age_2_to_3", "測試市")
    check("地方從嚴覆寫生效（1:8 → 1:7）", strict.staff_threshold == 7,
          f"1:{strict.staff_threshold}")
    regulations.LOCAL_OVERRIDES["測試市"] = {"age_2_to_3": {"staff_threshold": 12}}
    loose = regulations.band_for("幼兒園", "age_2_to_3", "測試市")
    check("地方放寬覆寫被擋下（維持 1:8）", loose.staff_threshold == 8,
          f"1:{loose.staff_threshold}")
    regulations.LOCAL_OVERRIDES.pop("測試市", None)

    print("\n[3c] 工具 E：法規遵循檢核")
    cc = compliance.check(iid)
    check("法規遵循子分數在 0-100", 0 <= cc["compliance_subscore"] <= 100,
          str(cc["compliance_subscore"]))
    check("有檢核項目清單", len(cc["checked_items"]) >= 8,
          f"{len(cc['checked_items'])} 項")
    check("每項違規都有法源條號",
          all(v["citation"]["article"] for v in cc["violations"]),
          f"{cc['violation_count']} 項違規")
    check("有分數計算拆解", "score_breakdown" in cc)

    city_c = compliance.scan_city("新北市")
    check("全市法規遵循掃描可執行", city_c["scanned"] >= 30, f"{city_c['scanned']} 間")
    check("有偵測到違規機構", city_c["with_violations"] > 0,
          f"{city_c['with_violations']} 間")
    check("有偵測到重大違規", city_c["with_critical"] > 0, f"{city_c['with_critical']} 間")

    # 核心驗證：合併看合格、分開看違規（訪談指出的盲點）
    masking = [x for x in city_c["results"]
               if "合併計算掩蓋年齡層人力缺口" in x["top_violations"]]
    check("抓到「合併師生比掩蓋年齡層缺口」的案例", len(masking) > 0,
          f"{len(masking)} 間：" + "、".join(m["name"].replace("新北市", "")
                                          for m in masking[:3]))
    if masking:
        detail = compliance.check(masking[0]["inst_id"])
        blended_v = next((v for v in detail["violations"]
                          if v["code"] == "BLENDED_RATIO_MASKING"), None)
        band_v = [v for v in detail["violations"] if v["code"].startswith("STAFF_RATIO_")]
        check("該案例確實同時有年齡層違規", bool(band_v),
              band_v[0]["indicator"] if band_v else "")
        check("該案例的合併值確實看似合格", bool(blended_v),
              blended_v["observed"] if blended_v else "")

    # 師生比必須分年齡層出現在鑑識會計結果中
    ra = f["signals"]["ratio_analysis"]
    bands = ra.get("ratio_by_age_band", [])
    check("鑑識會計回傳分年齡層師生比", len(bands) >= 1,
          "、".join(f"{b['band']} 1:{b['ratio']}" for b in bands))
    check("每個年齡層都帶法定基準", all("legal_limit" in b for b in bands))
    check("有明確標示合併值不作為合規依據", "不作為合規判定依據" in ra.get("ratio_note", ""))

    print("\n[3d] 全體統計基準與區域穩定度")
    base = forensic.citywide_baseline("新北市")
    check("全體統計以全市為母體", base["population"] >= 30, f"{base['population']} 間")
    check("有多項指標的統計量", len(base["indicators"]) >= 5,
          f"{len(base['indicators'])} 項")
    check("每項指標都有 1σ/2σ/3σ 門檻",
          all(len(s["thresholds"]) == 3 for s in base["indicators"].values()))
    check("σ 門檻可由設定調整", base["sigma_bands"] == config.SIGMA_BANDS,
          str(base["sigma_bands"]))
    z, level = forensic.sigma_level(10.0, 5.0, 2.0)   # z = 2.5
    check("2.5σ 判為異常", level == "異常", f"z={z} → {level}")
    z, level = forensic.sigma_level(11.5, 5.0, 2.0)   # z = 3.25
    check("3.25σ 判為重大偏離", level == "重大偏離", f"z={z} → {level}")

    bc = forensic.baseline_comparison(iid)
    check("單一機構可在全體統計上定位", len(bc["comparisons"]) >= 3,
          f"{len(bc['comparisons'])} 項指標")

    ds = forensic.district_stability("新北市")
    check("區域穩定度可執行", len(ds["districts"]) >= 5, f"{len(ds['districts'])} 區")
    check("樣本過小的區有被標記出來", "underpowered_districts" in ds,
          f"{len(ds['underpowered_districts'])} 區樣本不足")
    check("有給出基準選用建議", bool(ds["recommendation"]), ds["recommendation"][:50])

    print("\n[4] 工具 D：風險評分")
    r = scoring.score_institution(iid, sentiment_days=365)
    check("總分在 0-100", 0 <= r["total_score"] <= 100, str(r["total_score"]))
    check("四個子分數都有", len(r["subscores"]) == 4, json.dumps(r["subscores"]))
    check("含法規遵循子分數", "regulatory_compliance" in r["subscores"])
    w = sum(r["weights"].values())
    check("權重合計為 1", abs(w - 1.0) < 1e-6, str(w))
    recomputed = sum(r["weighted_contributions"].values())
    check("加權貢獻合計等於總分", abs(recomputed - r["total_score"]) < 0.2,
          f"{recomputed} vs {r['total_score']}")
    check("有風險等級與建議處置", bool(r["risk_level"] and r["recommended_action"]),
          f"{r['risk_level']} / {r['recommended_action']}")
    check("有證據引用", len(r["evidence"]) > 0, f"{len(r['evidence'])} 條")

    # 排行榜與等級下限的驗證需要全市都評分過，這裡自己跑一次，讓測試完全自足
    if scoring.leaderboard("新北市", 100)["count"] < 10:
        print("  （執行全市掃描以產生排行榜，約 30 秒）")
        city_scan = scoring.scan_city("新北市")
        check("全市掃描可執行", city_scan["scanned"] >= 30,
              f"{city_scan['scanned']} 間，等級分佈 {city_scan['risk_distribution']}")
        check("有機構被自動加派深入調查",
              len(city_scan["escalated_institutions"]) > 0,
              f"{len(city_scan['escalated_institutions'])} 間")

    lb = scoring.leaderboard("新北市", 40)
    check("排行榜有資料", lb["count"] > 0, f"{lb['count']} 筆")
    totals = [x["total"] for x in lb["leaderboard"]]
    check("排行榜依分數遞減", totals == sorted(totals, reverse=True))
    check("排行榜有區分度（非全部同分）", len(set(round(t) for t in totals)) > 5,
          f"{len(set(round(t) for t in totals))} 種分數")

    print("\n[5] 感知層與預警")
    p = agent_mod.perceive("新北市")
    check("感知層可產生訊號", len(p["triggers"]) > 0, f"{len(p['triggers'])} 個")
    al = scoring.alerts(50)
    check("有主動預警", len(al["alerts"]) > 0, f"{len(al['alerts'])} 則")

    print("\n[6] 監督式模型（後期路線）")
    t = scoring.train_supervised("新北市")
    if t["trained"]:
        check("模型可訓練且 AUC 合理", t["train_auc"] >= 0.5,
              f"AUC={t['train_auc']} samples={t['samples']}")
    else:
        check("樣本不足時明確拒絕訓練（不假裝可用）", "reason" in t, t["reason"][:60])

    print("\n[6b] 重大違規的等級下限保護欄")
    lvl, act, reason = config.apply_level_floor("低風險", 1)
    check("1 項重大違規至少拉到中風險", lvl == "中風險", f"低風險 → {lvl}")
    check("拉升時有說明原因", bool(reason), (reason or "")[:40])
    lvl, _, _ = config.apply_level_floor("低風險", 3)
    check("3 項重大違規至少拉到高風險", lvl == "高風險", f"低風險 → {lvl}")
    lvl, _, reason = config.apply_level_floor("極高風險", 5)
    check("已是極高風險時不會被降級", lvl == "極高風險" and reason is None)
    lvl, _, _ = config.apply_level_floor("低風險", 0)
    check("無重大違規時不動等級", lvl == "低風險")

    floored = [x for x in store.q(
        "SELECT inst_id FROM institutions WHERE city='新北市'")]
    lifted = 0
    for row in floored[:40]:
        sc = store.q1("SELECT total, level FROM scores WHERE inst_id=?"
                      " ORDER BY scored_at DESC LIMIT 1", (row["inst_id"],))
        if not sc:
            continue
        natural, _ = config.risk_band(sc["total"])
        if sc["level"] != natural:
            lifted += 1
    check("實際掃描結果中有機構因重大違規被拉升等級", lifted > 0, f"{lifted} 間")

    print("\n[7] 工具契約與稽核軌跡")
    from guardian import toolspec

    check("五大工具都已註冊", len(toolspec.TOOL_NAMES) == 5, str(toolspec.TOOL_NAMES))
    check("法規遵循工具已註冊", "compliance_check_tool" in toolspec.TOOL_NAMES)
    check("每個工具都有 dispatch 實作",
          all(n in toolspec.DISPATCH for n in toolspec.TOOL_NAMES))
    out = toolspec.execute("data_integration_tool", {"action": "search", "keyword": "青草地"},
                           "smoke", 1)
    check("工具可透過統一介面呼叫", out["result"]["count"] >= 1)
    log = store.q("SELECT * FROM audit_log WHERE session='smoke' ORDER BY id DESC LIMIT 1")
    check("工具呼叫有寫入稽核軌跡", len(log) == 1 and log[0]["tool"] == "data_integration_tool")

    print("\n" + "=" * 72)
    if FAILED:
        print(f"失敗 {len(FAILED)} 項：")
        for x in FAILED:
            print("  -", x)
        return 1
    print("全部通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
