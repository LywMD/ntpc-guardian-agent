"""掃描影像型 PDF 的結構化抽取（Bedrock 視覺模型 OCR）。

為什麼不用 Amazon Textract
  Textract 的 DetectDocumentText / AnalyzeDocument 目前不支援繁體中文，
  對這批財報會抽不出東西。改用 Bedrock 的視覺模型（Claude Sonnet 4.5）
  直接讀頁面影像，繁中表格辨識準確度足夠，而且可以一次要求輸出結構化 JSON。

兩段式流程（控制成本與時間）
  Pass 1 定位：低解析度（90 DPI）批次送入，只做頁面分類，
              找出哪幾頁是收支表／資產負債表／財產目錄。
  Pass 2 抽取：只對命中的頁面用較高解析度（200 DPI）逐頁抽取明細。

所有結果逐頁快取在 data/raw/ocr/ 之下，重跑不會重複花 token。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Iterable

from .. import aws, config

log = logging.getLogger("guardian.ingest.ocr")

OCR_CACHE = config.RAW_DIR / "ocr"
CLASSIFY_DPI = 90
EXTRACT_DPI = 200
BATCH_SIZE = 4          # Pass 1 每次送幾頁（Converse 單次最多 20 張圖）
MAX_IMAGE_BYTES = 3_600_000

# 頁面類型的抽取優先序。
# 高優先：直接餵給鑑識會計三項訊號（比率／Benford／交叉比對）的資料來源。
# 低優先：財產目錄動輒 9 頁以上，對風險訊號幫助有限，預設不抽以節省 OCR 成本，
#         需要時用 include_low_priority=True 開啟。
WANTED_HIGH = {"收支表", "人事費用", "預算執行", "資產負債表"}
WANTED_LOW = {"財產目錄", "現金流量表", "淨值變動表"}
WANTED = WANTED_HIGH | WANTED_LOW


# ------------------------------------------------------------------ 影像
def page_count(path: Path) -> int:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(path))
    try:
        return len(doc)
    finally:
        doc.close()


def render_page(path: Path, page_index: int, dpi: int = EXTRACT_DPI) -> bytes:
    """把 PDF 單頁轉成 PNG bytes。page_index 為 0-based。"""
    import io

    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(path))
    try:
        page = doc[page_index]
        bitmap = page.render(scale=dpi / 72.0)
        img = bitmap.to_pil()
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
        # 太大就降尺寸重存，避免超過 Bedrock 單張上限
        scale = 0.75
        while len(data) > MAX_IMAGE_BYTES and scale > 0.3:
            w, h = int(img.width * scale), int(img.height * scale)
            buf = io.BytesIO()
            img.resize((w, h)).save(buf, format="PNG", optimize=True)
            data = buf.getvalue()
            scale -= 0.15
        return data
    finally:
        doc.close()


# ------------------------------------------------------------------ 快取
def _doc_id(path: Path) -> str:
    st = path.stat()
    key = f"{path.name}|{st.st_size}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _cache_path(path: Path, kind: str, page: int | None = None) -> Path:
    d = OCR_CACHE / _doc_id(path)
    d.mkdir(parents=True, exist_ok=True)
    name = f"{kind}.json" if page is None else f"{kind}_p{page:04d}.json"
    return d / name


def _load(p: Path) -> Any | None:
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
    return None


def _save(p: Path, obj: Any) -> None:
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str),
                 encoding="utf-8")


# ------------------------------------------------------------------ Bedrock
def _converse_images(images: list[bytes], prompt: str, max_tokens: int = 16000,
                     retries: int = 3) -> tuple[str, str]:
    """回傳 (文字, stopReason)。stopReason == 'max_tokens' 代表被截斷。"""
    content: list[dict[str, Any]] = [
        {"image": {"format": "png", "source": {"bytes": b}}} for b in images
    ]
    content.append({"text": prompt})
    last: Exception | None = None
    for attempt in range(retries):
        try:
            resp = aws.bedrock_runtime().converse(
                modelId=aws.resolve_model_id(),
                messages=[{"role": "user", "content": content}],
                inferenceConfig={"maxTokens": max_tokens, "temperature": 0.0},
            )
            text = "".join(c.get("text", "")
                           for c in resp["output"]["message"]["content"])
            return text, resp.get("stopReason", "")
        except Exception as exc:  # noqa: BLE001
            last = exc
            wait = 2 ** attempt * 2
            log.warning("Bedrock 視覺呼叫失敗（第 %d 次），%d 秒後重試：%s",
                        attempt + 1, wait, exc)
            time.sleep(wait)
    raise RuntimeError(f"Bedrock 視覺呼叫連續失敗：{last}")


def _repair_truncated(s: str) -> str | None:
    """回覆被 maxTokens 截斷時，砍到最後一個完整元素再把括號補起來。

    這比整頁重抽便宜太多，而且已讀到的明細都是有效資料。
    """
    depth_stack: list[str] = []
    last_safe = None          # 在頂層陣列／物件中，最後一個完整元素結束的位置
    in_str = False
    esc = False
    for i, ch in enumerate(s):
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in "{[":
            depth_stack.append(ch)
        elif ch in "}]":
            if depth_stack:
                depth_stack.pop()
            if len(depth_stack) <= 2:
                last_safe = i
    if last_safe is None:
        return None
    head = s[: last_safe + 1]
    # 把還沒關的括號補上
    depth_stack = []
    in_str = False
    esc = False
    for ch in head:
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in "{[":
            depth_stack.append(ch)
        elif ch in "}]":
            if depth_stack:
                depth_stack.pop()
    closing = "".join("}" if c == "{" else "]" for c in reversed(depth_stack))
    return head.rstrip().rstrip(",") + closing


def _json_from(text: str) -> Any | None:
    """從模型回覆裡挖出 JSON，必要時修復被截斷的內容。"""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1)
    else:
        # 沒有結尾 fence（通常就是被截斷了）
        text = re.sub(r"^\s*```(?:json)?\s*", "", text)

    candidates: list[str] = []
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        candidates.append(m.group(0))
    m = re.search(r"\[[\s\S]*\]", text)
    if m:
        candidates.append(m.group(0))
    # 截斷情境：從第一個 { 或 [ 一路吃到結尾
    for opener in "{[":
        idx = text.find(opener)
        if idx >= 0:
            candidates.append(text[idx:])

    for cand in candidates:
        for attempt in (cand,
                        re.sub(r",\s*([}\]])", r"\1", cand),
                        _repair_truncated(cand) or ""):
            if not attempt:
                continue
            try:
                return json.loads(attempt)
            except json.JSONDecodeError:
                continue
    return None


# ------------------------------------------------------------------ Pass 1 定位
CLASSIFY_PROMPT = """你在協助稽查人員處理台灣的幼兒園財務報表／政府決算書（掃描影像）。

我給你 {n} 張連續頁面的影像，依序對應頁碼 {pages}。
請判斷每一頁的內容類型，只輸出 JSON 陣列，不要任何其他文字：

[{{"page": 頁碼, "type": "類型", "title": "頁面標題原文", "has_amounts": true/false,
   "institution": "頁面上出現的機構名稱，沒有就 null"}}]

type 只能是下列之一：
  封面、目錄、會計師查核報告、資產負債表、收支表、現金流量表、淨值變動表、
  財產目錄、預算執行、人事費用、附註、其他

判斷要點：
- 「收支表」包含收入與支出科目及金額，可能叫「收支餘絀表」「收支決算表」「損益表」
- has_amounts 指這一頁是否含有可讀的金額數字
- institution 請抓完整名稱，例如「新北市新樂非營利幼兒園」
"""


def classify_pages(path: Path, dpi: int = CLASSIFY_DPI, batch: int = BATCH_SIZE,
                   max_pages: int | None = None, on_progress=None) -> list[dict[str, Any]]:
    """Pass 1：低解析度批次分類每一頁。結果會快取。"""
    cache = _cache_path(path, "classify")
    cached = _load(cache)
    if cached:
        log.info("使用分類快取 %s（%d 頁）", path.name, len(cached))
        return cached

    n = page_count(path)
    if max_pages:
        n = min(n, max_pages)
    out: list[dict[str, Any]] = []
    for start in range(0, n, batch):
        idxs = list(range(start, min(start + batch, n)))
        pages = [i + 1 for i in idxs]
        if on_progress:
            on_progress("classify", pages[0], n)
        images = [render_page(path, i, dpi) for i in idxs]
        text, _stop = _converse_images(
            images, CLASSIFY_PROMPT.format(n=len(idxs), pages=pages), max_tokens=2000)
        parsed = _json_from(text)
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict) and "page" in item:
                    out.append(item)
        else:
            log.warning("%s 第 %s 頁分類回覆無法解析", path.name, pages)
            for p in pages:
                out.append({"page": p, "type": "解析失敗", "has_amounts": None})
    _save(cache, out)
    return out


def wanted_pages(classified: Iterable[dict[str, Any]],
                 include_low_priority: bool = False) -> list[int]:
    """挑出值得做高解析度抽取的頁碼（1-based）。"""
    allowed = WANTED if include_low_priority else WANTED_HIGH
    out = []
    for c in classified:
        t = (c.get("type") or "").strip()
        if t in allowed and c.get("has_amounts") is not False:
            try:
                out.append(int(c["page"]))
            except (TypeError, ValueError):
                continue
    return sorted(set(out))


# ------------------------------------------------------------------ Pass 2 抽取
# 刻意用「陣列」而不是逐欄位命名的物件：同樣的明細可省下約 60% 輸出 token，
# 大幅降低被 maxTokens 截斷的機率（截斷是這類長表格最主要的失敗原因）。
EXTRACT_PROMPT = """這是台灣幼兒園財務報表／政府決算書的一頁掃描影像（頁碼 {page}）。

把這一頁的表格完整轉成 JSON。只輸出 JSON，不要說明文字、不要 markdown 標記。

格式（items 用緊湊陣列以節省長度）：
{{
"page":{page},
"statement":"報表名稱原文",
"institution":"機構名稱或null",
"period":"會計期間原文",
"unit":"金額單位，例如 元 或 千元",
"cols":["subject","flow","amount","prior","budget","level"],
"items":[
 ["會計科目原文","income",1234567,890123,null,1],
 ["會計科目原文","expense",456789,null,null,2]
],
"totals":{{"income":數字或null,"expense":數字或null,"surplus":數字或null}},
"headcount":{{"children":幼兒人數或null,"staff":教保服務人員數或null}},
"confidence":0到1,
"unreadable":false
}}

規則
- flow 只能是 income / expense / asset / liability / net_asset
- amount、prior、budget 一律純數字，去掉逗號與貨幣符號；括號或△代表負數請輸出負值；空白給 null
- level 是科目層級，1 為大類、2 為子科目，依縮排判斷
- 科目原文照抄，不要翻譯或改寫
- 不要自己計算或推估任何沒印在頁面上的數字
- 看不清楚或這頁沒有金額表格：unreadable 設 true、items 給 []
"""

_ITEM_KEYS = ("subject", "flow", "amount", "prior_amount", "budget", "level")

# 模型會依實際表格結構回不同的欄位名（這是好事，比硬套簡化 schema 準）。
# 這裡統一正規化成內部欄位名。
_COL_ALIASES = {
    "subject": "subject", "科目": "subject", "項目": "subject", "name": "subject",
    "flow": "flow", "type": "flow", "類別": "flow",
    # 決算數／實際數才是我們要的 amount
    "amount": "amount", "actual": "amount", "決算數": "amount", "決算": "amount",
    "actual_amount": "amount", "實際": "amount", "本期": "amount",
    "current": "amount", "current_amount": "amount", "本年度": "amount",
    "prior": "prior_amount", "prior_amount": "prior_amount", "上期": "prior_amount",
    "previous": "prior_amount", "上年度": "prior_amount", "last_year": "prior_amount",
    "budget": "budget", "預算數": "budget", "預算": "budget",
    "difference": "diff", "diff": "diff", "比較增減": "diff", "增減": "diff",
    "variance": "diff",
    "execution_rate": "exec_rate", "執行率": "exec_rate", "rate": "exec_rate",
    "percent": "exec_rate", "%": "exec_rate",
    "level": "level", "層級": "level",
    "note": "note", "備註": "note", "說明": "note", "附註": "note",
}


# 欄位名常見的裝飾：決算數(a)、執行率(%)(d)=(b)/(a)、amount_113、本年度決算數…
# 先剝掉這些雜訊再比對，否則別名表永遠對不上。
_COL_NOISE = re.compile(r"[（(\[].*?[)\]）]|[=%＝／/]|\s+|　")
_COL_YEAR = re.compile(r"(?:^|[_\-\s])(\d{3})(?:$|[_\-\s學年度])")

# 比對優先序：長字串先比，避免「預算數」被「算數」之類的短鍵搶走。
# 決算數必須比預算數先判定，否則含「算數」的欄位會誤配。
_COL_MATCH_ORDER = [
    ("決算數", "amount"), ("決算", "amount"), ("實際數", "amount"), ("實支數", "amount"),
    ("實收數", "amount"), ("本年度", "amount"), ("本期", "amount"),
    ("預算數", "budget"), ("預算", "budget"),
    ("上年度", "prior_amount"), ("上期", "prior_amount"), ("前期", "prior_amount"),
    ("比較增減", "diff"), ("增減", "diff"), ("差異", "diff"),
    ("執行率", "exec_rate"), ("達成率", "exec_rate"),
    ("科目", "subject"), ("項目", "subject"), ("名稱", "subject"),
    ("附註", "note"), ("備註", "note"), ("說明", "note"),
    ("層級", "level"), ("類別", "flow"),
    ("actual", "amount"), ("current", "amount"), ("amount", "amount"),
    ("prior", "prior_amount"), ("previous", "prior_amount"), ("last", "prior_amount"),
    ("budget", "budget"),
    ("difference", "diff"), ("variance", "diff"), ("diff", "diff"),
    ("rate", "exec_rate"), ("percent", "exec_rate"), ("pct", "exec_rate"),
    ("subject", "subject"), ("name", "subject"),
    ("flow", "flow"), ("type", "flow"),
    ("level", "level"), ("note", "note"),
]


def _canon_col(name: Any) -> str:
    """把各種欄位名正規化成內部欄位名，並保留年度標記（民國年）。

    例：'決算數(a)'          → 'amount'
        'amount_113'         → 'amount@113'
        'amount_113_決算數'   → 'amount@113'
        '執行率(%)(d)=(b)/(a)' → 'exec_rate'
    """
    raw = str(name or "").strip()
    if not raw:
        return raw
    year = None
    ym = _COL_YEAR.search(raw)
    if ym:
        y = int(ym.group(1))
        if 100 <= y <= 130:          # 民國年合理範圍
            year = y

    key = _COL_NOISE.sub("", raw)
    if year:
        key = re.sub(rf"[_\-]?{year}[_\-]?", "", key)
    lower = key.lower()

    canon = _COL_ALIASES.get(key) or _COL_ALIASES.get(lower)
    if not canon:
        for needle, target in _COL_MATCH_ORDER:
            if needle in key or needle in lower:
                canon = target
                break
    if not canon:
        canon = key or raw
    return f"{canon}@{year}" if (year and canon in ("amount", "budget", "prior_amount",
                                                    "diff", "exec_rate")) else canon


_EXPENSE_STATEMENTS = ("業務費", "材料費", "維護費", "修繕", "人事費", "設備",
                       "費用明細", "支出明細", "經費", "雜支")
_INCOME_STATEMENTS = ("收入明細", "代收代付收入")


def _default_flow(statement: str | None) -> str | None:
    """費用明細表整頁都是支出，但這種頁面常常沒有 flow 欄位，需由報表名稱推定。"""
    s = statement or ""
    if any(k in s for k in _INCOME_STATEMENTS):
        return "income"
    if any(k in s for k in _EXPENSE_STATEMENTS):
        return "expense"
    return None


def _split_year_columns(item: dict[str, Any], statement: str | None
                        ) -> list[dict[str, Any]]:
    """把 110-113 四年並排的寬表，拆成每個年度一筆。

    p27/p28 這類「各學年收支比較表」一列同時有 amount@113、amount@112…，
    不拆開就整頁抽不到金額，拆開反而能一次拿到多年歷史。
    """
    years: dict[int, dict[str, Any]] = {}
    base: dict[str, Any] = {}
    for k, v in item.items():
        if "@" in k:
            field, ys = k.split("@", 1)
            try:
                y = int(ys)
            except ValueError:
                continue
            years.setdefault(y, {})[field] = v
        else:
            base[k] = v
    if not years:
        return [base]
    out = []
    for y, fields in sorted(years.items(), reverse=True):
        row = dict(base)
        row.update(fields)
        row["roc_year"] = y
        out.append(row)
    return out


def _expand_items(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """把緊湊陣列格式還原成 dict 清單，正規化欄位名，並展開多年度寬表。"""
    statement = parsed.get("statement")
    raw_items: list[dict[str, Any]] = []

    if isinstance(parsed.get("line_items"), list) and parsed["line_items"]:
        first = parsed["line_items"][0]
        if isinstance(first, dict):
            raw_items = [{_canon_col(k): v for k, v in it.items()}
                         for it in parsed["line_items"] if isinstance(it, dict)]

    if not raw_items:
        cols = [_canon_col(c) for c in (parsed.get("cols") or list(_ITEM_KEYS))]
        for row in parsed.get("items") or []:
            if isinstance(row, dict):
                raw_items.append({_canon_col(k): v for k, v in row.items()})
            elif isinstance(row, list):
                raw_items.append({cols[i]: row[i]
                                  for i in range(min(len(cols), len(row)))})

    default_flow = _default_flow(statement)
    out: list[dict[str, Any]] = []
    for item in raw_items:
        if not item.get("subject"):
            continue
        for row in _split_year_columns(item, statement):
            if not row.get("flow") and default_flow:
                row["flow"] = default_flow
            out.append(row)
    return out


def extract_page(path: Path, page: int, dpi: int = EXTRACT_DPI,
                 force: bool = False) -> dict[str, Any]:
    """Pass 2：對單一頁面做結構化抽取。page 為 1-based。結果會快取。"""
    cache = _cache_path(path, "extract", page)
    if not force:
        cached = _load(cache)
        if cached and not cached.get("error"):
            # 讀快取時也跑一次欄位正規化，這樣舊快取不必重抽就能沿用
            cached["line_items"] = _expand_items(cached)
            return cached

    image = render_page(path, page - 1, dpi)
    text, stop = _converse_images([image], EXTRACT_PROMPT.format(page=page),
                                  max_tokens=16000)
    parsed = _json_from(text)
    if not isinstance(parsed, dict):
        parsed = {"page": page, "unreadable": True, "line_items": [],
                  "error": "回覆無法解析為 JSON",
                  "stop_reason": stop, "raw_head": (text or "")[:600]}
    else:
        parsed["line_items"] = _expand_items(parsed)
        parsed["stop_reason"] = stop
        if stop == "max_tokens":
            parsed["truncated"] = True
            log.warning("%s p%d 回覆被截斷，已保留可解析的 %d 筆明細",
                        path.name, page, len(parsed["line_items"]))
        if parsed["line_items"]:
            parsed["unreadable"] = False
    parsed.setdefault("page", page)
    parsed.setdefault("line_items", [])
    parsed["_source"] = {"file": path.name, "dpi": dpi}
    _save(cache, parsed)
    return parsed


def extract_document(path: Path, pages: list[int] | None = None,
                     max_extract: int | None = None,
                     include_low_priority: bool = False,
                     on_progress=None) -> dict[str, Any]:
    """對一份文件跑完整兩段式流程。"""
    t0 = time.perf_counter()
    classified = classify_pages(path, on_progress=on_progress)
    targets = (pages if pages is not None
               else wanted_pages(classified, include_low_priority))
    if max_extract:
        targets = targets[:max_extract]

    institution = None
    for c in classified:
        if c.get("institution"):
            institution = c["institution"]
            break

    extracted: list[dict[str, Any]] = []
    for i, p in enumerate(targets, start=1):
        if on_progress:
            on_progress("extract", p, len(targets))
        extracted.append(extract_page(path, p, force=False))

    type_counts: dict[str, int] = {}
    for c in classified:
        t = c.get("type") or "?"
        type_counts[t] = type_counts.get(t, 0) + 1

    return {
        "file": path.name,
        "path": str(path),
        "total_pages": page_count(path),
        "institution": institution,
        "page_types": type_counts,
        "classified": classified,
        "target_pages": targets,
        "extracted": extracted,
        "line_item_count": sum(len(e.get("line_items", [])) for e in extracted),
        "elapsed_s": round(time.perf_counter() - t0, 1),
    }


def cache_stats() -> dict[str, Any]:
    if not OCR_CACHE.exists():
        return {"docs": 0, "files": 0, "bytes": 0}
    files = list(OCR_CACHE.rglob("*.json"))
    return {
        "cache_dir": str(OCR_CACHE),
        "docs": len([d for d in OCR_CACHE.iterdir() if d.is_dir()]),
        "files": len(files),
        "bytes": sum(f.stat().st_size for f in files),
    }
