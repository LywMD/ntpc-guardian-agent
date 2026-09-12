"""小小守護員 HTTP API + 前端（對應提案的 API Gateway + Lambda 層）。

啟動：python cli.py serve  → http://127.0.0.1:8000

安全性提醒：本服務預設「沒有任何身分驗證」，只綁 127.0.0.1 供本機示範使用。
內含機構財務與民眾投訴內容，屬敏感資料。若要對外開放，必須先加上：
  - 身分驗證與授權（Cognito / API Gateway Authorizer / IAM）
  - HTTPS、CORS 白名單、稽核日誌
  - 依稽查人員角色限制可見機構範圍
"""
from __future__ import annotations

import threading
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from guardian import agent as agent_mod
from guardian import config, store
from guardian.tools import compliance, etl, forensic, scoring, sentiment

app = FastAPI(title="小小守護員 風險稽查智能代理人", version="1.0.0")

_sessions: dict[str, agent_mod.GuardianAgent] = {}
_lock = threading.Lock()
WEB_DIR = config.ROOT / "web"


# ------------------------------------------------------------------ 基本
@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    from guardian import aws

    out: dict[str, Any] = {"ok": True, "db": store.stats(), "weights": config.SCORE_WEIGHTS}
    try:
        out["aws"] = aws.whoami()
        out["model_id"] = aws.resolve_model_id()
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["aws_error"] = f"{type(exc).__name__}: {exc}"
    return out


# ------------------------------------------------------------------ 排行榜 / 地圖
@app.get("/api/leaderboard")
def leaderboard(city: str | None = "新北市", top_n: int = 40) -> dict[str, Any]:
    return scoring.leaderboard(city, top_n)


@app.get("/api/alerts")
def alerts(limit: int = 30) -> dict[str, Any]:
    return scoring.alerts(limit)


@app.get("/api/perceive")
def perceive(city: str = "新北市") -> dict[str, Any]:
    return agent_mod.perceive(city)


@app.get("/api/institutions")
def institutions(city: str = "新北市", limit: int = 500) -> dict[str, Any]:
    return etl.list_all(city, limit)


@app.get("/api/institution/{inst_id}")
def institution(inst_id: str) -> dict[str, Any]:
    r = etl.get(inst_id)
    if r.get("error"):
        raise HTTPException(404, r["error"])
    return r


@app.get("/api/institution/{inst_id}/detail")
def institution_detail(inst_id: str, year: int = forensic.CURRENT_YEAR) -> dict[str, Any]:
    if not store.get_institution(inst_id):
        raise HTTPException(404, f"找不到機構 {inst_id}")
    return {
        "profile": etl.get(inst_id),
        "forensic": forensic.scan(inst_id, year, include_two_digit=True),
        "compliance": compliance.check(inst_id),
        "sentiment": sentiment.scan(inst_id=inst_id, days=365, allow_live=False),
        "history": scoring.history_subscore(inst_id),
        "score_history": scoring.score_history(inst_id)["history"],
    }


@app.get("/api/compliance")
def compliance_scan(city: str = "新北市") -> dict[str, Any]:
    """法規遵循全市掃描（工具 E）。"""
    return compliance.scan_city(city)


@app.get("/api/baseline")
def baseline(city: str = "新北市", year: int = forensic.CURRENT_YEAR) -> dict[str, Any]:
    """全體統計基準與 1σ/2σ/3σ 異常門檻。"""
    return forensic.citywide_baseline(city, year)


@app.get("/api/district-stability")
def district_stability(city: str = "新北市", year: int = forensic.CURRENT_YEAR) -> dict[str, Any]:
    """各區資料穩定度（變異係數）。"""
    return forensic.district_stability(city, year)


@app.get("/api/regulations")
def regulations_index() -> dict[str, Any]:
    """列出程式實際採用的法定門檻與法源，供教育局核對。"""
    from guardian import regulations as reg

    return {
        "age_bands": [
            {"key": b.key, "label": b.label,
             "staff_ratio": f"1:{b.staff_threshold}",
             "class_size_limit": b.class_size_limit,
             "no_mixed_age": b.no_mixed_age, "note": b.note,
             "citation_class": b.citation_class.to_dict(),
             "citation_staff": b.citation_staff.to_dict()}
            for b in reg.BANDS_BY_KEY.values()
        ],
        "assistant_max_share": reg.ASSISTANT_MAX_SHARE,
        "bus_max_age_years": reg.BUS_MAX_AGE_YEARS,
        "oversubscribe_hard_limit": reg.OVERSUBSCRIBE_HARD_LIMIT,
        "infant_center": {
            "min_total_area_sqm": reg.INFANT_MIN_TOTAL_AREA,
            "min_indoor_per_child_sqm": reg.INFANT_MIN_INDOOR_PER_CHILD,
            "min_outdoor_per_child_sqm": reg.INFANT_MIN_OUTDOOR_PER_CHILD,
            "max_floor": reg.INFANT_MAX_FLOOR,
        },
        "sigma_bands": config.SIGMA_BANDS,
        "district_cv_stable_max": config.DISTRICT_CV_STABLE_MAX,
        "score_weights": config.SCORE_WEIGHTS,
        "local_overrides": reg.LOCAL_OVERRIDES,
        "local_override_rule": reg.C_CWSS_LOCAL.to_dict(),
    }


@app.get("/api/search")
def search(q: str, limit: int = 10) -> dict[str, Any]:
    return etl.search(q, limit)


# ------------------------------------------------------------------ 批次作業
class ScanReq(BaseModel):
    city: str = "新北市"
    reset_history: bool = False


@app.post("/api/etl")
def run_etl(req: ScanReq) -> dict[str, Any]:
    return etl.refresh(req.city)


@app.post("/api/scan")
def run_scan(req: ScanReq) -> dict[str, Any]:
    """規則式全市掃描。40 間約需 20–40 秒，前端請顯示等待狀態。"""
    if req.reset_history:
        store.conn().execute("DELETE FROM scores")
        store.conn().execute("DELETE FROM alerts")
        store.conn().commit()
    return agent_mod.quick_scan(req.city)


@app.post("/api/score/{inst_id}")
def score_one(inst_id: str) -> dict[str, Any]:
    r = scoring.score_institution(inst_id, sentiment_days=365)
    if r.get("error"):
        raise HTTPException(404, r["error"])
    return r


@app.post("/api/train")
def train(req: ScanReq) -> dict[str, Any]:
    return scoring.train_supervised(req.city)


# ------------------------------------------------------------------ Agent
class ChatReq(BaseModel):
    message: str
    session_id: str | None = None


@app.post("/api/chat")
def chat(req: ChatReq) -> dict[str, Any]:
    """稽查人員自然語言追問；Agent 會即時決定要不要再查工具。"""
    with _lock:
        ag = _sessions.get(req.session_id or "")
        if ag is None:
            try:
                ag = agent_mod.GuardianAgent(session_id=req.session_id)
            except Exception as exc:  # noqa: BLE001
                return JSONResponse(status_code=503,
                                    content={"error": f"Bedrock 無法連線：{exc}"})
            _sessions[ag.session_id] = ag
    try:
        r = ag.ask(req.message)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"{type(exc).__name__}: {exc}") from exc
    return {"session_id": r["session_id"], "answer": r["report"],
            "tool_calls": r["tool_calls"], "trace": r["trace"],
            "tokens": r["tokens"], "elapsed_ms": r["elapsed_ms"]}


class InvestigateReq(BaseModel):
    name: str | None = None
    inst_id: str | None = None
    city: str = "新北市"
    mode: str = "investigate"      # investigate | full_scan | why


@app.post("/api/agent/run")
def agent_run(req: InvestigateReq) -> dict[str, Any]:
    try:
        ag = agent_mod.GuardianAgent()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"Bedrock 無法連線：{exc}") from exc
    target = req.name
    if not target and req.inst_id:
        inst = store.get_institution(req.inst_id)
        target = inst["name"] if inst else None
    if req.mode == "full_scan":
        r = ag.full_scan(req.city)
    elif req.mode == "why":
        if not target:
            raise HTTPException(400, "需要 name 或 inst_id")
        r = ag.explain_score(target)
    else:
        if not target:
            raise HTTPException(400, "需要 name 或 inst_id")
        r = ag.investigate(target)
    agent_mod.save_report(r["report"], f"{req.mode}-{target or req.city}")
    return r


@app.get("/api/audit")
def audit(session: str | None = None, limit: int = 100) -> dict[str, Any]:
    """Agent 工具呼叫軌跡，可用來檢視「它到底查了什麼」。"""
    if session:
        rows = store.q("SELECT * FROM audit_log WHERE session=? ORDER BY id DESC LIMIT ?",
                       (session, limit))
    else:
        rows = store.q("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
    return {"count": len(rows), "audit": rows}
