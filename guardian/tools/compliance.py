"""工具 E：法規遵循檢核工具。

把可直接對照法條的硬性規定做成檢核清單，產出「法規遵循子分數」。

為什麼要獨立成一個工具，而不塞進鑑識會計？
  鑑識會計三項訊號（比率／Benford／交叉比對）是「異常線索」，證據力屬初篩；
  本工具檢出的是「已可對照條文與罰則的違規事實」，證據力完全不同等級，
  混在一起加權會把最硬的證據稀釋掉。

關鍵設計：年齡層必須分開計算
  依教育局／城鄉發展局訪談結論與幼照法第16條，2歲專班（實質 1:8，不得混齡）
  與 3歲以上（實質 1:15）必須分開判定。本工具額外做一項「合併稀釋檢查」：
  當「全園合併師生比」看起來合格、但任一年齡層單獨計算已違規時，會明確標記出來，
  避免用平均值掩蓋 2 歲專班的人力缺口。

資料不足時的處理
  欄位為 NULL 一律列入 unverifiable，不當成「合規」。這樣報告才不會把
  「沒資料」誤報成「沒問題」。
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from .. import regulations as reg
from .. import store

# 各嚴重度的扣分點數
SEVERITY_POINTS = {3: 30.0, 2: 14.0, 1: 5.0}

# 點數 → 0~100 子分數的換算尺度。
# 用遞減飽和函數 100 * (1 - exp(-raw / SCALE))，理由：
#   直接把點數截斷在 100 會讓「10 項違規含 4 項重大」和「8 項違規含 2 項重大」同分，
#   排行榜就失去區分度。用飽和函數可保持單調遞增又不會爆表。
# 對照表（SCALE=55）：1 項重大 ≈ 42 分、2 項重大 ≈ 66 分、
#   重大+中度共 100 點 ≈ 84 分、200 點 ≈ 97 分。
SUBSCORE_SCALE = 55.0


def _num(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _fnum(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def check(inst_id: str, city: str | None = None) -> dict[str, Any]:
    """對單一機構跑完法規遵循檢核清單。"""
    inst = store.get_institution(inst_id)
    if not inst:
        return {"error": f"找不到機構 {inst_id}"}
    city = city or inst.get("city") or ""
    inst_type = inst.get("inst_type") or "幼兒園"

    violations: list[reg.Violation] = []
    unverifiable: list[dict[str, str]] = []
    checked: list[str] = []

    def miss(indicator: str, fields: str, citation: reg.Citation) -> None:
        unverifiable.append({"indicator": indicator, "missing_fields": fields,
                             "citation": citation.cite(),
                             "note": "整合資料庫缺此欄位，無法檢核；不得視為合規"})

    total = _num(inst.get("enrolled"))
    capacity = _num(inst.get("capacity"))

    if inst_type == "托嬰中心":
        _check_infant_center(inst, city, violations, unverifiable, checked, miss)
    else:
        _check_kindergarten(inst, city, violations, unverifiable, checked, miss)

    # ---------------- 共通：超收 ----------------
    checked.append("核定招收人數 vs 實際招收人數")
    if total is None or capacity is None:
        miss("超收檢核", "enrolled / capacity", reg.C_ECEA_38)
    elif total > capacity:
        over = total - capacity
        hard = over > reg.OVERSUBSCRIBE_HARD_LIMIT
        violations.append(reg.Violation(
            code="OVERSUBSCRIBE",
            indicator="超收幼兒",
            severity=3 if hard else 2,
            observed=f"實際 {total} 人 / 核定 {capacity} 人",
            required=f"不得超過核定招收人數 {capacity} 人",
            detail=(f"實際招收 {total} 人，超出核定招收人數 {over} 人"
                    + (f"，已逾 {reg.OVERSUBSCRIBE_HARD_LIMIT} 人門檻，"
                       "屬幼照法第52條第1項第1款情形" if hard else "")),
            citation=reg.C_ECEA_38,
            penalty=reg.C_ECEA_52,
            tags=["超收", "招收人數"],
        ))

    # ---------------- 共通：幼兒團體保險 ----------------
    checked.append("幼兒團體保險")
    ins = _num(inst.get("has_group_insurance"))
    if ins is None:
        miss("幼兒團體保險", "has_group_insurance", reg.C_ECEA_34)
    elif ins == 0:
        violations.append(reg.Violation(
            code="NO_GROUP_INSURANCE", indicator="未辦理幼兒團體保險", severity=3,
            observed="未辦理", required="應辦理幼兒團體保險",
            detail="查無幼兒團體保險投保紀錄，屬幼照法第52條第1項第5款情形",
            citation=reg.C_ECEA_34, penalty=reg.C_ECEA_52, tags=["保險"]))

    # ---------------- 共通：幼童專用車 ----------------
    bus_count = _num(inst.get("bus_count")) or 0
    if bus_count > 0:
        checked.append("幼童專用車車齡與隨車人員")
        year = _num(inst.get("bus_oldest_year"))
        escort = _num(inst.get("bus_has_escort"))
        this_year = datetime.now(timezone.utc).year
        if year is None:
            miss("幼童專用車車齡", "bus_oldest_year", reg.C_ECEA_31)
        else:
            age = this_year - year
            if age > reg.BUS_MAX_AGE_YEARS:
                violations.append(reg.Violation(
                    code="BUS_TOO_OLD", indicator="幼童專用車車齡逾限", severity=3,
                    observed=f"最舊車輛 {year} 年出廠，車齡約 {age} 年",
                    required=f"車齡不得逾出廠 {reg.BUS_MAX_AGE_YEARS} 年",
                    detail=(f"最舊幼童專用車為 {year} 年出廠，車齡約 {age} 年，"
                            f"已逾法定 {reg.BUS_MAX_AGE_YEARS} 年上限"),
                    citation=reg.C_ECEA_31, tags=["交通安全"]))
        if escort is None:
            miss("隨車人員配置", "bus_has_escort", reg.C_ECEA_31)
        elif escort == 0:
            violations.append(reg.Violation(
                code="BUS_NO_ESCORT", indicator="未配置隨車人員", severity=3,
                observed="無隨車人員", required="應配置具教保服務人員資格或成年人隨車照護",
                detail="幼童專用車未配置隨車人員，接送過程無人照護幼兒",
                citation=reg.C_ECEA_31, tags=["交通安全"]))

    # ---------------- 共通：收費報備查 ----------------
    checked.append("收費數額報主管機關備查")
    filed = inst.get("fee_filed_date")
    if not filed:
        violations.append(reg.Violation(
            code="FEE_NOT_FILED", indicator="收費數額未報備查", severity=2,
            observed="查無報備查紀錄", required="應於每學年度開始前對外公布並報主管機關備查",
            detail=("查無收費數額報主管機關備查之紀錄；未報備查或以超過備查之數額及"
                    "項目收費，屬幼照法第52條第1項第6款情形"),
            citation=reg.C_ECEA_43, penalty=reg.C_ECEA_52, tags=["收費"]))

    # ---------------- 計分 ----------------
    raw = sum(SEVERITY_POINTS.get(v.severity, 0.0) for v in violations)
    subscore = 100.0 * (1.0 - math.exp(-raw / SUBSCORE_SCALE)) if raw > 0 else 0.0
    critical = [v for v in violations if v.severity == 3]

    return {
        "inst_id": inst_id,
        "institution": inst["name"],
        "inst_type": inst_type,
        "city": city,
        "compliance_subscore": round(subscore, 1),
        "score_breakdown": {
            "raw_penalty_points": round(raw, 1),
            "points_by_severity": {SEVERITY_POINTS[s]: sum(1 for v in violations if v.severity == s)
                                   for s in sorted(SEVERITY_POINTS, reverse=True)
                                   if any(v.severity == s for v in violations)},
            "scale": SUBSCORE_SCALE,
            "formula": "100 × (1 − e^(−點數 / 55))",
        },
        "violation_count": len(violations),
        "critical_count": len(critical),
        "violations": [v.to_dict() for v in violations],
        "evidence": [reg.evidence_line(v) for v in violations]
                    or ["法規遵循檢核清單未發現違規項目。"],
        "checked_items": checked,
        "unverifiable": unverifiable,
        "local_override_applied": bool(reg.LOCAL_OVERRIDES.get(city)),
        "note": (
            f"共檢核 {len(checked)} 項硬性規定，發現 {len(violations)} 項違規"
            f"（重大 {len(critical)} 項）。"
            + (f"另有 {len(unverifiable)} 項因資料庫欄位缺漏無法檢核，已列為待補資料，"
               "不得視為合規。" if unverifiable else "")
            + " 本工具僅比對可量化之法定門檻，實際處分仍須經實地稽查與行政程序認定。"
        ),
    }


# ------------------------------------------------------------------ 幼兒園
def _check_kindergarten(inst, city, violations, unverifiable, checked, miss) -> None:
    e2 = _num(inst.get("enrolled_2y"))
    e35 = _num(inst.get("enrolled_3to5"))
    c2 = _num(inst.get("classes_2y"))
    c35 = _num(inst.get("classes_3to5"))
    c5 = _num(inst.get("classes_5y"))
    s2 = _num(inst.get("staff_2y"))
    s35 = _num(inst.get("staff_3to5"))
    staff_total = _num(inst.get("staff_count"))
    total = _num(inst.get("enrolled"))
    remote = bool(_num(inst.get("is_remote_area")))
    approved = bool(_num(inst.get("mixed_age_approved")))
    public_aff = bool(_num(inst.get("is_public_affiliated")))
    disabled = _num(inst.get("disabled_children")) or 0

    band2 = reg.band_for("幼兒園", "age_2_to_3", city)
    band35 = reg.band_for("幼兒園", "age_3_to_6", city)

    checked.append("2歲專班與3歲以上班級人數上限（分開計算）")
    checked.append("2歲專班與3歲以上教保服務人員配置（分開計算）")

    if e2 is None or e35 is None:
        miss("年齡層師生比", "enrolled_2y / enrolled_3to5", reg.C_ECEA_16_4)
        return

    # ---- 不得混齡編班 ----
    if e2 > 0:
        checked.append("2歲專班不得與其他年齡幼兒混齡")
        if c2 is not None and c2 == 0:
            if remote and approved:
                pass  # 離島偏遠原民區經核准者為法定例外
            else:
                violations.append(reg.Violation(
                    code="MIXED_AGE_2Y", indicator="2歲專班混齡編班", severity=3,
                    observed=f"2歲以上未滿3歲 {e2} 人，但未設獨立班級",
                    required="應單獨成班，不得與其他年齡幼兒混齡",
                    detail=(f"收托 2 歲以上未滿 3 歲幼兒 {e2} 人卻未設獨立班級。"
                            "混齡編班僅限離島、偏遠及原住民族地區且經主管機關同意，"
                            "本機構不符例外要件"),
                    citation=reg.C_ECEA_16_1, penalty=reg.C_ECEA_52,
                    tags=["2歲專班", "混齡"]))

    # ---- 每班人數上限 ----
    for label, enrolled_n, classes_n, band in (
        ("2歲專班", e2, c2, band2), ("3歲以上班級", e35, c35, band35)
    ):
        if not enrolled_n:
            continue
        if classes_n is None:
            miss(f"{label}每班人數", "classes_*", band.citation_class)
            continue
        if classes_n <= 0:
            continue
        avg = enrolled_n / classes_n
        if avg > band.class_size_limit:
            allow = f"每班以 {band.class_size_limit} 人為限"
            if disabled:
                allow += f"（該園有 {disabled} 名身心障礙幼兒，得依{reg.C_ECEA_16_2.cite()}酌減）"
            violations.append(reg.Violation(
                code=f"CLASS_SIZE_{band.key.upper()}",
                indicator=f"{label}每班人數超限", severity=2,
                observed=f"{enrolled_n} 人 ÷ {classes_n} 班 = 平均每班 {avg:.1f} 人",
                required=allow,
                detail=(f"{label} {enrolled_n} 人分 {classes_n} 班，平均每班 {avg:.1f} 人，"
                        f"超過法定上限 {band.class_size_limit} 人"),
                citation=band.citation_class, penalty=reg.C_ECEA_52,
                tags=[label, "班級人數"]))

    # ---- 教保服務人員配置：分年齡層 ----
    need2 = band2.min_staff(e2, c2) if e2 else 0
    need35 = band35.min_staff(e35, c35) if e35 else 0
    band_shortfall: list[str] = []

    for label, enrolled_n, staff_n, need_n, band in (
        ("2歲專班", e2, s2, need2, band2),
        ("3歲以上班級", e35, s35, need35, band35),
    ):
        if not enrolled_n:
            continue
        if staff_n is None:
            miss(f"{label}人員配置", "staff_*", band.citation_staff)
            continue
        if staff_n < need_n:
            short = need_n - staff_n
            actual_ratio = enrolled_n / staff_n if staff_n else float("inf")
            band_shortfall.append(
                f"{label} 1:{actual_ratio:.1f}（法定 1:{band.staff_threshold}，短缺 {short} 人）")
            violations.append(reg.Violation(
                code=f"STAFF_RATIO_{band.key.upper()}",
                indicator=f"{label}教保服務人員配置不足", severity=3,
                observed=f"{enrolled_n} 人配置 {staff_n} 人，實際 1:{actual_ratio:.1f}",
                required=f"至少 {need_n} 人（實質 1:{band.staff_threshold}）",
                detail=(f"{label} 收托 {enrolled_n} 人，依法至少應置 {need_n} 名"
                        f"教保服務人員（每 {band.staff_threshold} 人 1 人，且園長不計入），"
                        f"實際僅 {staff_n} 名，短缺 {short} 名"
                        + (f"。{band.note}" if band.note else "")),
                citation=band.citation_staff, penalty=reg.C_ECEA_52,
                tags=[label, "師生比"]))

    # ---- 合併稀釋檢查（教育局／城鄉發展局訪談指出的盲點）----
    if band_shortfall and total and staff_total:
        checked.append("合併師生比是否掩蓋單一年齡層缺口")
        blended = total / staff_total
        # 以人數加權的合併法定基準，用來說明「全園平均值為什麼會失真」
        weighted_need = e2 / band2.staff_threshold + e35 / band35.staff_threshold
        blended_legal = total / weighted_need if weighted_need > 0 else 0.0
        if blended_legal > 0 and blended <= blended_legal:
            violations.append(reg.Violation(
                code="BLENDED_RATIO_MASKING",
                indicator="合併計算掩蓋年齡層人力缺口", severity=2,
                observed=f"全園合併 1:{blended:.1f}（加權法定基準 1:{blended_legal:.1f}，表面合格）",
                required="須依年齡層分別計算，不得以全園平均認定合規",
                detail=(f"全園 {total} 人 ÷ {staff_total} 名教保服務人員 = 1:{blended:.1f}，"
                        f"以加權後的法定基準 1:{blended_legal:.1f} 看似合格；"
                        f"但分年齡層計算後仍有缺口：{'、'.join(band_shortfall)}。"
                        "2歲專班與3歲以上不得混齡，兩者法定配置不同，"
                        "以全園平均認定合規會直接掩蓋 2 歲專班的人力不足"),
                citation=reg.C_ECEA_16_4, penalty=reg.C_ECEA_52,
                tags=["2歲專班", "指標合理性"]))

    # ---- 公立學校附設每園再增置1人 ----
    if public_aff and staff_total is not None:
        checked.append("公立學校附設幼兒園應再增置教保服務人員1人")
        need_all = need2 + need35 + 1
        if staff_total < need_all:
            violations.append(reg.Violation(
                code="PUBLIC_EXTRA_STAFF", indicator="公立學校附設未增置人員", severity=2,
                observed=f"{staff_total} 人", required=f"至少 {need_all} 人（含應再增置之 1 人）",
                detail=(f"公立學校附設幼兒園除依各年齡層配置（{need2}+{need35}={need2 + need35} 人）外，"
                        f"每園應再增置教保服務人員 1 人，合計 {need_all} 人，實際 {staff_total} 人"),
                citation=reg.C_ECEA_16_5, penalty=reg.C_ECEA_52, tags=["人員配置"]))

    # ---- 助理教保員比例 ----
    assistants = _num(inst.get("assistant_count"))
    if assistants is None or staff_total is None:
        miss("助理教保員比例", "assistant_count / staff_count", reg.C_ECEA_17_2)
    else:
        checked.append("助理教保員占教保服務人員總數比例")
        cap = math.floor(staff_total * reg.ASSISTANT_MAX_SHARE)
        if assistants > cap:
            violations.append(reg.Violation(
                code="ASSISTANT_SHARE", indicator="助理教保員超過三分之一上限", severity=2,
                observed=f"{assistants} 人 / 教保服務人員 {staff_total} 人 = "
                         f"{assistants / staff_total:.1%}",
                required=f"不得超過三分之一（上限 {cap} 人）",
                detail=(f"助理教保員 {assistants} 人，占園內教保服務人員總數 {staff_total} 人的"
                        f" {assistants / staff_total:.1%}，超過法定三分之一上限（{cap} 人）"),
                citation=reg.C_ECEA_17_2, tags=["人員資格"]))

    # ---- 5歲班應有幼兒園教師 ----
    teachers = _num(inst.get("teacher_count"))
    if c5:
        checked.append("5歲至入國民小學前班級應有幼兒園教師")
        if teachers is None:
            miss("幼兒園教師配置", "teacher_count", reg.C_ECEA_17_1)
        elif teachers < c5:
            violations.append(reg.Violation(
                code="NO_QUALIFIED_TEACHER_5Y", indicator="5歲班未配置足額幼兒園教師",
                severity=2,
                observed=f"幼兒園教師 {teachers} 人 / 5歲班 {c5} 班",
                required=f"每班至少 1 人為幼兒園教師（至少 {c5} 人）",
                detail=(f"5 歲至入國民小學前共 {c5} 班，每班應有 1 人以上為幼兒園教師，"
                        f"實際僅 {teachers} 名幼兒園教師，不足 {c5 - teachers} 名"),
                citation=reg.C_ECEA_17_1, tags=["人員資格"]))

    # ---- 護理人員 ----
    if total is not None:
        checked.append("護理人員配置型態")
        allowed, cite = reg.required_nurse_types(total)
        nurse = inst.get("nurse_type")
        if not nurse:
            miss("護理人員配置", "nurse_type", cite)
        elif nurse == "無" or nurse not in allowed:
            violations.append(reg.Violation(
                code="NURSE_TYPE", indicator="護理人員配置型態不符", severity=2,
                observed=f"目前為「{nurse}」", required=f"招收 {total} 人應為：{'／'.join(allowed)}",
                detail=(f"合計招收幼兒 {total} 人，依法應以 {'／'.join(allowed)} 方式置護理人員，"
                        f"實際為「{nurse}」"),
                citation=cite, tags=["衛生保健"]))

    # ---- 廚工 ----
    if total is not None:
        checked.append("廚工人數")
        is_public = bool(public_aff) or "市立" in (inst.get("name") or "")
        need_cook, cite = reg.required_cooks(total, is_public)
        cooks = _num(inst.get("cook_count"))
        if cooks is None:
            miss("廚工人數", "cook_count", cite)
        elif cooks < need_cook:
            violations.append(reg.Violation(
                code="COOK_COUNT", indicator="廚工人數不足", severity=1,
                observed=f"{cooks} 人", required=f"至少 {need_cook} 人",
                detail=(f"招收 {total} 人，依幼兒園行政組織及員額編制標準"
                        f"至少應置廚工 {need_cook} 人，實際 {cooks} 人"),
                citation=cite, tags=["員額編制"]))

    # ---- 專任園長 ----
    principal = _num(inst.get("has_principal"))
    checked.append("專任園長")
    if principal is None:
        miss("專任園長", "has_principal", reg.C_ORG_2)
    elif principal == 0:
        violations.append(reg.Violation(
            code="NO_PRINCIPAL", indicator="未置專任園長", severity=2,
            observed="未置", required="園長 1 人，專任",
            detail="查無專任園長；且園長不計入幼照法第16條第4項之教保服務人員配置",
            citation=reg.C_ORG_2, tags=["員額編制"]))

    # ---- 2歲專班室外活動區隔 ----
    if e2 > 0:
        checked.append("2歲專班室外活動空間或時間區隔")
        sep = _num(inst.get("outdoor_separated_2y"))
        if sep is None:
            miss("2歲專班室外活動區隔", "outdoor_separated_2y", reg.C_IMPL_16)
        elif sep == 0:
            violations.append(reg.Violation(
                code="OUTDOOR_NOT_SEPARATED", indicator="2歲專班室外活動未與3歲以上區隔",
                severity=2,
                observed="未區隔", required="室外活動之空間或時間應與3歲以上幼兒區隔",
                detail=(f"收托 2 歲以上未滿 3 歲幼兒 {e2} 人，其室外活動未與 3 歲以上幼兒"
                        "在空間或時間上區隔，與幼兒教保及照顧服務實施準則不符"),
                citation=reg.C_IMPL_16, tags=["2歲專班", "安全"]))


# ------------------------------------------------------------------ 托嬰中心
def _check_infant_center(inst, city, violations, unverifiable, checked, miss) -> None:
    total = _num(inst.get("enrolled"))
    staff = _num(inst.get("staff_count"))
    band = reg.band_for("托嬰中心", "age_under_2", city)

    checked.append("托育人員配置（每收托5名應置1人）")
    if total is None or staff is None:
        miss("托育人員配置", "enrolled / staff_count", band.citation_staff)
    else:
        need = band.min_staff(total)
        if staff < need:
            violations.append(reg.Violation(
                code="INFANT_STAFF_RATIO", indicator="托育人員配置不足", severity=3,
                observed=f"{total} 人配置 {staff} 人，實際 1:{total / staff:.1f}"
                         if staff else f"{total} 人配置 0 人",
                required=f"至少 {need} 人（每收托 {band.staff_threshold} 名 1 人，"
                         f"未滿 {band.staff_threshold} 人者以 {band.staff_threshold} 人計）",
                detail=(f"收托 {total} 名未滿 2 歲兒童，依法至少應置專任托育人員 {need} 名，"
                        f"實際 {staff} 名，短缺 {need - staff} 名"),
                citation=band.citation_staff, tags=["托嬰中心", "師生比"]))

    checked.append("專任主管人員與特約醫師／專任護理人員")
    principal = _num(inst.get("has_principal"))
    if principal == 0:
        violations.append(reg.Violation(
            code="INFANT_NO_SUPERVISOR", indicator="未置專任主管人員", severity=2,
            observed="未置", required="應置專任主管人員 1 人綜理業務",
            detail="查無專任主管人員", citation=reg.C_CWSS_25, tags=["托嬰中心"]))
    nurse = inst.get("nurse_type")
    if not nurse:
        miss("特約醫師或專任護理人員", "nurse_type", reg.C_CWSS_25)
    elif nurse in ("無",) or nurse == "兼任":
        violations.append(reg.Violation(
            code="INFANT_NO_MEDICAL", indicator="未置特約醫師或專任護理人員", severity=2,
            observed=f"目前為「{nurse}」", required="特約醫師或專任護理人員至少 1 人",
            detail=(f"托嬰中心應置特約醫師或專任護理人員至少 1 人，"
                    f"實際為「{nurse}」，不符規定（兼任護理人員不符本條要件）"),
            citation=reg.C_CWSS_25, tags=["托嬰中心", "衛生保健"]))

    # ---- 空間 ----
    indoor = _fnum(inst.get("indoor_area"))
    outdoor = _fnum(inst.get("outdoor_area"))
    checked.append("室內樓地板面積與室外活動面積")
    if indoor is None or outdoor is None or total is None:
        miss("空間面積", "indoor_area / outdoor_area", reg.C_CWSS_23)
    else:
        combined = indoor + outdoor
        if combined < reg.INFANT_MIN_TOTAL_AREA:
            violations.append(reg.Violation(
                code="INFANT_TOTAL_AREA", indicator="室內外面積合計不足", severity=2,
                observed=f"{combined:.1f} ㎡",
                required=f"合計應達 {reg.INFANT_MIN_TOTAL_AREA:.0f} ㎡ 以上",
                detail=(f"室內樓地板面積 {indoor:.1f} ㎡ + 室外活動面積 {outdoor:.1f} ㎡ "
                        f"= {combined:.1f} ㎡，未達法定 {reg.INFANT_MIN_TOTAL_AREA:.0f} ㎡"),
                citation=reg.C_CWSS_23, tags=["托嬰中心", "空間"]))
        per_in = indoor / total if total else 0
        if per_in < reg.INFANT_MIN_INDOOR_PER_CHILD:
            violations.append(reg.Violation(
                code="INFANT_INDOOR_PER_CHILD", indicator="室內每人面積不足", severity=3,
                observed=f"每人 {per_in:.2f} ㎡",
                required=f"每人不得少於 {reg.INFANT_MIN_INDOOR_PER_CHILD} ㎡",
                detail=(f"室內樓地板面積 {indoor:.1f} ㎡ ÷ 收托 {total} 人 = 每人 {per_in:.2f} ㎡，"
                        f"低於法定 {reg.INFANT_MIN_INDOOR_PER_CHILD} ㎡；"
                        f"依現有面積最多僅能收托 {int(indoor // reg.INFANT_MIN_INDOOR_PER_CHILD)} 人"),
                citation=reg.C_CWSS_23, tags=["托嬰中心", "空間", "超收"]))
        per_out = outdoor / total if total else 0
        # 室外不足時得以其他室內樓地板面積每人至少 1.5 ㎡ 代之
        if per_out < reg.INFANT_MIN_OUTDOOR_PER_CHILD and per_in < (
            reg.INFANT_MIN_INDOOR_PER_CHILD + reg.INFANT_MIN_OUTDOOR_PER_CHILD
        ):
            violations.append(reg.Violation(
                code="INFANT_OUTDOOR_PER_CHILD", indicator="室外活動面積不足且未以室內替代",
                severity=2,
                observed=f"室外每人 {per_out:.2f} ㎡、室內每人 {per_in:.2f} ㎡",
                required=f"室外每人不得少於 {reg.INFANT_MIN_OUTDOOR_PER_CHILD} ㎡，"
                         f"不足時得以其他室內樓地板面積每人至少 {reg.INFANT_MIN_OUTDOOR_PER_CHILD} ㎡代之",
                detail=(f"室外活動面積每人僅 {per_out:.2f} ㎡，低於法定 "
                        f"{reg.INFANT_MIN_OUTDOOR_PER_CHILD} ㎡，且室內面積亦不足以替代"),
                citation=reg.C_CWSS_23, tags=["托嬰中心", "空間"]))

    # ---- 樓層 ----
    floor = _num(inst.get("floor_max"))
    checked.append("使用建築物樓層")
    if floor is None:
        miss("使用樓層", "floor_max", reg.C_CWSS_21)
    elif floor > reg.INFANT_MAX_FLOOR:
        violations.append(reg.Violation(
            code="INFANT_FLOOR", indicator="使用樓層逾限", severity=3,
            observed=f"使用至 {floor} 樓",
            required=f"以地面樓層 1 樓至 {reg.INFANT_MAX_FLOOR} 樓為限",
            detail=(f"托嬰中心使用至 {floor} 樓，超過法定 {reg.INFANT_MAX_FLOOR} 樓上限，"
                    "影響緊急疏散安全"),
            citation=reg.C_CWSS_21, tags=["托嬰中心", "安全"]))


# ------------------------------------------------------------------ 批次
def scan_city(city: str = "新北市", limit: int = 500) -> dict[str, Any]:
    rows = store.q("SELECT inst_id, name FROM institutions WHERE city=? LIMIT ?", (city, limit))
    results = []
    code_counter: dict[str, int] = {}
    for r in rows:
        c = check(r["inst_id"], city)
        if c.get("error"):
            continue
        for v in c["violations"]:
            code_counter[v["indicator"]] = code_counter.get(v["indicator"], 0) + 1
        results.append({
            "inst_id": r["inst_id"], "name": r["name"],
            "compliance_subscore": c["compliance_subscore"],
            "violation_count": c["violation_count"],
            "critical_count": c["critical_count"],
            "top_violations": [v["indicator"] for v in c["violations"][:3]],
        })
    results.sort(key=lambda x: (-x["critical_count"], -x["compliance_subscore"]))
    return {
        "city": city,
        "scanned": len(results),
        "with_violations": sum(1 for r in results if r["violation_count"]),
        "with_critical": sum(1 for r in results if r["critical_count"]),
        "violation_frequency": dict(sorted(code_counter.items(), key=lambda kv: -kv[1])),
        "results": results[:30],
    }
