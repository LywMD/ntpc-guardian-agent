"""法規基準單一真實來源（Single Source of Truth）。

所有法定門檻集中在這裡，每一條都附法源條號，讓 Agent 產出的報告可以直接引用，
稽查人員也能回查原文。任何判定邏輯都不得在別的模組硬寫數字。

已核對之法源（全國法規資料庫）
  幼兒教育及照顧法              https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=H0070031
  幼兒教保及照顧服務實施準則      https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=H0070047
  幼兒園行政組織及員額編制標準    https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=H0070046
  兒童及少年福利機構設置標準      https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=D0050015

地方從嚴原則
  兒童及少年福利機構設置標準附則明定：「直轄市、縣（市）自治法規有關人員配置及
  樓地板面積之規定高於本標準者，從其規定。」
  因此本模組支援以 LOCAL_OVERRIDES 針對特定縣市加嚴，且只允許加嚴、不允許放寬
  （apply_local_override 會擋掉放寬的設定並記錄警告）。
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("guardian.regulations")

MOJ = "https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode="


@dataclass(frozen=True)
class Citation:
    """法源引用。報告裡的每個違規判定都要掛一個。"""

    law: str
    article: str
    summary: str
    pcode: str = ""

    @property
    def url(self) -> str:
        return f"{MOJ}{self.pcode}" if self.pcode else ""

    def cite(self) -> str:
        return f"{self.law}{self.article}"

    def to_dict(self) -> dict[str, str]:
        return {"law": self.law, "article": self.article,
                "summary": self.summary, "url": self.url}


# ------------------------------------------------------------------ 法源
C_ECEA_16_1 = Citation(
    "幼兒教育及照顧法", "第16條第1項",
    "幼兒園2歲以上未滿3歲幼兒每班以16人為限，且不得與其他年齡幼兒混齡；"
    "3歲以上至入國民小學前幼兒每班以30人為限。", "H0070031")
C_ECEA_16_1_EX = Citation(
    "幼兒教育及照顧法", "第16條第1項但書",
    "離島、偏遠及原住民族地區因2歲以上未滿3歲幼兒人數稀少無法單獨成班者，"
    "得報主管機關同意後混齡編班，每班以15人為限。", "H0070031")
C_ECEA_16_2 = Citation(
    "幼兒教育及照顧法", "第16條第2項",
    "幼兒園有招收身心障礙幼兒之班級，得酌予減少班級人數。", "H0070031")
C_ECEA_16_4 = Citation(
    "幼兒教育及照顧法", "第16條第4項",
    "幼兒園及其分班「除園長外」應配置教保服務人員：招收2歲以上未滿3歲之班級，"
    "8人以下置1人、9人以上置2人；招收3歲以上至入國民小學前之班級，"
    "15人以下置1人、16人以上置2人。", "H0070031")
C_ECEA_16_5 = Citation(
    "幼兒教育及照顧法", "第16條第5項",
    "公立學校附設幼兒園，除依規定配置教保服務人員外，每園應再增置教保服務人員1人。",
    "H0070031")
C_ECEA_17_1 = Citation(
    "幼兒教育及照顧法", "第17條第1項",
    "幼兒園有5歲至入國民小學前幼兒之班級，其配置之教保服務人員每班應有1人以上為幼兒園教師。",
    "H0070031")
C_ECEA_17_2 = Citation(
    "幼兒教育及照顧法", "第17條第2項",
    "幼兒園助理教保員之人數，不得超過園內教保服務人員總人數之三分之一。", "H0070031")
C_ECEA_17_4 = Citation(
    "幼兒教育及照顧法", "第17條第4項",
    "幼兒園及其分班應置護理人員：合計招收幼兒總數60人以下以特約或兼任方式；"
    "61人至200人以特約、兼任或專任方式；201人以上以專任方式。", "H0070031")
C_ECEA_30 = Citation(
    "幼兒教育及照顧法", "第30條",
    "不得對幼兒有身心虐待、體罰、霸凌、性騷擾、不當管教或其他身心暴力或不當對待；"
    "知悉時通報主管機關至遲不得超過24小時。", "H0070031")
C_ECEA_31 = Citation(
    "幼兒教育及照顧法", "第31條",
    "接送幼兒應以核准之幼童專用車輛為之，車齡不得逾出廠10年；駕駛人應具職業駕駛執照，"
    "並配置具教保服務人員資格或成年人擔任隨車人員。", "H0070031")
C_ECEA_34 = Citation(
    "幼兒教育及照顧法", "第34條第1項",
    "教保服務機構應依高級中等以下學校學生及教保服務機構幼兒團體保險條例辦理幼兒團體保險。",
    "H0070031")
C_ECEA_38 = Citation(
    "幼兒教育及照顧法", "第38條",
    "教保服務機構應公開核定之招收人數及實際招收人數等資訊。", "H0070031")
C_ECEA_43 = Citation(
    "幼兒教育及照顧法", "第43條",
    "私立教保服務機構訂定之收費數額，應於每學年度開始前對外公布並報主管機關備查；"
    "收退費基準、收費項目及數額應至少於每學期開始前1個月公告。", "H0070031")
C_ECEA_45 = Citation(
    "幼兒教育及照顧法", "第45條",
    "教保服務機構各項經費收支保管及運用應設置專帳處理，收支應有合法憑證並依規定年限保存；"
    "私立機構會計帳簿與憑證依相關稅法規定辦理；法人附設機構之財務應獨立。", "H0070031")
C_ECEA_50 = Citation(
    "幼兒教育及照顧法", "第50條",
    "對幼兒身心虐待，或情節重大之體罰、霸凌、性騷擾、不當管教、其他身心暴力或不當對待，"
    "處行為人6萬元以上60萬元以下罰鍰，並公布行為人姓名及機構名稱。", "H0070031")
C_ECEA_52 = Citation(
    "幼兒教育及照顧法", "第52條",
    "違反班級人數規定或每班配置教保服務人員規定、超收幼兒逾15人或檢查時藏匿幼兒、"
    "未辦理幼兒團體保險、以超過備查之數額及項目收費等，處負責人6萬元以上30萬元以下罰鍰，"
    "並令限期改善；屆期未改善得按次處罰，情節重大得減招、停止招生6個月至1年、"
    "停辦1年至3年或廢止設立許可。", "H0070031")
C_IMPL_16 = Citation(
    "幼兒教保及照顧服務實施準則", "第16條",
    "幼兒園2歲以上未滿3歲幼兒之室外活動，其空間或時間應與3歲以上幼兒區隔。", "H0070047")
C_IMPL_15 = Citation(
    "幼兒教保及照顧服務實施準則", "第15條第2項第4款",
    "校外教學照顧者與3歲以上幼兒人數比例不得逾1比8；與2歲以上未滿3歲幼兒不得逾1比3。",
    "H0070047")
C_ORG_2 = Citation(
    "幼兒園行政組織及員額編制標準", "第2條",
    "園長1人專任；園長以外之教保服務人員依幼照法第16條第4項配置且應為專任；"
    "私立幼兒園廚工招收人數120人以下置1人，超過者每120人增置1人。", "H0070046")
C_CWSS_25 = Citation(
    "兒童及少年福利機構設置標準", "第25條",
    "托嬰中心應置專任主管人員1人，並置特約醫師或專任護理人員至少1人；"
    "每收托5名兒童應置專任托育人員1人，未滿5人者以5人計。", "D0050015")
C_CWSS_23 = Citation(
    "兒童及少年福利機構設置標準", "第23條",
    "托嬰中心室內樓地板面積及室外活動面積扣除非兒童主要活動空間後合計應達60平方公尺以上；"
    "室內樓地板面積每人不得少於2平方公尺，室外活動面積每人不得少於1.5平方公尺。", "D0050015")
C_CWSS_21 = Citation(
    "兒童及少年福利機構設置標準", "第21條",
    "托嬰中心使用建築物樓層以地面樓層一樓至三樓為限。", "D0050015")
C_CWSS_LOCAL = Citation(
    "兒童及少年福利機構設置標準", "附則第52條",
    "本標準施行後，直轄市、縣（市）自治法規有關人員配置及樓地板面積之規定"
    "高於本標準者，從其規定。", "D0050015")


# ------------------------------------------------------------------ 年齡層規則
@dataclass(frozen=True)
class AgeBandRule:
    """單一年齡層的班級與人員配置規則。

    幼照法第16條第4項是「逐班分級」規定，不是單純的比例。但可以證明：
    設某年齡層幼兒 N 人、班級 C 班，每班上限 L、第一級門檻 t（且 L = 2t），
    則最少應置教保服務人員 = max(C, ceil(N / t))。
    因此 t 就是實質的法定師生比分母（2-3歲為 8、3歲以上為 15）。
    """

    key: str
    label: str
    staff_threshold: int      # t：每 t 名幼兒至少 1 名人員
    class_size_limit: int     # L：每班人數上限
    citation_class: Citation
    citation_staff: Citation
    no_mixed_age: bool = False
    note: str = ""

    def min_staff(self, children: int, classes: int | None = None) -> int:
        if children <= 0:
            return 0
        by_ratio = math.ceil(children / self.staff_threshold)
        return max(by_ratio, classes or 0)

    def min_classes(self, children: int) -> int:
        return math.ceil(children / self.class_size_limit) if children > 0 else 0


BAND_2Y = AgeBandRule(
    key="age_2_to_3",
    label="2歲以上未滿3歲（2歲專班）",
    staff_threshold=8,
    class_size_limit=16,
    citation_class=C_ECEA_16_1,
    citation_staff=C_ECEA_16_4,
    no_mixed_age=True,
    note="不得與其他年齡幼兒混齡編班；室外活動之空間或時間亦應與3歲以上幼兒區隔",
)
BAND_3TO5 = AgeBandRule(
    key="age_3_to_6",
    label="3歲以上至入國民小學前",
    staff_threshold=15,
    class_size_limit=30,
    citation_class=C_ECEA_16_1,
    citation_staff=C_ECEA_16_4,
)
BAND_MIXED_REMOTE = AgeBandRule(
    key="age_mixed_remote",
    label="2歲以上至入國民小學前混齡（離島偏遠原民區例外）",
    staff_threshold=8,
    class_size_limit=15,
    citation_class=C_ECEA_16_1_EX,
    citation_staff=C_ECEA_16_4,
    note="限離島、偏遠及原住民族地區，且需報主管機關同意",
)
BAND_INFANT = AgeBandRule(
    key="age_under_2",
    label="未滿2歲（托嬰中心）",
    staff_threshold=5,
    class_size_limit=5,
    citation_class=C_CWSS_25,
    citation_staff=C_CWSS_25,
    note="每收托5名兒童應置專任托育人員1人，未滿5人者以5人計",
)

KINDERGARTEN_BANDS = (BAND_2Y, BAND_3TO5)
BANDS_BY_KEY = {b.key: b for b in (BAND_2Y, BAND_3TO5, BAND_MIXED_REMOTE, BAND_INFANT)}

INST_TYPE_BANDS: dict[str, tuple[AgeBandRule, ...]] = {
    "幼兒園": KINDERGARTEN_BANDS,
    "托嬰中心": (BAND_INFANT,),
}


# ------------------------------------------------------------------ 地方從嚴覆寫
# 只允許「加嚴」。城鄉發展局／教育局如以自治法規訂更嚴的配置，填在這裡。
# 例：{"新北市": {"age_2_to_3": {"staff_threshold": 7}}}
LOCAL_OVERRIDES: dict[str, dict[str, dict[str, int]]] = {}


def band_for(inst_type: str, band_key: str, city: str = "") -> AgeBandRule:
    """取得年齡層規則，並套用該縣市的從嚴覆寫。"""
    base = BANDS_BY_KEY[band_key]
    ov = (LOCAL_OVERRIDES.get(city) or {}).get(band_key)
    if not ov:
        return base
    kwargs: dict[str, Any] = {}
    for attr in ("staff_threshold", "class_size_limit"):
        if attr not in ov:
            continue
        proposed, current = int(ov[attr]), getattr(base, attr)
        if proposed < current:          # 數字變小 = 更嚴格
            kwargs[attr] = proposed
        else:
            log.warning(
                "忽略 %s 的 %s.%s=%s：地方自治法規只能從嚴（現行 %s），不得放寬（%s）",
                city, band_key, attr, proposed, current, C_CWSS_LOCAL.cite())
    if not kwargs:
        return base
    from dataclasses import replace

    out = replace(base, **kwargs)
    log.info("%s 套用地方從嚴基準：%s", city, kwargs)
    return out


# ------------------------------------------------------------------ 其他量化基準
# 助理教保員占教保服務人員總數上限
ASSISTANT_MAX_SHARE = 1 / 3

# 護理人員配置：招收總數 → 可接受的聘用型態
def required_nurse_types(total_enrolled: int) -> tuple[list[str], Citation]:
    if total_enrolled >= 201:
        return ["專任"], C_ECEA_17_4
    if total_enrolled >= 61:
        return ["特約", "兼任", "專任"], C_ECEA_17_4
    return ["特約", "兼任", "專任"], C_ECEA_17_4


def required_cooks(total_enrolled: int, is_public: bool) -> tuple[int, Citation]:
    """廚工應置人數（幼兒園行政組織及員額編制標準第2條第6款）。"""
    unit = 72 if is_public else 120
    return max(1, math.ceil(total_enrolled / unit)) if total_enrolled > 0 else 0, C_ORG_2


# 幼童專用車
BUS_MAX_AGE_YEARS = 10

# 超收人數達此值即屬幼照法第52條第1項第1款情形
OVERSUBSCRIBE_HARD_LIMIT = 15

# 校外教學照顧比
FIELD_TRIP_RATIO = {"age_2_to_3": 3, "age_3_to_6": 8}

# 托嬰中心空間基準
INFANT_MIN_TOTAL_AREA = 60.0        # 室內+室外合計（㎡）
INFANT_MIN_INDOOR_PER_CHILD = 2.0   # 室內每人（㎡）
INFANT_MIN_OUTDOOR_PER_CHILD = 1.5  # 室外每人（㎡）
INFANT_MAX_FLOOR = 3                # 樓層上限


# ------------------------------------------------------------------ 違規結構
SEVERITY_LABEL = {3: "重大", 2: "中度", 1: "輕微"}


@dataclass
class Violation:
    code: str
    indicator: str
    severity: int                # 3 重大 / 2 中度 / 1 輕微
    observed: Any
    required: Any
    detail: str
    citation: Citation
    penalty: Citation | None = None
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "indicator": self.indicator,
            "severity": self.severity,
            "severity_label": SEVERITY_LABEL.get(self.severity, "未分級"),
            "observed": self.observed,
            "required": self.required,
            "detail": self.detail,
            "citation": self.citation.to_dict(),
            "penalty": self.penalty.to_dict() if self.penalty else None,
            "tags": self.tags,
        }


def evidence_line(v: Violation) -> str:
    base = f"[法規遵循／{SEVERITY_LABEL.get(v.severity, '')}] {v.indicator}：{v.detail}"
    cite = f"（{v.citation.cite()}"
    if v.penalty:
        cite += f"；罰則 {v.penalty.cite()}"
    return base + cite + "）"
