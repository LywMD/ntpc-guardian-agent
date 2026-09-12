"""文字型 PDF 解析（新北市公校決算書）。

這批決算書是文字型 PDF（每冊約 750 頁），可直接用 pdfplumber 取文字與表格，
不必走 OCR。內容是全市教育局決算，幼兒園相關資料散落在其中，因此策略是：

  1. 全書掃一遍，標記出提到幼兒園／教保／非營利的頁面
  2. 只對這些頁面抽表格（科目／預算數／決算數／比較增減／說明）
  3. 另外抽出「補助標準」類的金額敘述（例如教保費每月最高 3,500 元），
     這些是交叉比對工具判斷收費合理性的重要參照
  4. 抽出機構名稱線索（XX國小附設幼兒園、XX非營利幼兒園）

注意：決算書是「全市層級」的預算執行資料，不是逐園財報。
      因此它的定位是政策與補助基準來源，不能當成單一機構的財務報表。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("guardian.ingest.pdf_text")

# 命中即視為與幼兒園相關的頁面
RELEVANT = ("幼兒園", "教保", "非營利幼兒園", "托嬰", "幼兒教育", "準公共")

# 機構名稱樣式
INST_PATTERNS = [
    re.compile(r"([\u4e00-\u9fff]{2,10}(?:國民小學|國小|國中|高中|高級中學)附設(?:幼兒園|公立幼兒園))"),
    re.compile(r"((?:新北市)?[\u4e00-\u9fff]{2,8}非營利幼兒園)"),
    re.compile(r"(新北市立?[\u4e00-\u9fff]{2,10}幼兒園)"),
]

# 決算書是連續排版，正則容易跨行誤抓（例如「及設備辦理非營利幼兒園」）。
# 名稱裡出現這些字就判定為誤抓片段。
NAME_NOISE = (
    "辦理", "新建", "設備", "津貼", "補助", "計畫", "經費", "委託", "增設", "改建",
    "擴建", "整建", "修繕", "招收", "核定", "評鑑", "輔導", "獎勵", "研習", "推動",
    "及", "等", "或", "暨", "與", "本市", "全市", "所",
)


def is_valid_institution_name(name: str) -> bool:
    """過濾跨行誤抓的機構名稱片段。"""
    n = (name or "").strip()
    if not (5 <= len(n) <= 20):
        return False
    if not n.endswith("幼兒園"):
        return False
    core = n.replace("新北市", "").replace("立", "", 1)
    if len(core) < 4:
        return False
    # 名稱主體（去掉類型後綴）不該含有動詞或連接詞
    head = n[:-3]
    for bad in NAME_NOISE:
        if bad in head:
            return False
    return True

# 決算表常見欄位標題
TABLE_HEADER_HINTS = ("科目", "預算數", "決算數", "比較增減", "說明", "本年度", "上年度")

# 補助／收費標準的敘述樣式
FEE_HINT = re.compile(
    r"(教保費|學費|雜費|代辦費|補助|收費|月費|減免|免學費)[^。；\n]{0,80}?"
    r"([\d,]+(?:\s*萬)?(?:\s*[\d,]+)?)\s*元")

_NUM = re.compile(r"-?[\d,]+(?:\.\d+)?")


def parse_amount_zh(text: str) -> float | None:
    """解析中文金額表述。

    支援 "280,000"、"1,006 萬 963 元"、"3,500元"、"(628,120)" 等寫法。
    括號一律視為負數（會計慣例）。
    """
    if not text:
        return None
    s = text.strip().replace(" ", "").replace("　", "")
    negative = s.startswith("(") and s.endswith(")") or s.startswith("△") or s.startswith("-")
    s = s.strip("()（）△-")
    total = 0.0
    matched = False

    # 億 / 萬 / 千 的組合式寫法
    for unit, mult in (("億", 1e8), ("萬", 1e4)):
        if unit in s:
            head, s = s.split(unit, 1)
            m = _NUM.search(head)
            if m:
                total += float(m.group(0).replace(",", "")) * mult
                matched = True
    m = _NUM.search(s)
    if m:
        total += float(m.group(0).replace(",", ""))
        matched = True
    if not matched:
        return None
    return -total if negative else total


def _roc_year(name: str) -> int | None:
    m = re.search(r"(\d{3})\s*年", name)
    if m:
        return int(m.group(1))
    return None


def roc_to_ad(roc: int | None) -> int | None:
    return roc + 1911 if roc else None


def _classify_flow(subject: str) -> str:
    s = subject or ""
    if any(k in s for k in ("收入", "收益", "補助收入", "學費", "雜費", "代辦費", "捐贈")):
        return "income"
    if any(k in s for k in ("支出", "費用", "成本", "人事費", "業務費", "設備", "維護")):
        return "expense"
    return "unknown"


def _parse_table(rows: list[list[str | None]], page: int) -> list[dict[str, Any]]:
    """把 pdfplumber 的表格轉成結構化列。"""
    out: list[dict[str, Any]] = []
    if not rows:
        return out

    # 找標題列
    header_idx = 0
    for i, r in enumerate(rows[:4]):
        joined = "".join(c or "" for c in r)
        if sum(1 for h in TABLE_HEADER_HINTS if h in joined) >= 2:
            header_idx = i
            break
    header = [(c or "").replace("\n", "").strip() for c in rows[header_idx]]

    def col_of(*names: str) -> int | None:
        for idx, h in enumerate(header):
            if any(n in h for n in names):
                return idx
        return None

    c_subject = col_of("科目", "項目", "名稱") or 0
    c_budget = col_of("預算數", "預算")
    c_actual = col_of("決算數", "決算", "實支", "實收")
    c_diff = col_of("比較增減", "增減")
    c_note = col_of("說明", "備註")

    for r in rows[header_idx + 1:]:
        cells = [(c or "").replace("\n", " ").strip() for c in r]
        if not any(cells):
            continue
        subject = cells[c_subject] if c_subject < len(cells) else ""
        if not subject or len(subject) > 60:
            continue
        row: dict[str, Any] = {
            "page": page,
            "subject": subject,
            "flow": _classify_flow(subject),
        }
        for key, ci in (("budget", c_budget), ("actual", c_actual), ("diff", c_diff)):
            if ci is not None and ci < len(cells):
                row[key] = parse_amount_zh(cells[ci])
        if c_note is not None and c_note < len(cells):
            note = cells[c_note]
            if note:
                row["note"] = note[:200]
        # 至少要有一個金額才留下
        if any(row.get(k) is not None for k in ("budget", "actual", "diff")):
            out.append(row)
    return out


def parse_settlement(path: Path, max_pages: int | None = None,
                     on_progress: Callable[[int, int], None] | None = None
                     ) -> dict[str, Any]:
    """解析一冊決算書。"""
    import pdfplumber

    roc = _roc_year(path.name)
    result: dict[str, Any] = {
        "file": path.name,
        "path": str(path),
        "year_roc": roc,
        "year_ad": roc_to_ad(roc),
        "total_pages": 0,
        "relevant_pages": [],
        "institutions": {},
        "table_rows": [],
        "fee_standards": [],
        "keyword_pages": {},
    }
    inst_hits: dict[str, list[int]] = {}
    rejected: dict[str, int] = {}
    kw_pages: dict[str, list[int]] = {k: [] for k in RELEVANT}

    with pdfplumber.open(path) as pdf:
        n = len(pdf.pages)
        if max_pages:
            n = min(n, max_pages)
        result["total_pages"] = len(pdf.pages)

        for i in range(n):
            page_no = i + 1
            if on_progress:
                on_progress(page_no, n)
            pg = pdf.pages[i]
            try:
                text = pg.extract_text() or ""
            except Exception as exc:  # noqa: BLE001
                log.debug("第 %d 頁取文字失敗：%s", page_no, exc)
                continue
            if not text:
                continue

            hit = [k for k in RELEVANT if k in text]
            for k in hit:
                kw_pages[k].append(page_no)
            if not hit:
                continue
            result["relevant_pages"].append(page_no)

            # 機構名稱（濾掉跨行誤抓的片段）
            for pat in INST_PATTERNS:
                for m in pat.finditer(text):
                    name = m.group(1).strip()
                    if is_valid_institution_name(name):
                        inst_hits.setdefault(name, []).append(page_no)
                    else:
                        rejected.setdefault(name, 0)
                        rejected[name] += 1

            # 補助／收費標準敘述
            for m in FEE_HINT.finditer(text):
                amt = parse_amount_zh(m.group(2))
                if amt and amt >= 100:
                    result["fee_standards"].append({
                        "page": page_no,
                        "category": m.group(1),
                        "amount": amt,
                        "context": text[max(0, m.start() - 40): m.end() + 40]
                                   .replace("\n", " "),
                    })

            # 表格
            try:
                for tbl in pg.extract_tables():
                    result["table_rows"].extend(_parse_table(tbl, page_no))
            except Exception as exc:  # noqa: BLE001
                log.debug("第 %d 頁抽表格失敗：%s", page_no, exc)

    result["institutions"] = {k: sorted(set(v)) for k, v in
                              sorted(inst_hits.items(), key=lambda kv: -len(kv[1]))}
    result["rejected_name_fragments"] = dict(
        sorted(rejected.items(), key=lambda kv: -kv[1])[:20])
    result["keyword_pages"] = {k: v for k, v in kw_pages.items() if v}
    result["table_row_count"] = len(result["table_rows"])
    # 去重補助標準（同一數字同一類別只留第一次）
    seen: set[tuple[str, float]] = set()
    dedup = []
    for f in result["fee_standards"]:
        key = (f["category"], f["amount"])
        if key in seen:
            continue
        seen.add(key)
        dedup.append(f)
    result["fee_standards"] = dedup
    return result
