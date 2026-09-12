"""Agent 的工具介面定義（Bedrock Converse toolConfig）與執行分派。

同一份 schema 也可直接搬到 Amazon Bedrock Agents 的 Action Group
（OpenAPI / function schema），因此工具契約在本機與雲端是一致的。
"""
from __future__ import annotations

import time
from typing import Any, Callable

from . import store
from .tools import compliance, etl, forensic, scoring, sentiment


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"json": {"type": "object", "properties": properties, "required": required or []}}


TOOL_SPECS: list[dict[str, Any]] = [
    {
        "toolSpec": {
            "name": "data_integration_tool",
            "description": (
                "工具 A：資料整合（ETL）。爬取／清洗官方公開資料（機構基本資料、評鑑結果、"
                "裁罰紀錄、收費明細、決算報告 PDF），以機構名稱＋地址模糊比對建立統一機構 ID，"
                "寫入整合資料庫。也用來查詢機構清單與單一機構的整合檔案。\n"
                "action=refresh 執行 ETL；action=search 依關鍵字找機構（需要 inst_id 時先用它）；"
                "action=get 取單一機構完整整合資料；action=list 列出某縣市所有機構；"
                "action=data_sources 查目前資料庫裡每一筆資料的實際出處"
                "（真實文件 real／官方 API live／示範資料 seed），寫報告要標註來源時用它。"
            ),
            "inputSchema": _schema({
                "action": {"type": "string",
                           "enum": ["refresh", "search", "get", "list", "data_sources"],
                           "description": "要執行的動作"},
                "city": {"type": "string", "description": "縣市，例如 新北市"},
                "keyword": {"type": "string", "description": "action=search 時的機構名稱關鍵字"},
                "inst_id": {"type": "string", "description": "action=get 時的統一機構 ID"},
                "limit": {"type": "integer", "description": "回傳筆數上限，預設 10"},
            }, ["action"]),
        }
    },
    {
        "toolSpec": {
            "name": "forensic_accounting_tool",
            "description": (
                "工具 B：鑑識會計異常偵測。三項訊號：(1) 財務比率分析－師生比、人事費用占比、"
                "收支結構、年增率，並與同類型同縣市同業比較；(2) Benford's Law－決算逐筆金額"
                "首位／前兩位數字分佈的卡方與 MAD 檢定；(3) 交叉比對－公告收費×在園人數推估"
                "預期收入 vs 決算申報收入，以及人事費與人員數的合理性。回傳財務異常子分數(0-100)"
                "與可引用的證據明細。analysis=all 為三項全跑。"
            ),
            "inputSchema": _schema({
                "inst_id": {"type": "string", "description": "統一機構 ID"},
                "analysis": {"type": "string",
                             "enum": ["all", "ratio", "benford", "cross_check"],
                             "description": "要跑的分析，預設 all"},
                "year": {"type": "integer", "description": "分析年度，預設 2024"},
                "benford_digits": {"type": "integer", "enum": [1, 2],
                                   "description": "Benford 檢定用首位(1)或前兩位(2)數字，預設 1"},
            }, ["inst_id"]),
        }
    },
    {
        "toolSpec": {
            "name": "social_sentiment_tool",
            "description": (
                "工具 C：NLP 社群輿情分析。爬取／讀取 PTT、Dcard、Google 評論、新聞上提及該機構"
                "的公開討論，做中文分詞、負面關鍵字偵測（體罰、不當管教、餐飲衛生、公共安全、"
                "收費爭議、人員與行政）與情感分析，回傳輿情異常子分數(0-100)、分類命中次數與"
                "可引用的原文摘錄。懷疑財務異常時可用它交叉驗證；days 可放大觀察窗。"
            ),
            "inputSchema": _schema({
                "inst_id": {"type": "string", "description": "統一機構 ID（優先）"},
                "inst_name": {"type": "string", "description": "機構名稱（沒有 inst_id 時使用）"},
                "days": {"type": "integer", "description": "觀察期天數，預設 180；要深入查證可設 365"},
                "use_llm": {"type": "boolean",
                            "description": "是否對語意模糊貼文交由 Bedrock 複判，預設 false"},
                "allow_live": {"type": "boolean", "description": "是否嘗試即時爬取，預設 false"},
            }, []),
        }
    },
    {
        "toolSpec": {
            "name": "compliance_check_tool",
            "description": (
                "工具 E：法規遵循檢核。比對可量化的法定門檻並回傳附法源條號與罰則的違規清單，"
                "產出法規遵循子分數(0-100)。\n"
                "重要：師生比與班級人數一律依幼兒教育及照顧法第16條「分年齡層」判定—"
                "2歲專班實質1:8、每班上限16人、不得與其他年齡混齡；3歲以上實質1:15、每班上限30人。"
                "本工具另有「合併稀釋檢查」，會抓出「全園平均看似合格但單一年齡層已違規」的案例。\n"
                "檢核項目還包含：助理教保員三分之一上限、5歲班幼兒園教師、護理人員配置型態、"
                "廚工人數、專任園長、超收、幼童專用車車齡與隨車人員、幼兒團體保險、收費報備查、"
                "2歲專班室外活動區隔；托嬰中心另檢核1:5托育人員、空間面積與使用樓層。\n"
                "action=check 檢核單一機構；action=scan_city 全市批次；"
                "action=baseline 取全市全體統計基準與 1σ/2σ/3σ 門檻；"
                "action=district_stability 取各區資料穩定度（變異係數）。"
            ),
            "inputSchema": _schema({
                "action": {"type": "string",
                           "enum": ["check", "scan_city", "baseline", "district_stability"],
                           "description": "要執行的動作，預設 check"},
                "inst_id": {"type": "string", "description": "action=check 時的統一機構 ID"},
                "city": {"type": "string", "description": "縣市，預設 新北市"},
                "year": {"type": "integer", "description": "統計年度，預設 2024"},
            }, ["action"]),
        }
    },
    {
        "toolSpec": {
            "name": "risk_scoring_tool",
            "description": (
                "工具 D：風險評分計算。把財務異常、法規遵循、社群輿情、歷史紀錄四個子分數"
                "以規則式權重整合為 0-100 風險總分與風險等級，並落地紀錄、與前次分數比較、觸發預警。\n"
                "action=score_one 對單一機構評分（會自動連動工具 B、C、E）；"
                "action=scan_city 全市批次掃描（財務子分數偏高者自動加派更深的輿情與 Benford 檢定）；"
                "action=leaderboard 取風險排行榜；action=alerts 取未處理預警；"
                "action=history 取某機構分數變化；action=train_model 嘗試以歷史裁罰為 label 訓練監督式模型。"
            ),
            "inputSchema": _schema({
                "action": {"type": "string",
                           "enum": ["score_one", "scan_city", "leaderboard", "alerts",
                                    "history", "train_model"]},
                "inst_id": {"type": "string"},
                "city": {"type": "string", "description": "預設 新北市"},
                "top_n": {"type": "integer", "description": "排行榜筆數，預設 20"},
                "sentiment_days": {"type": "integer", "description": "評分時的輿情觀察窗，預設 180"},
                "year": {"type": "integer", "description": "財務分析年度，預設 2024"},
            }, ["action"]),
        }
    },
]

TOOL_CONFIG: dict[str, Any] = {"tools": TOOL_SPECS}
TOOL_NAMES = [t["toolSpec"]["name"] for t in TOOL_SPECS]


# ------------------------------------------------------------------ 執行分派
def _run_data_integration(a: dict[str, Any]) -> Any:
    action = a.get("action", "search")
    if action == "refresh":
        return etl.refresh(city=a.get("city", "新北市"))
    if action == "search":
        return etl.search(a.get("keyword", ""), int(a.get("limit", 10)))
    if action == "get":
        return etl.get(a.get("inst_id", ""))
    if action == "list":
        return etl.list_all(a.get("city"), int(a.get("limit", 200)))
    if action == "data_sources":
        return etl.data_sources()
    return {"error": f"未知的 action：{action}"}


def _run_forensic(a: dict[str, Any]) -> Any:
    inst_id = a.get("inst_id", "")
    year = int(a.get("year", forensic.CURRENT_YEAR))
    which = a.get("analysis", "all")
    if which == "ratio":
        return forensic.ratio_analysis(inst_id, year)
    if which == "benford":
        return forensic.benford_test(inst_id, None, int(a.get("benford_digits", 1)))
    if which == "cross_check":
        return forensic.cross_check(inst_id, year)
    return forensic.scan(inst_id, year,
                         include_two_digit=int(a.get("benford_digits", 1)) == 2)


def _run_sentiment(a: dict[str, Any]) -> Any:
    return sentiment.scan(
        inst_id=a.get("inst_id"),
        inst_name=a.get("inst_name"),
        days=int(a.get("days", 180)),
        allow_live=bool(a.get("allow_live", False)),
        use_llm=bool(a.get("use_llm", False)),
    )


def _run_scoring(a: dict[str, Any]) -> Any:
    action = a.get("action", "leaderboard")
    if action == "score_one":
        return scoring.score_institution(
            a.get("inst_id", ""), year=int(a.get("year", forensic.CURRENT_YEAR)),
            sentiment_days=int(a.get("sentiment_days", 180)))
    if action == "scan_city":
        return scoring.scan_city(a.get("city", "新北市"),
                                 year=int(a.get("year", forensic.CURRENT_YEAR)))
    if action == "leaderboard":
        return scoring.leaderboard(a.get("city"), int(a.get("top_n", 20)))
    if action == "alerts":
        return scoring.alerts(int(a.get("top_n", 30)))
    if action == "history":
        return scoring.score_history(a.get("inst_id", ""))
    if action == "train_model":
        return scoring.train_supervised(a.get("city", "新北市"))
    return {"error": f"未知的 action：{action}"}


def _run_compliance(a: dict[str, Any]) -> Any:
    action = a.get("action", "check")
    city = a.get("city", "新北市")
    year = int(a.get("year", forensic.CURRENT_YEAR))
    if action == "check":
        return compliance.check(a.get("inst_id", ""))
    if action == "scan_city":
        return compliance.scan_city(city)
    if action == "baseline":
        return forensic.citywide_baseline(city, year)
    if action == "district_stability":
        return forensic.district_stability(city, year)
    return {"error": f"未知的 action：{action}"}


DISPATCH: dict[str, Callable[[dict[str, Any]], Any]] = {
    "data_integration_tool": _run_data_integration,
    "forensic_accounting_tool": _run_forensic,
    "social_sentiment_tool": _run_sentiment,
    "compliance_check_tool": _run_compliance,
    "risk_scoring_tool": _run_scoring,
}


def execute(name: str, args: dict[str, Any], session_id: str = "-", step: int = 0) -> dict[str, Any]:
    """執行單一工具並寫入稽核軌跡。"""
    fn = DISPATCH.get(name)
    t0 = time.perf_counter()
    if not fn:
        result: Any = {"error": f"未知的工具：{name}"}
    else:
        try:
            result = fn(args or {})
        except Exception as exc:  # noqa: BLE001
            result = {"error": f"{type(exc).__name__}: {exc}"}
    latency = int((time.perf_counter() - t0) * 1000)
    try:
        store.log_tool_call(session_id, step, name, args, result, latency)
    except Exception:  # noqa: BLE001
        pass
    return {"result": result, "latency_ms": latency}
