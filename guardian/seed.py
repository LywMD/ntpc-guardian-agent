"""離線示範資料集（新北市）。

用途：正式環境由 ETL 工具直接爬取官方公開資料；在沒有外網或政府 API
不可用時，這裡提供一份「結構與真實欄位一致」的可重現資料集，
讓 Agent 的規劃／工具呼叫／評分邏輯可以完整跑通並被驗證。

資料以固定亂數種子產生，因此每次結果一致；其中刻意埋入數種異常樣態，
用來驗證鑑識會計與輿情工具是否抓得出來。
"""
from __future__ import annotations

import hashlib
import math
import random
from typing import Any


def _stable_id(*parts: Any) -> str:
    """穩定雜湊。不能用內建 hash()：字串 hash 每個 process 都不同，
    會讓重跑 ETL 時的去重條件失效，貼文被重複累加。"""
    key = "|".join(str(p) for p in parts)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]

CITY = "新北市"

DISTRICTS = {
    "板橋區": (25.0117, 121.4628),
    "新莊區": (25.0359, 121.4503),
    "中和區": (24.9993, 121.4996),
    "三重區": (25.0614, 121.4849),
    "永和區": (25.0078, 121.5170),
    "土城區": (24.9723, 121.4433),
    "淡水區": (25.1697, 121.4407),
    "汐止區": (25.0629, 121.6410),
    "樹林區": (24.9905, 121.4207),
    "蘆洲區": (25.0848, 121.4737),
    "新店區": (24.9678, 121.5417),
    "三峽區": (24.9341, 121.3695),
}

_NAME1 = ["快樂", "小太陽", "彩虹", "向陽", "森林", "童心", "星光", "米諾", "貝貝", "橄欖",
          "大樹", "喜樂", "晨光", "小樹苗", "蒲公英", "海豚", "光年", "長頸鹿", "小熊",
          "麥克", "河馬", "青草地", "萌芽", "翡翠", "雲朵", "小蘋果", "螢火蟲", "四季",
          "水藍", "拾光", "橘子", "牧羊人", "小勇者", "百合", "楓葉", "泡泡", "小水滴",
          "花園", "月光", "陽光"]
_PREFIX = ["私立", "私立", "私立", "非營利", "市立"]

INCOME_SUBJECTS = [
    ("學費收入", 0.46),
    ("雜費收入", 0.13),
    ("代收代辦費收入", 0.14),
    ("政府補助收入", 0.20),
    ("其他收入", 0.07),
]
EXPENSE_SUBJECTS = [
    ("人事費", 0.55),
    ("業務費", 0.16),
    ("設備及維護費", 0.09),
    ("房租及水電費", 0.13),
    ("其他支出", 0.07),
]

# 異常樣態
#   clean    乾淨
#   gap      收入落差（公告收費 vs 決算申報）
#   benford  財報數字分佈不自然
#   staff    師資配置／人事費異常
#   buzz     社群輿情負面
#   ratio2y  總量看起來合格，但 2 歲專班單獨計算即違反幼照法第16條第4項
#            ← 這正是城鄉發展局會議指出的盲點，特意做成測試案例
#   compl    多項法規遵循缺失（助理教保員超比例、護理人員型態不符、
#            娃娃車車齡逾10年、未辦團保、超收逾15人…）
#   multi    複合型
PROFILE_PLAN = (
    ["clean"] * 18
    + ["gap"] * 4
    + ["benford"] * 3
    + ["staff"] * 3
    + ["buzz"] * 4
    + ["ratio2y"] * 3
    + ["compl"] * 2
    + ["multi"] * 3
)

NEG_TEMPLATES = [
    "小孩回來說老師會用尺打手心，問園方卻說是在教規矩，這樣算體罰吧？",
    "接送時間看到老師大聲吼小孩，不當管教真的讓人不放心。",
    "廚房衛生很差，餐點看起來不新鮮，小孩連續拉肚子兩次。",
    "教室有蟑螂，反映好幾次都沒改善，環境衛生根本沒在管。",
    "每學期都有莫名其妙的費用，代收代辦費收了卻沒看到東西，感覺亂收費。",
    "老師一年換三個，師資流動太嚴重，小孩剛適應又要重新來。",
    "說要退費卻一直拖，行政態度非常差。",
    "午睡時間把小孩單獨關在教室，這種處理方式我沒辦法接受。",
    "娃娃車沒有隨車人員，安全真的堪憂。",
    "投訴後園長態度很差，還說要我們自己轉學。",
]
MILD_TEMPLATES = [
    "整體還可以，就是接送有點擠，希望能改善動線。",
    "學費偏高，不過老師還算用心。",
    "戶外活動空間小了一點，其他都ok。",
]
POS_TEMPLATES = [
    "老師很有耐心，小孩每天都很期待上學。",
    "環境乾淨，餐點也用心，蔬菜水果都有。",
    "園長會主動溝通孩子的狀況，很放心。",
    "教具豐富，課程安排活潑，孩子進步明顯。",
    "衛生做得不錯，生病時通知很即時。",
]

PENALTY_REASONS = [
    ("違反幼兒教育及照顧法第 25 條－超收幼兒", "幼兒教育及照顧法", 60000, 2),
    ("違反幼兒教育及照顧法第 47 條－不當管教", "幼兒教育及照顧法", 120000, 3),
    ("違反食品安全衛生管理法－廚房衛生不合格", "食品安全衛生管理法", 30000, 2),
    ("違反幼兒教育及照顧法第 24 條－師生比不符規定", "幼兒教育及照顧法", 30000, 2),
    ("收費規定未報備核准即收取", "幼兒教育及照顧法", 15000, 1),
    ("消防安全設備檢修申報不實", "消防法", 20000, 1),
]


def _benford_amount(rng: random.Random, lo_exp: float, hi_exp: float) -> float:
    """log-uniform 取樣 → 首位數字自然符合 Benford 分佈。"""
    return round(10 ** rng.uniform(lo_exp, hi_exp), 0)


def _fabricated_amount(rng: random.Random, lo: float, hi: float) -> float:
    """模擬人為湊數：偏好整數千元、首位數字接近均勻分佈。"""
    if rng.random() < 0.55:
        return float(rng.randrange(int(lo), int(hi), 5000) or 5000)
    lead = rng.randint(1, 9)  # 均勻抽首位，刻意偏離 Benford
    magnitude = 10 ** rng.randint(3, 5)
    return float(lead * magnitude + rng.randrange(0, magnitude, 100))


def build(seed: int = 20260912) -> dict[str, list[dict[str, Any]]]:
    rng = random.Random(seed)
    names = _NAME1[:]
    rng.shuffle(names)
    profiles = PROFILE_PLAN[:]
    rng.shuffle(profiles)

    institutions: list[dict[str, Any]] = []
    fees: list[dict[str, Any]] = []
    financials: list[dict[str, Any]] = []
    penalties: list[dict[str, Any]] = []
    posts: list[dict[str, Any]] = []

    district_names = list(DISTRICTS)

    for i, base in enumerate(names[: len(profiles)]):
        profile = profiles[i]
        district = district_names[i % len(district_names)]
        lat, lng = DISTRICTS[district]
        prefix = _PREFIX[i % len(_PREFIX)]
        inst_type = "托嬰中心" if i % 9 == 4 else "幼兒園"
        name = f"{CITY}{district}{prefix}{base}{inst_type}"

        is_public = prefix == "市立"
        capacity = rng.choice([60, 75, 90, 100, 120, 150, 180])
        if profile in ("compl", "multi") and rng.random() < 0.6:
            enrolled = capacity + rng.randint(16, 26)      # 超收逾15人（幼照法§52Ⅰ①）
        else:
            enrolled = max(18, int(capacity * rng.uniform(0.62, 0.99)))

        inst = {
            "name": name,
            "inst_type": inst_type,
            "city": CITY,
            "district": district,
            "address": f"{CITY}{district}{base}路{rng.randint(1, 320)}號{rng.randint(1, 4)}樓",
            "lat": round(lat + rng.uniform(-0.012, 0.012), 6),
            "lng": round(lng + rng.uniform(-0.012, 0.012), 6),
            "capacity": capacity,
            "enrolled": enrolled,
            "rating": rng.choices(["優等", "甲等", "乙等", "丙等"],
                                  [0.15, 0.55, 0.22, 0.08])[0],
            "rating_year": 2024,
            "established": f"{rng.randint(1998, 2021)}-0{rng.randint(1,9)}-01",
            "is_public_affiliated": 1 if (is_public and inst_type == "幼兒園") else 0,
            "is_remote_area": 0,
            "mixed_age_approved": 0,
            "disabled_children": rng.choices([0, 1, 2], [0.7, 0.2, 0.1])[0],
            "has_principal": 1,
            "source": "全國教保資訊網(示範資料)",
            "_profile": profile,
        }
        if profile in ("multi", "buzz"):
            inst["rating"] = rng.choice(["乙等", "丙等"])

        inst.update(_staffing(rng, profile, inst_type, enrolled,
                              bool(inst["is_public_affiliated"])))
        inst.update(_facility_and_ops(rng, profile, inst_type, enrolled, is_public))
        staff = inst["staff_count"]
        institutions.append(inst)

        # ---------- 公告收費（每學期） ----------
        tuition = rng.choice([16000, 18000, 20000, 22000, 24000, 26000])
        misc = rng.choice([4000, 5000, 6000, 7000])
        extra = rng.choice([6000, 8000, 9000, 11000])
        if inst_type == "托嬰中心":
            tuition, misc, extra = rng.choice([48000, 54000, 60000]), 6000, 9000
        for item, amount in (("學費", tuition), ("雜費", misc), ("代收代辦費", extra)):
            fees.append({"name": name, "year": 2024, "item": item,
                         "amount": float(amount), "period": "每學期",
                         "source": "新北市教育局收費公告(示範資料)"})

        # ---------- 決算逐筆金額 ----------
        expected_annual = (tuition + misc + extra) * 2 * enrolled
        gap = 0.0
        if profile == "gap":
            gap = rng.uniform(0.18, 0.30)
        elif profile == "multi":
            gap = rng.uniform(0.26, 0.42)
        else:
            gap = rng.uniform(-0.06, 0.08)

        fabricate = profile in ("benford", "multi")

        for year in (2023, 2024):
            year_scale = 1.0 if year == 2024 else rng.uniform(0.86, 0.98)
            tuition_pool = expected_annual * (1 - gap) * year_scale
            total_income = tuition_pool / 0.73  # 學雜費約占七成，其餘為補助等
            # 人事費占比
            if profile in ("staff", "multi"):
                hr_ratio = rng.uniform(0.30, 0.40)   # 異常偏低
            else:
                hr_ratio = rng.uniform(0.52, 0.63)
            total_expense = total_income * rng.uniform(0.88, 0.99)

            for subject, share in INCOME_SUBJECTS:
                target = total_income * share * rng.uniform(0.92, 1.08)
                _emit_lines(financials, rng, name, year, "income", subject, target, fabricate)
            for subject, share in EXPENSE_SUBJECTS:
                s = hr_ratio if subject == "人事費" else share * (1 - hr_ratio) / 0.45
                target = total_expense * s * rng.uniform(0.93, 1.07)
                _emit_lines(financials, rng, name, year, "expense", subject, target, fabricate)

        # ---------- 裁罰紀錄 ----------
        n_pen = {"clean": 0, "gap": 1, "benford": 0, "staff": 1, "buzz": 1,
                 "ratio2y": 1, "compl": 2, "multi": 3}[profile]
        if profile == "clean" and rng.random() < 0.18:
            n_pen = 1
        used = set()
        for _ in range(n_pen):
            reason, law, amount, sev = rng.choice(PENALTY_REASONS)
            date = f"202{rng.randint(3,5)}-{rng.randint(1,12):02d}-{rng.randint(1,28):02d}"
            if (reason, date) in used:
                continue
            used.add((reason, date))
            penalties.append({"name": name, "date": date, "reason": reason, "law": law,
                              "amount": float(amount), "severity": sev,
                              "source": "新北市教育局裁罰公告(示範資料)"})

        # ---------- 社群輿情 ----------
        if profile in ("buzz", "multi"):
            n_neg, n_mild, n_pos = rng.randint(5, 8), rng.randint(1, 3), rng.randint(0, 2)
        elif profile in ("gap", "staff", "ratio2y", "compl"):
            n_neg, n_mild, n_pos = rng.randint(1, 3), rng.randint(1, 3), rng.randint(2, 4)
        else:
            n_neg, n_mild, n_pos = rng.randint(0, 1), rng.randint(0, 2), rng.randint(3, 6)

        pool = (
            [(t, "neg") for t in rng.sample(NEG_TEMPLATES, min(n_neg, len(NEG_TEMPLATES)))]
            + [(t, "mild") for t in rng.choices(MILD_TEMPLATES, k=n_mild)]
            + [(t, "pos") for t in rng.choices(POS_TEMPLATES, k=n_pos)]
        )
        for j, (text, kind) in enumerate(pool):
            platform = rng.choice(["PTT", "Dcard", "Google評論", "新聞"])
            days_ago = rng.randint(1, 75) if kind == "neg" else rng.randint(1, 300)
            posts.append({
                "name": name,
                "platform": platform,
                "days_ago": days_ago,
                "url": f"https://example.org/{platform.lower()}/{_stable_id(name, j)}",
                "author": rng.choice(["匿名家長", "路過的爸爸", "新莊媽媽", "在地居民", "記者"]),
                "content": f"{base}{inst_type}｜{text}",
            })

    return {
        "institutions": institutions,
        "fees": fees,
        "financials": financials,
        "penalties": penalties,
        "posts": posts,
    }


def _staffing(
    rng: random.Random, profile: str, inst_type: str, enrolled: int, public_affiliated: bool
) -> dict[str, Any]:
    """依年齡層產生班級數與教保服務人員配置。

    幼照法第16條第4項是逐班分級規定，2歲專班實質為 1:8、3歲以上為 1:15，
    且兩者不得混齡。因此人員配置必須「分開算」——把兩個年齡層合併平均會
    把 2 歲專班的缺口稀釋掉（ratio2y 樣態就是專門用來驗證這件事）。
    """
    if inst_type == "托嬰中心":
        # 每收托 5 名應置專任托育人員 1 人（兒少機構設置標準§25）
        need = math.ceil(enrolled / 5)
        staff = need - rng.randint(1, 3) if profile in ("staff", "multi") else need + rng.choice([0, 0, 1])
        staff = max(1, staff)
        return {
            "enrolled_2y": 0, "enrolled_3to5": 0,
            "classes_2y": 0, "classes_3to5": 0, "classes_5y": 0,
            "staff_2y": 0, "staff_3to5": 0,
            "staff_count": staff,
            "teacher_count": 0,
            "assistant_count": 0,
        }

    # 是否設 2 歲專班
    has_2y = rng.random() < 0.55 or profile == "ratio2y"
    e2 = rng.randint(9, 32) if has_2y else 0
    e2 = min(e2, max(0, enrolled - 20))
    e35 = enrolled - e2

    # 班級數：合法下限為 ceil(人數/每班上限)
    c2 = math.ceil(e2 / 16) if e2 else 0
    c35 = math.ceil(e35 / 30) if e35 else 0
    if profile not in ("compl", "multi"):
        c35 += rng.choice([0, 0, 1])          # 多開班是合法且常見的
    c5 = max(1, c35 // 2) if e35 >= 15 else (1 if e35 else 0)

    need2 = math.ceil(e2 / 8) if e2 else 0
    need35 = math.ceil(e35 / 15) if e35 else 0

    if profile == "ratio2y":
        # 3 歲以上配置寬裕、2 歲專班短缺 → 合併平均看起來合格
        s2 = max(1, need2 - rng.randint(1, 2))
        s35 = need35 + rng.randint(1, 3)
    elif profile in ("staff", "multi"):
        s2 = max(1, need2 - rng.randint(0, 1)) if e2 else 0
        s35 = max(1, need35 - rng.randint(1, 3))
    else:
        s2 = need2 + rng.choice([0, 0, 1]) if e2 else 0
        s35 = need35 + rng.choice([0, 0, 1])

    staff = s2 + s35
    if public_affiliated and profile not in ("compl", "multi", "staff"):
        staff += 1                            # 公立學校附設每園再增置1人（§16Ⅴ）

    # 助理教保員不得超過教保服務人員總數 1/3（§17Ⅱ）
    cap_assistant = math.floor(staff / 3)
    if profile in ("compl", "multi"):
        assistants = cap_assistant + rng.randint(1, 2)
    else:
        assistants = rng.randint(0, max(0, cap_assistant))

    # 5歲以上班級每班應有1人以上為幼兒園教師（§17Ⅰ）
    if profile in ("compl", "multi"):
        teachers = max(0, c5 - rng.randint(1, 2))
    else:
        teachers = c5 + rng.choice([0, 0, 1])
    teachers = min(teachers, max(0, staff - assistants))

    return {
        "enrolled_2y": e2, "enrolled_3to5": e35,
        "classes_2y": c2, "classes_3to5": c35, "classes_5y": c5,
        "staff_2y": s2, "staff_3to5": s35,
        "staff_count": staff,
        "teacher_count": teachers,
        "assistant_count": assistants,
    }


def _facility_and_ops(
    rng: random.Random, profile: str, inst_type: str, enrolled: int, is_public: bool
) -> dict[str, Any]:
    """護理人員、廚工、幼童專用車、團保、收費報備、空間與樓層。"""
    bad = profile in ("compl", "multi")

    # 護理人員：201人以上須專任（§17Ⅳ）
    if enrolled >= 201:
        nurse = "兼任" if bad else "專任"
    elif enrolled >= 61:
        nurse = "無" if bad else rng.choice(["特約", "兼任", "專任"])
    else:
        nurse = "無" if bad and rng.random() < 0.5 else rng.choice(["特約", "兼任"])

    unit = 72 if is_public else 120
    need_cook = max(1, math.ceil(enrolled / unit))
    cooks = max(0, need_cook - 1) if bad else need_cook

    has_bus = rng.random() < 0.45
    bus_count = rng.randint(1, 3) if has_bus else 0
    if not has_bus:
        bus_year, escort = None, None
    elif bad or profile == "staff":
        bus_year = rng.randint(2011, 2015)          # 車齡逾10年（§31）
        escort = 0 if bad else 1
    else:
        bus_year = rng.randint(2017, 2024)
        escort = 1

    out: dict[str, Any] = {
        "nurse_type": nurse,
        "cook_count": cooks,
        "bus_count": bus_count,
        "bus_oldest_year": bus_year,
        "bus_has_escort": escort,
        "has_group_insurance": 0 if bad and rng.random() < 0.6 else 1,
        # 私立收費數額應於每學年度開始前（8/30）對外公布並報備查（§43Ⅲ）
        "fee_filed_date": None if bad else f"2024-0{rng.randint(6, 8)}-{rng.randint(10, 28)}",
        "outdoor_separated_2y": 0 if bad else 1,
    }

    if inst_type == "托嬰中心":
        if bad:
            out["indoor_area"] = round(enrolled * rng.uniform(1.3, 1.8), 1)
            out["outdoor_area"] = round(enrolled * rng.uniform(0.6, 1.2), 1)
            out["floor_max"] = 4
        else:
            out["indoor_area"] = round(max(45.0, enrolled * rng.uniform(2.2, 3.0)), 1)
            out["outdoor_area"] = round(max(20.0, enrolled * rng.uniform(1.6, 2.4)), 1)
            out["floor_max"] = rng.randint(1, 3)
    return out


def _emit_lines(
    out: list[dict[str, Any]],
    rng: random.Random,
    name: str,
    year: int,
    flow: str,
    subject: str,
    target: float,
    fabricate: bool,
) -> None:
    """把一個會計科目的年度總額拆成多筆明細，供 Benford 檢定使用。"""
    remaining = target
    n = rng.randint(8, 16)
    for k in range(n):
        if remaining <= 0:
            break
        if k == n - 1:
            amount = round(remaining, 0)
        else:
            portion = remaining * rng.uniform(0.04, 0.28)
            if fabricate:
                amount = _fabricated_amount(rng, max(5000, portion * 0.5), max(15000, portion * 1.5))
            else:
                lo = math.log10(max(1000.0, portion * 0.35))
                hi = math.log10(max(2000.0, portion * 1.8))
                amount = _benford_amount(rng, lo, hi)
            amount = min(amount, remaining)
        if amount < 100:
            continue
        remaining -= amount
        out.append({"name": name, "year": year, "flow": flow, "subject": subject,
                    "amount": float(amount), "source": "決算報告PDF(示範資料)"})
