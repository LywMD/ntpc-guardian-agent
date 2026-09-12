"""公校決算書的「逐園分決算」解析器。

背景
    新北市總決算的教育局主管部分共 5 冊。第 1–4 冊是全市層級彙總
    （政府撥入收入 173 億這種數字），**不能**歸給任何單一機構；
    但第 5 冊收錄「附屬單位決算之分決算」——每一間市立幼兒園各自的
    收入支出表、決算與會計收支對照表、基金來源用途明細表、現金流量表。
    那才是逐園的真實財務資料。

    這個模組只處理逐園頁面，因此與 pdf_text.parse_settlement（全市層級：
    名冊 + 補助基準）互補，兩者輸出會分別載入。

報表 role 指派（避免重複計算）
    收入支出表              primary   ← 唯一用來算年度合計與比率的來源，且含上年度比較數
    決算與會計收支對照表    budget    ← 決算數 vs 會計收支，與 primary 涵蓋同一筆錢
    基金來源／用途明細表    detail    ← 只供 Benford 逐筆數字檢定
    資本資產明細表          balance
    現金流量表              balance
    總說明／目次            略過

    同一間園同一年度若同時抓到 primary 與 budget，合計只採 primary。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("guardian.ingest.pdf_units")

# 逐園頁面的機構名樣式。決算書用全名，含「新北市立」前綴。
INST_PAT = re.compile(
    r"新北市立[\u4e00-\u9fff]{2,4}幼兒園"
    r"|[\u4e00-\u9fff]{2,8}(?:國民小學|國小|國民中學|國中)附設幼兒園")

# 報表標題 → role。
#
# 每間園每年約 22 頁，其中「收入支出表」「基金來源、用途及餘絀表」
# 「決算與會計收支對照表」三張涵蓋同一筆錢，只能挑一張算合計。
# 這裡以「收入支出表」為 primary，因為它同一張表就含本年度與上年度金額，
# 年增率可直接算；另兩張分別標為 primary_alt 與 budget，不納入合計。
# 若某園缺「收入支出表」，載入端會把 primary_alt 升為 primary（見 loader）。
#
# 注意標題會跨行截斷（例如「資本資產明細表」在頁首只剩「產明細表」），
# 因此比對用的關鍵字要避免依賴完整標題。
STATEMENT_ROLES: list[tuple[str, str]] = [
    ("收入支出表", "primary"),
    ("基金來源、用途及餘絀表", "primary_alt"),
    ("基金來源用途及餘絀表", "primary_alt"),
    ("基金來源、用途及餘絀", "primary_alt"),
    ("決算與會計收支對照表", "budget"),
    ("主要業務計畫執行績效摘要表", "budget"),
    ("基金來源明細表", "detail"),
    ("基金用途明細表", "detail"),
    ("各項費用彙計表", "detail"),
    ("用人費用彙計表", "hr_detail"),
    ("管制性項目及統計所需項目比較表", "detail"),
    ("員工人數彙計表", "headcount"),
    ("增購及汰舊換新管理用公務車輛明細表", "vehicle"),
    ("資本資產明細表", "balance"),
    ("產明細表", "balance"),          # 標題跨行截斷後的殘餘形式
    ("固定資產", "balance"),
    ("現金流量表", "balance"),
    ("資產負債表", "balance"),
    ("淨值變動表", "balance"),
    ("平衡表", "balance"),
]
_SKIP_TITLES = ("總說明", "總 說 明", "目次", "目 次", "審核報告", "決算書封面",
                "新 北 市 總 決 算", "附屬單位決算之分決算")

# 「收入」「支出」段落切換
_INCOME_HEAD = ("收入", "基金來源")
_EXPENSE_HEAD = ("支出", "基金用途", "用途")

SUBTOTAL_MARKERS = ("合計", "小計", "總計", "共計", "合  計", "餘絀", "賸餘", "短絀")

_NUM_TOKEN = re.compile(r"^-?[\d,]+(?:\.\d+)?$")

# 頁首／表頭的年度與單位標註列。若不排除，「中華民國 112 年度」會被當成
# 一筆金額 112 的資料列寫進財務明細。
_NOISE_LINE = re.compile(r"中\s*華\s*民\s*國|單位[：:]|貨幣單位|年\s*度\s*$|^第\s*\d+\s*頁")


def _split_row(line: str) -> tuple[str, list[float]] | None:
    """把「科目 數字 數字 …」拆成 (科目, 數字清單)。

    刻意不用單一正規表達式比對整列：科目字元類若包含空白，會與分隔用的
    \\s+ 重疊而產生災難性回溯，決算書那種一列七八個數字的表格會讓
    比對時間爆炸（實測整份 724 頁跑不完）。改成先分詞再判斷型別，
    複雜度是線性的。
    """
    parts = line.split()
    if len(parts) < 2:
        return None
    # 找出中間那段連續的數字 token。
    # 不能只從尾端往前找：「決算與會計收支對照表」最後一欄是文字
    #   用人費用  7,897,081  7,897,081  人事支出
    # 尾端是中文時整列就會被丟掉。
    start = None
    for i, tok in enumerate(parts):
        if _NUM_TOKEN.match(tok):
            start = i
            break
    if start is None or start == 0:
        return None
    end = start
    while end < len(parts) and _NUM_TOKEN.match(parts[end]):
        end += 1
    nums: list[float] = []
    for tok in parts[start:end]:
        v = _clean_num(tok)
        if v is None:
            return None
        nums.append(v)
    if not nums:
        return None
    subject = "".join(parts[:start]).strip()
    if not (2 <= len(subject) <= 24):
        return None
    # 科目必須含中文，排除純數字或代號列
    if not re.search(r"[\u4e00-\u9fff]", subject):
        return None
    return subject, nums

_ROC_YEAR = re.compile(r"中\s*華\s*民\s*國\s*(\d{2,3})\s*年度")
_UNIT_THOUSAND = re.compile(r"單位[：:]\s*新?臺?台?幣?\s*千元")
# 員工人數彙計表的單位是「人」，不是金額，必須分開處理，
# 否則人數會被當成金額寫進 financials，把合計與 Benford 全部污染。
_UNIT_PERSON = re.compile(r"單位[：:]\s*人")

# 員工人數彙計表裡代表「教保服務人員」與「合計」的列
_STAFF_TOTAL_KEYS = ("合計", "總計", "小計")
_TEACHER_KEYS = ("教保員", "教保服務人員", "教師", "園長", "助理教保員",
                 "職員", "技工", "工友", "廚工", "約僱", "聘用")


def _clean_num(s: str) -> float | None:
    try:
        v = float((s or "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return v


def _statement_of(head: str) -> tuple[str | None, str | None]:
    """從頁首判斷報表名稱與 role。"""
    for title, role in STATEMENT_ROLES:
        if title in head:
            return title, role
    for t in _SKIP_TITLES:
        if t in head:
            return t, "skip"
    return None, None


def _is_subtotal(subject: str) -> bool:
    return any(m in (subject or "") for m in SUBTOTAL_MARKERS)


def _default_flow(statement: str | None) -> str:
    """由報表名稱推定該表的收支性質。"""
    s = statement or ""
    if any(k in s for k in ("用途", "費用", "支出")):
        return "expense"
    if any(k in s for k in ("來源", "收入")):
        return "income"
    return "income"


def _flow_of(subject: str, current: str) -> str:
    """依段落標題推定收支方向。

    決算書的收入與支出分段列示，段落標題本身就是「收入」「支出」
    或「基金來源」「基金用途」，因此以最近一次出現的段落標題為準。
    """
    s = subject or ""
    if any(h == s or s.startswith(h) for h in _EXPENSE_HEAD):
        return "expense"
    if any(h == s or s.startswith(h) for h in _INCOME_HEAD):
        return "income"
    return current


def _parse_headcount(text: str, unit: dict[str, Any], page_no: int) -> None:
    """從員工人數彙計表抽出人員數。

    這張表的單位是「人」而非金額，所以獨立處理——若混進財務明細，
    人數會被當成金額參與合計與 Benford 檢定，結果會完全失真。

    公校幼兒園原本沒有任何人員數欄位，法規遵循的師生比項目全部落在
    unverifiable；有了這張表就能實際檢核。
    """
    hc = unit.setdefault("headcount", {})
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # 形如「教保員 12 12 0」或「合 計 25 24 1」：取第一個整數為本年度人數
        parsed = _split_row(line)
        if not parsed:
            continue
        label, nums = parsed
        if not nums or nums[0] != int(nums[0]):
            continue
        n = int(nums[0])
        if n > 2000:                      # 不合理的大數，應該是金額誤入
            continue
        if any(k in label for k in _STAFF_TOTAL_KEYS):
            hc["total"] = n
            hc.setdefault("_source_page", page_no)
        elif any(k in label for k in _TEACHER_KEYS):
            hc[label] = n
            hc.setdefault("_source_page", page_no)


def parse_unit_settlements(
    path: str | Path,
    on_progress: Callable[[int, int], None] | None = None,
    pages: list[int] | None = None,
) -> dict[str, Any]:
    """抽出一冊決算書裡所有逐園分決算的收支明細。

    pages：只解析指定頁碼（1-based），供開發與驗證時針對特定頁快速核對，
    不必為了確認幾個數字重掃整冊 700 多頁。
    """
    try:
        import pdfplumber
    except ImportError:
        return {"error": "pdfplumber 未安裝"}

    path = Path(path)
    result: dict[str, Any] = {
        "file": path.name,
        "path": str(path),
        "kind": "public_unit_settlement",
        "total_pages": 0,
        "year_roc": None,
        "year_ad": None,
        "units": {},          # 機構名 -> {pages, statements, line_items}
        "unit_page_count": 0,
        "line_item_count": 0,
    }

    with pdfplumber.open(str(path)) as pdf:
        n = len(pdf.pages)
        result["total_pages"] = n
        targets = ([p for p in pages if 1 <= p <= n] if pages
                   else list(range(1, n + 1)))
        for page_no in targets:
            i = page_no - 1
            if on_progress:
                on_progress(page_no, n)
            try:
                text = pdf.pages[i].extract_text() or ""
            except Exception as exc:  # noqa: BLE001
                log.debug("第 %d 頁取文字失敗：%s", page_no, exc)
                continue
            if not text:
                continue

            if result["year_roc"] is None:
                m = _ROC_YEAR.search(text)
                if m:
                    result["year_roc"] = int(m.group(1))
                    result["year_ad"] = int(m.group(1)) + 1911

            names = set(INST_PAT.findall(text))
            if len(names) != 1:
                continue                      # 只處理「單一機構」的頁面
            inst = next(iter(names))

            lines = text.splitlines()
            head = "\n".join(lines[:4])
            statement, role = _statement_of(head)
            if role in (None, "skip"):
                continue

            unit = result["units"].setdefault(
                inst, {"pages": [], "statements": {}, "line_items": [],
                       "headcount": {}})
            unit["pages"].append(page_no)
            unit["statements"][statement] = unit["statements"].get(statement, 0) + 1
            result["unit_page_count"] += 1

            # 員工人數彙計表：抽人員數，不寫進財務明細
            if role == "headcount" or _UNIT_PERSON.search(text):
                _parse_headcount(text, unit, page_no)
                continue

            unit_mult = 1000.0 if _UNIT_THOUSAND.search(text) else 1.0
            # 報表名稱本身就決定了性質：用途／費用類報表整張都是支出，
            # 不能預設 income 再等段落標題來翻轉——那些表往往沒有段落標題列，
            # 結果支出會被整批誤標成收入。
            flow = _default_flow(statement)
            is_income_expense = statement == "收入支出表"
            seen_rows: set[tuple[str, float]] = set()
            for raw in lines:
                line = raw.strip()
                if not line or "科目" in line or "預算項目" in line:
                    continue
                if _NOISE_LINE.search(line):
                    continue          # 年度／單位標註列，不是資料
                parsed = _split_row(line)
                if not parsed:
                    continue
                subject, nums = parsed
                # 收入支出表的大類與其唯一細項常同名同額（人事支出／業務支出），
                # 不去重會讓同一筆錢在 Benford 檢定裡被算兩次。
                key = (subject, nums[0])
                if key in seen_rows:
                    continue
                seen_rows.add(key)
                flow = _flow_of(subject, flow)

                amount = nums[0] * unit_mult
                prior = None
                # 收入支出表欄位順序：本年度金額、本年度%、上年度金額、上年度%、
                # 比較增減金額、比較增減%。取第 3 個數字為上年度金額。
                if is_income_expense and len(nums) >= 4:
                    prior = nums[2] * unit_mult

                unit["line_items"].append({
                    "page": page_no, "statement": statement, "role": role,
                    "subject": subject, "flow": flow,
                    "amount": amount, "prior_amount": prior,
                    "is_subtotal": _is_subtotal(subject),
                })
                result["line_item_count"] += 1

    result["unit_count"] = len(result["units"])
    result["units_with_headcount"] = sum(
        1 for u in result["units"].values() if (u.get("headcount") or {}).get("total"))
    return result
