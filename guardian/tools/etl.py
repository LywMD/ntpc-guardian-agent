"""工具 A：資料整合工具（ETL）。

職責
  1. 抓取官方公開資料：基本資料／評鑑結果／裁罰紀錄／收費明細／決算報告
  2. 非結構化 PDF 決算報告 → 結構化欄位
  3. 以「機構名稱＋地址」模糊比對，建立統一機構 ID，消除重複與表述不一致
  4. 寫入單一整合資料庫，供異常偵測與評分模組共用

實作說明：live 來源在無外網或政府 API 變更時會失敗，此時自動退回
內建示範資料集，並在回傳結果標明 source_mode，絕不假裝資料是真的。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import aws, config, seed, store

log = logging.getLogger("guardian.etl")

# 官方公開資料來源（正式環境於 Lambda + EventBridge 排程執行）
OFFICIAL_SOURCES = [
    {
        "id": "moe_kindergarten_base",
        "name": "全國教保資訊網－幼兒園基本資料",
        "url": "https://ap.ece.moe.edu.tw/webecems/OpenData/KIDBaseInfo.aspx",
        "kind": "basic",
    },
    {
        "id": "ntpc_open_data",
        "name": "新北市政府資料開放平臺－幼兒園名冊",
        "url": "https://data.ntpc.gov.tw/api/datasets/", 
        "kind": "basic",
    },
    {
        "id": "moe_penalty",
        "name": "全國教保資訊網－裁罰公告",
        "url": "https://ap.ece.moe.edu.tw/webecems/OpenData/KIDPunish.aspx",
        "kind": "penalty",
    },
    {
        "id": "moe_fee",
        "name": "全國教保資訊網－收費項目及金額",
        "url": "https://ap.ece.moe.edu.tw/webecems/OpenData/KIDFee.aspx",
        "kind": "fee",
    },
]

HEADERS = {"User-Agent": "GuardianAgent/1.0 (public-data ETL; contact: education-bureau)"}


# ------------------------------------------------------------------ live 抓取
def _try_live(timeout: float = 8.0) -> tuple[list[dict[str, Any]], list[str]]:
    """嘗試抓真實開放資料。回傳 (records, 錯誤訊息清單)。"""
    notes: list[str] = []
    records: list[dict[str, Any]] = []
    try:
        import requests
    except ImportError:
        return [], ["requests 未安裝，跳過 live 抓取"]

    for src in OFFICIAL_SOURCES:
        if src["kind"] != "basic":
            continue
        try:
            r = requests.get(src["url"], headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            payload = r.json() if "json" in r.headers.get("Content-Type", "") else json.loads(r.text)
            rows = payload if isinstance(payload, list) else payload.get("data") or []
            for row in rows:
                rec = _map_basic_row(row, src["name"])
                if rec:
                    records.append(rec)
            notes.append(f"{src['name']}：取得 {len(rows)} 筆")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{src['name']}：失敗（{type(exc).__name__}: {str(exc)[:90]}）")
    return records, notes


_FIELD_ALIASES = {
    "name": ("園所名稱", "機構名稱", "名稱", "SchoolName", "KidName", "name"),
    "address": ("地址", "園所地址", "Address", "address"),
    "city": ("縣市", "縣市別", "City", "city"),
    "district": ("鄉鎮市區", "區域", "District", "district"),
    "capacity": ("核定招收人數", "核定人數", "Capacity"),
    "enrolled": ("在園幼兒數", "現有人數", "Enrolled"),
    "staff_count": ("教保服務人員數", "教職員人數", "StaffCount"),
    "rating": ("評鑑結果", "評鑑等第", "Rating"),
}


def _pick(row: dict[str, Any], key: str) -> Any:
    for alias in _FIELD_ALIASES.get(key, ()):
        if alias in row and row[alias] not in ("", None):
            return row[alias]
    return None


def _map_basic_row(row: dict[str, Any], source: str) -> dict[str, Any] | None:
    name = _pick(row, "name")
    if not name:
        return None
    def _int(v: Any) -> int | None:
        try:
            return int(str(v).strip())
        except (TypeError, ValueError):
            return None
    return {
        "name": str(name).strip(),
        "address": str(_pick(row, "address") or "").strip(),
        "city": str(_pick(row, "city") or "").strip(),
        "district": str(_pick(row, "district") or "").strip(),
        "capacity": _int(_pick(row, "capacity")),
        "enrolled": _int(_pick(row, "enrolled")),
        "staff_count": _int(_pick(row, "staff_count")),
        "rating": _pick(row, "rating"),
        "source": source,
    }


# ------------------------------------------------------------------ PDF 決算
def parse_settlement_pdf(path: str, inst_name: str, year: int) -> list[dict[str, Any]]:
    """把非結構化的決算報告 PDF 轉為結構化的逐筆收支明細。"""
    import re

    try:
        import pdfplumber
    except ImportError:
        return []
    rows: list[dict[str, Any]] = []
    money = re.compile(r"([\u4e00-\u9fffA-Za-z（）()、\s]{2,20}?)\s+([\d,]{4,})")
    flow = "income"
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for line in text.splitlines():
                if any(k in line for k in ("支出", "費用", "成本")):
                    flow = "expense"
                elif any(k in line for k in ("收入", "收益")):
                    flow = "income"
                for subject, amount in money.findall(line):
                    try:
                        amt = float(amount.replace(",", ""))
                    except ValueError:
                        continue
                    if amt < 100:
                        continue
                    rows.append({
                        "name": inst_name, "year": year, "flow": flow,
                        "subject": subject.strip(), "amount": amt,
                        "source": f"決算報告PDF:{path}",
                    })
    return rows


# ------------------------------------------------------------------ 主流程
def _resolve(name: str, address: str, city: str) -> str:
    """模糊比對既有機構，命中就沿用同一個統一 ID。"""
    candidates = store.find_institutions(name, limit=3)
    norm_addr = store.normalize_address(address)
    for cand in candidates:
        if store.normalize_name(cand["name"]) == store.normalize_name(name):
            cand_addr = store.normalize_address(cand["address"] or "")
            if not norm_addr or not cand_addr or cand_addr[:10] == norm_addr[:10]:
                return cand["inst_id"]
    return store.make_inst_id(name, address, city)


def _load_real_data(city: str, notes: list[str]) -> dict[str, Any] | None:
    """若已有從 S3 抽取出來的真實資料，就載入資料庫。

    抽取流程（OCR / 文字解析）成本較高，因此獨立成 scripts/ingest_real_data.py，
    這裡只負責把抽取結果寫進整合資料庫。
    """
    from pathlib import Path

    extracted = Path(config.ROOT) / "data" / "extracted"
    files = sorted(extracted.glob("*.json")) if extracted.exists() else []
    if not files:
        notes.append("尚無真實資料抽取結果（可執行 scripts/ingest_real_data.py）")
        return None
    try:
        from ..ingest import loader

        res = loader.load_all(city=city)
        notes.append(
            f"載入真實資料：非營利園財報 {res.get('nonprofit_reports_loaded')} 份、"
            f"公校決算書 {res.get('public_settlements_loaded')} 份、"
            f"財務明細 {res.get('nonprofit_financial_rows')} 筆")
        return res
    except Exception as exc:  # noqa: BLE001
        notes.append(f"真實資料載入失敗（{type(exc).__name__}: {exc}）")
        log.exception("載入真實資料失敗")
        return None


def refresh(city: str = "新北市", allow_live: bool = True, seed_fallback: bool = True,
            use_real: bool = True) -> dict[str, Any]:
    """執行一次完整 ETL，回傳可讀的整合摘要。"""
    notes: list[str] = []
    live_records: list[dict[str, Any]] = []
    if allow_live:
        live_records, live_notes = _try_live()
        notes += live_notes

    # 先看有沒有已抽取好的真實資料（S3 上的非營利園財報／公校決算書）
    # use_real=False 用於測試：強制走示範資料集，避免測試結果被真實資料影響
    real = _load_real_data(city, notes) if use_real else None

    source_mode = "live"
    dataset: dict[str, list[dict[str, Any]]]
    if live_records:
        dataset = {"institutions": live_records, "fees": [], "financials": [],
                   "penalties": [], "posts": []}
    elif real:
        # 真實資料已由 loader 直接寫入資料庫，這裡不再走 dataset 流程
        store.set_meta("etl_source_mode", "real")
        store.set_meta("etl_last_run", store.now_iso())
        return {
            "ok": True,
            "source_mode": "real",
            "city": city,
            "real_data": real,
            "institutions_upserted": real.get("db_institutions", 0),
            "duplicates_merged_by_fuzzy_match": 0,
            "fee_rows": real.get("fee_standard_rows", 0),
            "financial_rows": real.get("db_financial_rows", 0),
            "penalty_rows": 0,
            "social_posts": store.stats()["social_posts"],
            "s3_snapshot": None,
            "sources_tried": [s["name"] for s in OFFICIAL_SOURCES],
            "notes": notes,
            "db": store.stats(),
        }
    elif seed_fallback:
        source_mode = "seed"
        notes.append("live 來源皆不可用，改用內建示範資料集（結構與官方欄位一致）")
        dataset = seed.build()
    else:
        return {"ok": False, "source_mode": "none", "notes": notes,
                "error": "無法取得任何官方資料，且已停用示範資料集"}

    # 1) 機構主檔 → 統一 ID
    name_to_id: dict[str, str] = {}
    merged = 0
    for rec in dataset["institutions"]:
        rec = dict(rec)
        rec.pop("_profile", None)
        # 年齡層與法規遵循欄位由 store.upsert_institution 依實際 schema 逐欄帶入，
        # 新增欄位不需要改這裡
        inst_id = _resolve(rec["name"], rec.get("address", ""), rec.get("city", city))
        if inst_id in name_to_id.values():
            merged += 1
        rec["inst_id"] = inst_id
        store.upsert_institution(rec)
        name_to_id[rec["name"]] = inst_id

    def _id_of(name: str) -> str | None:
        if name in name_to_id:
            return name_to_id[name]
        hits = store.find_institutions(name, limit=1)
        return hits[0]["inst_id"] if hits else None

    # 2) 收費公告
    fee_rows = []
    for f in dataset["fees"]:
        iid = _id_of(f["name"])
        if iid:
            fee_rows.append({"inst_id": iid, "year": f["year"], "item": f["item"],
                             "amount": f["amount"], "period": f["period"], "source": f["source"]})
    n_fee = store.insert_many("fees", fee_rows)

    # 3) 決算逐筆（先清掉同機構同年舊資料，避免重跑重複累加）
    fin_rows = []
    touched: set[tuple[str, int]] = set()
    for f in dataset["financials"]:
        iid = _id_of(f["name"])
        if not iid:
            continue
        touched.add((iid, f["year"]))
        fin_rows.append({"inst_id": iid, "year": f["year"], "flow": f["flow"],
                         "subject": f["subject"], "amount": f["amount"], "source": f["source"]})
    for iid, yr in touched:
        store.conn().execute("DELETE FROM financials WHERE inst_id=? AND year=?", (iid, yr))
    store.conn().commit()
    n_fin = store.insert_many("financials", fin_rows, ignore=False)

    # 4) 裁罰
    pen_rows = []
    for p in dataset["penalties"]:
        iid = _id_of(p["name"])
        if iid:
            pen_rows.append({"inst_id": iid, "date": p["date"], "reason": p["reason"],
                             "law": p["law"], "amount": p["amount"],
                             "severity": p["severity"], "source": p["source"]})
    n_pen = store.insert_many("penalties", pen_rows)

    # 5) 社群貼文原文（情感分析交由工具 C）
    post_rows = []
    now = datetime.now(timezone.utc)
    for p in dataset["posts"]:
        iid = _id_of(p["name"])
        if not iid:
            continue
        posted = (now - timedelta(days=int(p["days_ago"]))).isoformat(timespec="seconds")
        post_rows.append({"inst_id": iid, "platform": p["platform"], "posted_at": posted,
                          "url": p["url"], "author": p["author"], "content": p["content"]})
    n_post = store.insert_many("social_posts", post_rows)

    # 6) 記錄資料來源模式，讓後續的 Agent 報告能誠實標註資料出處
    store.set_meta("etl_source_mode", source_mode)
    store.set_meta("etl_last_run", store.now_iso())

    # 7) 選用：原始資料快照上 S3（對應提案的 Amazon S3 層）
    s3_uri = aws.s3_put(
        f"etl/{datetime.now(timezone.utc):%Y%m%d}/snapshot_{city}.json",
        json.dumps({"mode": source_mode, "institutions": len(name_to_id)},
                   ensure_ascii=False).encode("utf-8"),
        "application/json",
    )

    return {
        "ok": True,
        "source_mode": source_mode,
        "city": city,
        "institutions_upserted": len(name_to_id),
        "duplicates_merged_by_fuzzy_match": merged,
        "fee_rows": n_fee,
        "financial_rows": n_fin,
        "penalty_rows": n_pen,
        "social_posts": n_post,
        "s3_snapshot": s3_uri,
        "sources_tried": [s["name"] for s in OFFICIAL_SOURCES],
        "notes": notes,
        "db": store.stats(),
    }


def search(keyword: str, limit: int = 10) -> dict[str, Any]:
    hits = store.find_institutions(keyword, limit=limit)
    return {
        "keyword": keyword,
        "count": len(hits),
        "results": [
            {k: h[k] for k in ("inst_id", "name", "inst_type", "district", "address",
                               "enrolled", "staff_count", "rating")}
            for h in hits
        ],
    }


def get(inst_id: str) -> dict[str, Any]:
    inst = store.get_institution(inst_id)
    if not inst:
        return {"error": f"找不到機構 {inst_id}"}
    inst["aliases"] = json.loads(inst.get("aliases") or "[]")
    inst["sources"] = json.loads(inst.get("sources") or "[]")
    return {
        "institution": inst,
        "fees": store.q("SELECT year,item,amount,period FROM fees WHERE inst_id=? ORDER BY year DESC", (inst_id,)),
        "penalties": store.q("SELECT date,reason,law,amount,severity FROM penalties WHERE inst_id=? ORDER BY date DESC", (inst_id,)),
        "financial_summary": store.q(
            "SELECT year, flow, SUM(amount) total, COUNT(*) lines FROM financials"
            " WHERE inst_id=? GROUP BY year, flow ORDER BY year DESC", (inst_id,)),
        "social_post_count": store.q1(
            "SELECT COUNT(*) n FROM social_posts WHERE inst_id=?", (inst_id,))["n"],
        "latest_score": store.q1(
            "SELECT scored_at,total,financial_sub,sentiment_sub,history_sub,level"
            " FROM scores WHERE inst_id=? ORDER BY scored_at DESC LIMIT 1", (inst_id,)),
    }


def data_sources() -> dict[str, Any]:
    """回報資料庫裡每一類資料的實際出處，供報告標註來源。"""
    mode = store.get_meta("etl_source_mode", "unknown")
    real_meta = store.get_meta("real_data_sources")
    fin_by_source = store.q(
        "SELECT source, COUNT(*) n, COUNT(DISTINCT inst_id) insts,"
        " MIN(year) y0, MAX(year) y1 FROM financials"
        " GROUP BY source ORDER BY n DESC LIMIT 40")
    # 把 p12 這種頁碼收斂掉，讓來源清單好讀
    grouped: dict[str, dict[str, Any]] = {}
    for r in fin_by_source:
        key = re.sub(r"\s*p\d+.*$", "", r["source"] or "未標註").strip() or "未標註"
        g = grouped.setdefault(key, {"rows": 0, "institutions": set(),
                                     "year_min": None, "year_max": None})
        g["rows"] += r["n"]
        g["institutions"].add(r["insts"])
        for k, v in (("year_min", r["y0"]), ("year_max", r["y1"])):
            cur = g[k]
            if v is None:
                continue
            g[k] = v if cur is None else (min(cur, v) if k == "year_min" else max(cur, v))

    return {
        "data_source_mode": mode,
        "mode_meaning": {
            "real": "新北市政府提供的真實文件（非營利園財報 OCR／公校決算書解析）",
            "live": "官方開放資料 API",
            "seed": "內建示範資料集，非真實機構數據",
            "unknown": "尚未執行 ETL",
        }.get(mode, mode),
        "last_etl_at": store.get_meta("etl_last_run", "尚未執行"),
        "real_data_files": json.loads(real_meta) if real_meta else None,
        "financial_sources": [
            {"source": k, "rows": v["rows"],
             "year_range": [v["year_min"], v["year_max"]]}
            for k, v in sorted(grouped.items(), key=lambda kv: -kv[1]["rows"])
        ],
        "fee_standards": store.q(
            "SELECT year, category, amount, source FROM fee_standards"
            " ORDER BY year DESC, amount DESC LIMIT 30"),
        "institution_sources": store.q(
            "SELECT sources, COUNT(*) n FROM institutions GROUP BY sources"
            " ORDER BY n DESC LIMIT 20"),
        "social_post_count": store.stats()["social_posts"],
        "caveat": ("財務明細若來自 OCR，請以 confidence 值判讀可靠度；"
                   "示範資料集（seed）不得當成真實機構數據引用。"),
    }


def list_all(city: str | None = None, limit: int = 200) -> dict[str, Any]:
    sql = "SELECT inst_id,name,inst_type,district,enrolled,staff_count,rating FROM institutions"
    params: tuple = ()
    if city:
        sql += " WHERE city=?"
        params = (city,)
    sql += " ORDER BY name LIMIT ?"
    rows = store.q(sql, params + (limit,))
    return {"count": len(rows), "institutions": rows}
