"""整合資料庫（SQLite）。

對應提案中的 Amazon RDS (PostgreSQL)：本機以 SQLite 實作同一份 schema，
所有 SQL 皆為標準語法，正式環境把連線換成 RDS 即可沿用。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Iterable

from . import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS institutions (
    inst_id        TEXT PRIMARY KEY,   -- 統一機構 ID
    name           TEXT NOT NULL,
    aliases        TEXT DEFAULT '[]',  -- 各來源的不同表述
    inst_type      TEXT,               -- 幼兒園／托嬰中心…
    city           TEXT,
    district       TEXT,
    address        TEXT,
    lat            REAL,
    lng            REAL,
    capacity       INTEGER,            -- 核定招收人數
    enrolled       INTEGER,            -- 在園幼兒總數
    staff_count    INTEGER,            -- 教保服務人員總數（不含園長）
    rating         TEXT,               -- 最近一次評鑑等第
    rating_year    INTEGER,
    established    TEXT,
    sources        TEXT DEFAULT '[]',
    updated_at     TEXT
);

CREATE TABLE IF NOT EXISTS penalties (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    inst_id   TEXT NOT NULL,
    date      TEXT,
    reason    TEXT,
    law       TEXT,
    amount    REAL,
    severity  INTEGER DEFAULT 1,       -- 1 輕微 / 2 中等 / 3 重大
    source    TEXT,
    UNIQUE(inst_id, date, reason)
);

CREATE TABLE IF NOT EXISTS fees (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    inst_id   TEXT NOT NULL,
    year      INTEGER,
    item      TEXT,                    -- 學費／雜費／代收代辦費…
    amount    REAL,
    period    TEXT,                    -- 每學期／每月
    source    TEXT,
    UNIQUE(inst_id, year, item)
);

CREATE TABLE IF NOT EXISTS financials (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    inst_id   TEXT NOT NULL,
    year      INTEGER,
    flow      TEXT,                    -- income / expense
    subject   TEXT,                    -- 會計科目
    amount    REAL,
    source    TEXT
);

CREATE TABLE IF NOT EXISTS social_posts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    inst_id      TEXT,
    platform     TEXT,
    posted_at    TEXT,
    url          TEXT,
    author       TEXT,
    content      TEXT,
    sentiment    REAL,                 -- -1 ~ 1
    neg_keywords TEXT DEFAULT '[]',
    UNIQUE(platform, url, content)
);

CREATE TABLE IF NOT EXISTS scores (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    inst_id       TEXT NOT NULL,
    scored_at     TEXT,
    total         REAL,
    financial_sub REAL,
    sentiment_sub REAL,
    history_sub   REAL,
    level         TEXT,
    detail        TEXT                 -- JSON：證據與計算明細
);

CREATE TABLE IF NOT EXISTS alerts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    inst_id    TEXT,
    created_at TEXT,
    kind       TEXT,                   -- threshold / spike / sentiment
    message    TEXT,
    total      REAL,
    delta      REAL,
    acked      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT,
    session   TEXT,
    step      INTEGER,
    tool      TEXT,
    tool_input  TEXT,
    tool_output TEXT,
    latency_ms  INTEGER
);

-- 公校決算書抽出的收費／補助基準（全市層級，供交叉比對參照）
CREATE TABLE IF NOT EXISTS fee_standards (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    city      TEXT,
    year      INTEGER,
    category  TEXT,
    amount    REAL,
    context   TEXT,
    source    TEXT,
    UNIQUE(city, year, category, amount, source)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_fin_inst ON financials(inst_id, year);
CREATE INDEX IF NOT EXISTS idx_scores_inst ON scores(inst_id, scored_at);
CREATE INDEX IF NOT EXISTS idx_social_inst ON social_posts(inst_id, posted_at);
CREATE INDEX IF NOT EXISTS idx_pen_inst ON penalties(inst_id);
"""


# 依法規遵循檢核需要而後續追加的欄位。
# 用 ALTER TABLE 增量套用，讓既有資料庫不必重建（正式環境對應 RDS migration）。
MIGRATIONS: list[tuple[str, str, str]] = [
    # (table, column, DDL type/default)
    ("institutions", "enrolled_2y", "INTEGER"),        # 2歲以上未滿3歲在園人數
    ("institutions", "enrolled_3to5", "INTEGER"),      # 3歲以上至入小學前在園人數
    ("institutions", "classes_2y", "INTEGER"),         # 2歲專班班級數
    ("institutions", "classes_3to5", "INTEGER"),       # 3歲以上班級數
    ("institutions", "classes_5y", "INTEGER"),         # 5歲至入小學前班級數
    ("institutions", "staff_2y", "INTEGER"),           # 配置於2歲專班之教保服務人員
    ("institutions", "staff_3to5", "INTEGER"),         # 配置於3歲以上班級之教保服務人員
    ("institutions", "teacher_count", "INTEGER"),      # 幼兒園教師人數
    ("institutions", "assistant_count", "INTEGER"),    # 助理教保員人數
    ("institutions", "has_principal", "INTEGER"),      # 是否置專任園長
    ("institutions", "nurse_type", "TEXT"),            # 無／特約／兼任／專任
    ("institutions", "cook_count", "INTEGER"),         # 廚工人數
    ("institutions", "has_group_insurance", "INTEGER"),
    ("institutions", "bus_count", "INTEGER"),
    ("institutions", "bus_oldest_year", "INTEGER"),    # 最舊幼童專用車出廠年
    ("institutions", "bus_has_escort", "INTEGER"),     # 是否配置隨車人員
    ("institutions", "is_public_affiliated", "INTEGER"),  # 公立學校附設
    ("institutions", "is_remote_area", "INTEGER"),     # 離島／偏遠／原住民族地區
    ("institutions", "mixed_age_approved", "INTEGER"), # 經主管機關同意混齡編班
    ("institutions", "disabled_children", "INTEGER"),  # 身心障礙幼兒數
    ("institutions", "fee_filed_date", "TEXT"),        # 收費數額報主管機關備查日期
    ("institutions", "indoor_area", "REAL"),           # 室內樓地板面積（㎡）
    ("institutions", "outdoor_area", "REAL"),          # 室外活動面積（㎡）
    ("institutions", "floor_max", "INTEGER"),          # 使用之最高樓層
    ("institutions", "outdoor_separated_2y", "INTEGER"),  # 2歲專班室外活動是否區隔
    ("scores", "compliance_sub", "REAL"),              # 法規遵循子分數
    # 財務明細的用途分類。真實財報同一年度會有多張報表（收支餘絀表、功能別、
    # 各學年比較表、費用明細…），全部加總會嚴重重複計算，因此必須分開標記：
    #   primary    該年度的權威收支表 → 唯一用來算合計與比率的來源
    #   detail     費用明細（業務費、材料費、人事費用…）→ 只供 Benford 使用
    #   comparison 多年度比較表 → 不納入單一年度合計
    #   balance    資產負債表
    #   budget     預算對照／流用檢查
    #   subtotal   合計／小計列 → 一律不納入計算
    ("financials", "role", "TEXT"),
    # 資料集標記：real（新北市政府提供的真實文件）／demo（內建示範資料集）。
    # 為什麼需要：真實財報沒有年齡層拆分與人員配置欄位，示範資料有。
    # 兩者若混在同一個母體算平均與標準差，異常判定會失真，
    # 因此同業比較與全市基準一律「只跟同一個 dataset 的機構比」。
    # 兩者可以同時存在於資料庫（真實財務證據 + 完整功能演示），但統計上分開。
    ("institutions", "dataset", "TEXT"),
]


def _migrate(c: sqlite3.Connection) -> None:
    by_table: dict[str, set[str]] = {}
    for table, column, ddl in MIGRATIONS:
        if table not in by_table:
            by_table[table] = {
                r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()
            }
        if column in by_table[table]:
            continue
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        by_table[table].add(column)
    c.commit()


def conn() -> sqlite3.Connection:
    if getattr(_local, "conn", None) is None:
        c = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.executescript(SCHEMA)
        _migrate(c)
        _local.conn = c
    return _local.conn


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 機構識別
_NOISE = re.compile(
    r"(私立|市立|縣立|公立|附設|非營利|財團法人|社團法人|股份有限公司|有限公司|"
    r"股份|分校|分園|\s|　|\(|\)|（|）|台|臺)"
)
_TYPE_WORDS = ("幼兒園", "托嬰中心", "課後照顧中心", "兒少安置機構", "托兒所", "幼稚園")


def normalize_name(name: str) -> str:
    """去除機關前後綴與異體字，讓不同來源的表述可以比對。"""
    s = (name or "").strip()
    s = _NOISE.sub("", s)
    return s


def normalize_address(addr: str) -> str:
    s = (addr or "").strip()
    s = re.sub(r"^\d{3,5}", "", s)                     # 郵遞區號
    s = s.replace("臺", "台").replace("F", "樓")
    s = re.sub(r"[\s　\-－之]", "", s)
    return s


def make_inst_id(name: str, address: str, city: str = "") -> str:
    key = f"{normalize_name(name)}|{normalize_address(address)[:14]}|{city}"
    return "INST-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12].upper()


def infer_type(name: str) -> str:
    for w in _TYPE_WORDS:
        if w in name:
            return {"托兒所": "幼兒園", "幼稚園": "幼兒園"}.get(w, w)
    return "幼兒園"


# ---------------------------------------------------------------- 寫入
def upsert_institution(rec: dict[str, Any]) -> str:
    """以「名稱＋地址」模糊比對後的統一 ID 寫入，重複來源合併為 aliases。"""
    inst_id = rec.get("inst_id") or make_inst_id(
        rec.get("name", ""), rec.get("address", ""), rec.get("city", "")
    )
    c = conn()
    existing = c.execute("SELECT * FROM institutions WHERE inst_id=?", (inst_id,)).fetchone()

    aliases = set(rec.get("aliases") or [])
    sources = set(rec.get("sources") or ([rec["source"]] if rec.get("source") else []))
    if existing:
        aliases |= set(json.loads(existing["aliases"] or "[]"))
        sources |= set(json.loads(existing["sources"] or "[]"))
        if existing["name"] != rec.get("name") and rec.get("name"):
            aliases.add(existing["name"])
    aliases.discard(rec.get("name"))

    # 新資料優先，缺值沿用既有資料；aliases / sources 為合併語意，另行處理
    carry = [col for col in _institution_columns(c)
             if col not in ("inst_id", "aliases", "sources", "updated_at")]
    merged: dict[str, Any] = {"inst_id": inst_id}
    for col in carry:
        if col in rec and rec[col] is not None:
            merged[col] = rec[col]
        elif existing is not None:
            merged[col] = existing[col]
        else:
            merged[col] = None
    merged["name"] = merged.get("name") or ""
    merged["inst_type"] = merged.get("inst_type") or infer_type(rec.get("name", ""))
    merged["aliases"] = json.dumps(sorted(a for a in aliases if a), ensure_ascii=False)
    merged["sources"] = json.dumps(sorted(s for s in sources if s), ensure_ascii=False)
    merged["updated_at"] = now_iso()

    cols = ",".join(merged)
    ph = ",".join("?" * len(merged))
    c.execute(f"INSERT OR REPLACE INTO institutions ({cols}) VALUES ({ph})", tuple(merged.values()))
    c.commit()
    return inst_id


def _institution_columns(c: sqlite3.Connection) -> list[str]:
    if getattr(_local, "inst_cols", None) is None:
        _local.inst_cols = [
            r["name"] for r in c.execute("PRAGMA table_info(institutions)").fetchall()
        ]
    return _local.inst_cols


def insert_many(table: str, rows: Iterable[dict[str, Any]], ignore: bool = True) -> int:
    rows = [r for r in rows if r]
    if not rows:
        return 0
    c = conn()
    verb = "INSERT OR IGNORE" if ignore else "INSERT"
    n = 0
    for r in rows:
        cols = ",".join(r)
        ph = ",".join("?" * len(r))
        cur = c.execute(f"{verb} INTO {table} ({cols}) VALUES ({ph})", tuple(r.values()))
        n += cur.rowcount if cur.rowcount > 0 else 0
    c.commit()
    return n


def log_tool_call(session_id: str, step: int, tool: str, tin: Any, tout: Any, latency_ms: int) -> None:
    def _trim(x: Any, n: int = 6000) -> str:
        s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False, default=str)
        return s[:n]

    conn().execute(
        "INSERT INTO audit_log (ts,session,step,tool,tool_input,tool_output,latency_ms)"
        " VALUES (?,?,?,?,?,?,?)",
        (now_iso(), session_id, step, tool, _trim(tin), _trim(tout), latency_ms),
    )
    conn().commit()


# ---------------------------------------------------------------- 查詢
def q(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn().execute(sql, params).fetchall()]


def q1(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    r = conn().execute(sql, params).fetchone()
    return dict(r) if r else None


def get_institution(inst_id: str) -> dict[str, Any] | None:
    return q1("SELECT * FROM institutions WHERE inst_id=?", (inst_id,))


def find_institutions(keyword: str, limit: int = 10) -> list[dict[str, Any]]:
    """名稱模糊搜尋：先精確、再正規化包含、最後 rapidfuzz 相似度。"""
    kw = (keyword or "").strip()
    if not kw:
        return []
    rows = q("SELECT * FROM institutions")
    norm_kw = normalize_name(kw)
    exact = [r for r in rows if r["name"] == kw]
    if exact:
        return exact[:limit]
    contains = [
        r for r in rows if norm_kw and (norm_kw in normalize_name(r["name"]) or norm_kw in (r["aliases"] or ""))
    ]
    if contains:
        return contains[:limit]
    try:
        from rapidfuzz import fuzz

        scored = sorted(
            ((fuzz.token_set_ratio(norm_kw, normalize_name(r["name"])), r) for r in rows),
            key=lambda t: -t[0],
        )
        return [r for s, r in scored[:limit] if s >= 60]
    except ImportError:
        return []


def set_meta(key: str, value: str) -> None:
    conn().execute("INSERT OR REPLACE INTO meta (key,value,updated_at) VALUES (?,?,?)",
                   (key, value, now_iso()))
    conn().commit()


def get_meta(key: str, default: str | None = None) -> str | None:
    r = q1("SELECT value FROM meta WHERE key=?", (key,))
    return r["value"] if r else default


def stats() -> dict[str, Any]:
    return {
        "institutions": q1("SELECT COUNT(*) n FROM institutions")["n"],
        "financial_rows": q1("SELECT COUNT(*) n FROM financials")["n"],
        "fee_rows": q1("SELECT COUNT(*) n FROM fees")["n"],
        "penalties": q1("SELECT COUNT(*) n FROM penalties")["n"],
        "social_posts": q1("SELECT COUNT(*) n FROM social_posts")["n"],
        "scored": q1("SELECT COUNT(DISTINCT inst_id) n FROM scores")["n"],
        "data_source_mode": get_meta("etl_source_mode", "unknown"),
        "last_etl_at": get_meta("etl_last_run", "尚未執行"),
        "db_path": str(config.DB_PATH),
    }
