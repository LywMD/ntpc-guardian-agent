"""把抽取結果寫入整合資料庫。

輸入：data/extracted/*.json（由 scripts/ingest_real_data.py 產生）
輸出：institutions / financials / fee_standards / meta

年度換算
  非營利園財報用「學年度」：113學年度 = 112.8.1~113.7.31 → 對應西元 2024
  公校決算書用「會計年度」：民國112年度 → 對應西元 2023
  兩者都存西元年，並在 source 欄位保留原始表述以便回溯。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from .. import config, store

log = logging.getLogger("guardian.ingest.loader")

EXTRACTED_DIR = config.ROOT / "data" / "extracted"

# 會計科目 → 我們的標準分類
INCOME_KEYS = ("收入", "收益", "補助", "學費", "雜費", "代辦", "捐贈", "利息", "餘絀")
EXPENSE_KEYS = ("支出", "費用", "成本", "人事", "業務", "設備", "維護", "折舊",
                "房租", "水電", "膳食", "教材", "保險")

HR_SUBJECTS = ("人事費", "薪資", "薪工", "員工", "教保服務人員", "勞健保", "退休",
               "獎金", "加班", "人事支出")

# 財報中常夾帶「同一受託法人辦理的其他業務」的報表（例如公共托育中心、親子館）。
# 這些是不同服務實體，收支混進幼兒園的比率會直接扭曲人事費占比與收支結構，
# 因此不寫入 financials，只在結果中記錄以供追溯。
RELATED_ENTITY_HINTS = ("托育中心", "親子館", "育兒指導", "社區保母", "課後照顧中心",
                        "居家托育服務中心", "兒童館")


def _is_related_entity(page_inst: str | None, main_inst: str) -> bool:
    if not page_inst:
        return False
    if any(h in page_inst for h in RELATED_ENTITY_HINTS):
        # 名稱裡同時有幼兒園又有托育中心時，看哪個是報表主體
        return "幼兒園" not in page_inst or page_inst != main_inst
    return False


# ------------------------------------------------------------------ 重複計算防護
# 「合計／小計」列是彙總值，不是明細。納入會同時造成兩個錯誤：
#   1. 支出合計被重複計算（合計列 + 各明細列都算一次）
#   2. Benford 檢定被彙總值污染（彙總值不服從 Benford）
SUBTOTAL_MARKERS = ("合計", "小計", "總計", "共計", "合  計", "小  計",
                    "本期餘絀", "本期賸餘", "本期短絀", "餘絀數", "累計")

# 報表用途分類。真實財報同一年度會出現多張涵蓋同一筆錢的報表，
# 只能挑一張當權威來源算合計，其餘另作他用，否則合計會爆掉。
_ROLE_RULES: list[tuple[tuple[str, ...], str]] = [
    (("各學年", "比較表", "110-113", "112-113", "近三年", "歷年"), "comparison"),
    (("功能別",), "functional"),
    (("預算數與決算數", "預算執行", "經費流用", "勾支檢查", "預算對照"), "budget"),
    (("資產負債", "會計科目餘額", "淨值變動", "現金流量"), "balance"),
    (("業務費", "材料費", "維護費", "修繕", "人事費用", "其他收入及其他支出",
      "代收代付", "決算書", "明細表", "清冊", "財產"), "detail"),
    (("收支餘絀表", "收支表", "收支檢討表", "損益表"), "primary"),
]


_MULTIYEAR_RANGE = re.compile(r"(\d{3})\s*[-~至－—到]\s*(\d{3})")


def _statement_role(statement: str | None, period: str | None) -> str:
    s = f"{statement or ''}"
    p = f"{period or ''}"
    # 期間橫跨多個年度 → 比較表，不能歸給單一年度
    if len(set(re.findall(r"(\d{3})\s*學年", p))) > 1:
        return "comparison"
    m = _MULTIYEAR_RANGE.search(p)          # 例如「110-113學年度」
    if m and abs(int(m.group(2)) - int(m.group(1))) >= 2:
        return "comparison"
    if len(set(re.findall(r"(\d{3})\s*[\.年]\s*\d{1,2}", p))) > 2:
        return "comparison"
    for keys, role in _ROLE_RULES:
        if any(k in s for k in keys):
            return role
    return "detail"


def _is_subtotal(subject: str) -> bool:
    return any(m in (subject or "") for m in SUBTOTAL_MARKERS)


# 每個年度只能有一個權威來源。優先序：
#   primary   該年度自己的收支餘絀表（最可靠）
#   prior     下一年度報表裡的「上期比較數」
#   multiyear 各學年比較表拆出來的該年度欄位
# 沒被選中的一律降級為 duplicate，不納入合計，但仍留在資料庫供追溯。
_AUTHORITY_RANK = {"primary": 3, "prior": 2, "multiyear": 1}


def _dedupe_primary(rows: list[dict[str, Any]]
                    ) -> tuple[list[dict[str, Any]], dict[int, str]]:
    """每個年度挑一個權威收支來源，其餘降級。

    這一步是防止「收支餘絀表」「收支檢討表」「功能別」「各學年比較表」
    同時被算進同一年的合計——那會讓支出變成收入的好幾倍。
    """
    cand: dict[tuple[int, str], dict[str, Any]] = {}
    for r in rows:
        if r["role"] not in _AUTHORITY_RANK:
            continue
        key = (r["year"], r["source"])
        g = cand.setdefault(key, {"n": 0, "flows": set(), "role": r["role"]})
        g["n"] += 1
        g["flows"].add(r["flow"])
        # 同一來源可能混有不同 role，取最高權威
        if _AUTHORITY_RANK[r["role"]] > _AUTHORITY_RANK[g["role"]]:
            g["role"] = r["role"]

    chosen: dict[int, str] = {}
    for (year, source), g in cand.items():
        score = (_AUTHORITY_RANK[g["role"]], len(g["flows"]), g["n"])
        cur = chosen.get(year)
        if cur is None:
            chosen[year] = source
            continue
        cg = cand[(year, cur)]
        if score > (_AUTHORITY_RANK[cg["role"]], len(cg["flows"]), cg["n"]):
            chosen[year] = source

    for r in rows:
        if r["role"] not in _AUTHORITY_RANK:
            continue
        r["role"] = "primary" if chosen.get(r["year"]) == r["source"] else "duplicate"
    return rows, chosen


def _norm_flow(item: dict[str, Any]) -> str:
    flow = (item.get("flow") or "").strip().lower()
    if flow in ("income", "expense"):
        return flow
    if flow in ("asset", "liability", "net_asset"):
        return flow
    subject = item.get("subject") or ""
    if any(k in subject for k in EXPENSE_KEYS):
        return "expense"
    if any(k in subject for k in INCOME_KEYS):
        return "income"
    return "unknown"


def _academic_year_to_ad(period: str | None) -> int | None:
    """把各種年度表述換算成西元年。無法明確判定時回 None，讓呼叫端用文件年度補。

    113學年度                    → 2024
    民國113年8月1日至114年7月31日   → 2024（起始年即學年度）
    112.8.1~113.7.31            → 2023
    112.8.1~113.7.31 及 113.8.1~114.7.31 → None（兩個期間，無法單一歸屬）
    """
    if not period:
        return None
    s = str(period)
    m = re.search(r"(\d{3})\s*學年", s)
    if m:
        return int(m.group(1)) + 1911

    # 期間區間：以「起始年」為學年度。若出現兩組以上區間就視為不明確。
    ranges = re.findall(r"(\d{3})\s*[\.年]\s*\d{1,2}", s)
    if ranges:
        starts = ranges[::2] if len(ranges) % 2 == 0 else ranges
        distinct = sorted(set(starts))
        if len(distinct) > 1 and len(ranges) >= 4:
            return None          # 多期並列，交給文件年度處理
        return int(distinct[0]) + 1911

    m = re.search(r"民國\s*(\d{3})\s*年", s)
    if m:
        return int(m.group(1)) + 1911
    m = re.search(r"(20\d{2})", s)
    if m:
        return int(m.group(1))
    return None


def _clean_amount(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace("　", "").strip()
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()（）")
    try:
        f = float(s)
    except ValueError:
        return None
    return -f if neg else f


def _guess_district(name: str, address: str | None) -> str | None:
    text = f"{address or ''}{name}"
    m = re.search(r"([\u4e00-\u9fff]{2,3}[區鄉鎮市])", text)
    return m.group(1) if m else None


# ------------------------------------------------------------------ 非營利園財報
def load_nonprofit_report(doc: dict[str, Any], city: str = "新北市") -> dict[str, Any]:
    """把一份非營利幼兒園財報寫入資料庫。"""
    name = (doc.get("institution") or "").strip()
    if not name:
        # 從檔名撈：N28新樂_113學年度財務報告.pdf → 新樂
        m = re.match(r"N\d+([\u4e00-\u9fff]+)_", doc.get("file", ""))
        if m:
            name = f"{city}{m.group(1)}非營利幼兒園"
    if not name:
        return {"skipped": doc.get("file"), "reason": "無法判定機構名稱"}
    if not name.startswith(city):
        name = city + name

    address = None
    for e in doc.get("extracted", []):
        if e.get("address"):
            address = e["address"]
            break

    inst_id = store.upsert_institution({
        "name": name,
        "inst_type": "幼兒園",
        "city": city,
        "district": _guess_district(name, address),
        "address": address,
        "source": f"非營利園財報:{doc.get('file')}",
    })

    # 逐頁明細 → financials
    rows: list[dict[str, Any]] = []
    years: set[int] = set()
    headcount: dict[str, Any] = {}
    hr_by_year: dict[int, float] = {}

    skipped_related: list[dict[str, Any]] = []
    low_confidence: list[dict[str, Any]] = []

    for e in doc.get("extracted", []):
        if e.get("unreadable"):
            continue
        # 排除同一受託法人其他業務的報表，避免污染幼兒園比率
        if _is_related_entity(e.get("institution"), name):
            skipped_related.append({
                "page": e.get("page"), "statement": e.get("statement"),
                "entity": e.get("institution"),
                "line_items": len(e.get("line_items", [])),
            })
            continue
        conf = e.get("confidence")
        if isinstance(conf, (int, float)) and conf < 0.9:
            low_confidence.append({"page": e.get("page"),
                                   "statement": e.get("statement"),
                                   "confidence": conf})
        year = _academic_year_to_ad(e.get("period")) or _academic_year_to_ad(
            doc.get("file"))
        if not year:
            continue
        unit_mult = 1000.0 if "千元" in (e.get("unit") or "") else 1.0
        statement = e.get("statement") or ""
        role = _statement_role(statement, e.get("period"))
        if role in ("primary", "detail"):
            years.add(year)

        for it in e.get("line_items", []):
            amt = _clean_amount(it.get("amount"))
            if amt is None or abs(amt) < 1:
                continue
            flow = _norm_flow(it)
            if flow not in ("income", "expense"):
                continue
            subject = (it.get("subject") or "").strip()
            if not subject:
                continue
            # 多年度寬表展開後，每一列自帶民國年，優先採用
            roc = it.get("roc_year")
            row_year = (int(roc) + 1911) if isinstance(roc, int) and 100 <= roc <= 130 \
                else year
            years.add(row_year)
            row_role = "subtotal" if _is_subtotal(subject) else role
            # 多年度比較表拆開後，每一年的數字本身是有效的年度決算數
            if role == "comparison" and roc:
                row_role = "subtotal" if _is_subtotal(subject) else "multiyear"
            rows.append({
                "inst_id": inst_id, "year": row_year, "flow": flow,
                "subject": subject, "amount": amt * unit_mult, "role": row_role,
                "source": f"非營利園財報 p{e.get('page')} {statement}",
            })

            # 上期比較數：只從權威收支表取，明細表的上期欄容易對不上科目
            if role == "primary" and not roc:
                prior = _clean_amount(it.get("prior_amount"))
                if prior is not None and abs(prior) >= 1:
                    rows.append({
                        "inst_id": inst_id, "year": row_year - 1, "flow": flow,
                        "subject": subject, "amount": prior * unit_mult,
                        "role": "subtotal" if _is_subtotal(subject) else "prior",
                        "source": f"非營利園財報 p{e.get('page')} {statement}（上期比較數）",
                    })

        hc = e.get("headcount") or {}
        for k in ("children", "staff"):
            if hc.get(k):
                headcount.setdefault(k, hc[k])

    # ---- 同一年度可能有多張 primary 報表，只留一張當權威來源 ----
    rows, primary_choice = _dedupe_primary(rows)

    for r in rows:
        if (r["role"] == "primary" and r["flow"] == "expense"
                and any(k in r["subject"] for k in HR_SUBJECTS)):
            hr_by_year[r["year"]] = hr_by_year.get(r["year"], 0.0) + r["amount"]

    # 先清掉同機構同年的舊資料，避免重跑累加
    touched = {(r["inst_id"], r["year"]) for r in rows}
    for iid, yr in touched:
        store.conn().execute(
            "DELETE FROM financials WHERE inst_id=? AND year=? AND source LIKE '非營利園財報%'",
            (iid, yr))
    store.conn().commit()
    n = store.insert_many("financials", rows, ignore=False)

    # 人數（若財報中有）
    upd: dict[str, Any] = {}
    if headcount.get("children"):
        upd["enrolled"] = int(headcount["children"])
    if headcount.get("staff"):
        upd["staff_count"] = int(headcount["staff"])
    if upd:
        upd.update({"name": name, "inst_id": inst_id, "city": city,
                    "inst_type": "幼兒園"})
        store.upsert_institution(upd)

    used = [e.get("page") for e in doc.get("extracted", [])
            if not e.get("unreadable")
            and not _is_related_entity(e.get("institution"), name)]
    return {
        "institution": name, "inst_id": inst_id,
        "years": sorted(years), "financial_rows": n,
        "headcount": headcount,
        "hr_expense_by_year": {k: round(v) for k, v in hr_by_year.items()},
        "pages_used": used,
        "skipped_related_entity_pages": skipped_related,
        "low_confidence_pages": low_confidence,
        "source_file": doc.get("file"),
    }


# ------------------------------------------------------------------ 公校決算書
def load_public_settlement(doc: dict[str, Any], city: str = "新北市") -> dict[str, Any]:
    """公校決算書：全市層級資料，寫入收費/補助基準與機構名冊線索。"""
    year = doc.get("year_ad")
    fee_rows: list[dict[str, Any]] = []
    for f in doc.get("fee_standards", []):
        fee_rows.append({
            "city": city, "year": year, "category": f.get("category"),
            "amount": f.get("amount"), "context": (f.get("context") or "")[:300],
            "source": f"{doc.get('file')} p{f.get('page')}",
        })
    n_fee = store.insert_many("fee_standards", fee_rows)

    # 機構名冊線索：只建立主檔，不寫財務（決算書是全市層級）
    created = 0
    for name, pages in (doc.get("institutions") or {}).items():
        if "幼兒園" not in name:
            continue
        full = name if name.startswith(city) else city + name
        store.upsert_institution({
            "name": full,
            "inst_type": "幼兒園",
            "city": city,
            "district": _guess_district(full, None),
            "is_public_affiliated": 1 if "附設" in full else 0,
            "source": f"公校決算書:{doc.get('file')} p{pages[:3]}",
        })
        created += 1

    return {"file": doc.get("file"), "year": year,
            "fee_standard_rows": n_fee,
            "institutions_from_roster": created,
            "relevant_pages": len(doc.get("relevant_pages", [])),
            "table_rows_available": doc.get("table_row_count", 0)}


# ------------------------------------------------------------------ 主流程
SEED_SOURCE_PATTERN = "%示範資料%"


def purge_seed() -> dict[str, int]:
    """清掉示範資料集的紀錄。

    為什麼要清：真實機構的資料沒有年齡層拆分（財報不會寫 2 歲專班人數），
    示範資料有。兩者混在同一個母體裡算全市平均與標準差會失真，
    法規遵循檢核也會出現一半機構「待補資料」、一半「有完整欄位」的怪狀況。
    """
    c = store.conn()
    seed_ids = [r["inst_id"] for r in store.q(
        "SELECT inst_id FROM institutions WHERE sources LIKE ?", (SEED_SOURCE_PATTERN,))]
    counts = {"institutions": len(seed_ids)}
    if not seed_ids:
        return counts
    marks = ",".join("?" * len(seed_ids))
    for table in ("financials", "fees", "penalties", "social_posts", "scores", "alerts"):
        cur = c.execute(f"DELETE FROM {table} WHERE inst_id IN ({marks})", seed_ids)
        counts[table] = cur.rowcount if cur.rowcount > 0 else 0
    c.execute(f"DELETE FROM institutions WHERE inst_id IN ({marks})", seed_ids)
    c.commit()
    return counts


def load_all(city: str = "新北市", purge_seed_data: bool = True) -> dict[str, Any]:
    if not EXTRACTED_DIR.exists():
        return {"error": f"找不到 {EXTRACTED_DIR}，請先跑 ingest_real_data.py"}

    nonprofit: list[dict[str, Any]] = []
    public: list[dict[str, Any]] = []
    for fp in sorted(EXTRACTED_DIR.glob("*.json")):
        try:
            doc = json.loads(fp.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("讀不到 %s：%s", fp.name, exc)
            continue
        if "extracted" in doc:          # OCR 產物（非營利園財報）
            nonprofit.append(load_nonprofit_report(doc, city))
        elif "relevant_pages" in doc:   # 文字解析產物（公校決算書）
            public.append(load_public_settlement(doc, city))

    purged: dict[str, int] = {}
    if (nonprofit or public) and purge_seed_data:
        purged = purge_seed()
        # 示範資料清掉後，同業母體與全市統計都得重算
        try:
            from ..tools import forensic

            forensic.clear_peer_cache()
        except Exception:  # noqa: BLE001
            pass

    if nonprofit or public:
        store.set_meta("etl_source_mode", "real")
        store.set_meta("etl_last_run", store.now_iso())
        store.set_meta("real_data_sources", json.dumps({
            "nonprofit_reports": [n.get("institution") for n in nonprofit
                                  if n.get("institution")],
            "public_settlements": [p.get("file") for p in public],
        }, ensure_ascii=False))

    st = store.stats()
    return {
        "seed_records_purged": purged,
        "nonprofit_reports_loaded": len(nonprofit),
        "nonprofit_institutions": [n.get("institution") for n in nonprofit],
        "nonprofit_financial_rows": sum(n.get("financial_rows", 0) for n in nonprofit),
        "public_settlements_loaded": len(public),
        "fee_standard_rows": sum(p.get("fee_standard_rows", 0) for p in public),
        "roster_institutions": sum(p.get("institutions_from_roster", 0) for p in public),
        "db_institutions": st["institutions"],
        "db_financial_rows": st["financial_rows"],
        "data_source_mode": st["data_source_mode"],
        "detail": {"nonprofit": nonprofit, "public": public},
    }
