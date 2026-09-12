"""「小小守護員」風險稽查智能代理人。

不是「資料進、分數出」的固定流程，而是具備四項能力的 Agent：

  感知 Perceive  監控官方資料異動與社群輿情變化，作為觸發調查的訊號
  規劃 Plan      依任務目標自主決定呼叫哪些工具、順序與深入程度
  行動 Act       自主呼叫四大工具（ETL／鑑識會計／輿情 NLP／風險評分）
  生成 Generate  整合證據後產出白話文風險報告，並支援稽查人員自然語言追問

技術上以 Amazon Bedrock Converse API 的 tool use 迴圈實作：
模型自己決定下一步要用哪個工具，本模組負責執行工具、回填結果、記錄軌跡。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from . import aws, config, store, toolspec
from .tools import forensic, scoring, sentiment

log = logging.getLogger("guardian.agent")

SYSTEM_PROMPT = """你是「小小守護員」，新北市教育局的兒少機構風險稽查智能代理人。
你的任務是把稽查從「事後被動」提前為「事前主動示警」，協助有限的稽查人力優先投放在最需要關注的機構。

# 你的運作方式
你不是固定的分析流程，而是要自主規劃調查步驟：
1. 先釐清任務目標屬於「全面掃描」還是「深入調查特定機構」。
2. 自主決定呼叫哪些工具、以什麼順序執行、要查多深。
3. 依證據動態調整：**如果某個工具回傳異常訊號，你必須主動加派其他工具交叉驗證**，不要跑完固定順序就結束。
4. 蒐集到足夠證據後，生成有引用來源的白話文風險報告。

# 法規判定的硬規則（不可違反）
這幾條來自新北市教育局／城鄉發展局的訪談結論與幼兒教育及照顧法原文，任何情況都不得便宜行事：
1. **師生比與班級人數一律分年齡層判定。** 2歲專班（2歲以上未滿3歲）實質 1:8、每班上限16人、
   且不得與其他年齡幼兒混齡；3歲以上至入國民小學前實質 1:15、每班上限30人。園長不計入配置。
   依據：幼兒教育及照顧法第16條第1項、第4項。
2. **絕對不可以用「全園合併師生比」認定合規。** 合併平均會把 2 歲專班的人力缺口稀釋掉。
   如果 compliance_check_tool 回報 BLENDED_RATIO_MASKING，你必須在報告中明確說明
   「表面合格但實際違規」這件事，這是本案最容易被忽略的風險。
3. **指標合理性一律以全體統計為基準，用標準差分級。** 判斷某個數字合不合理時，
   用 compliance_check_tool(action=baseline) 取全市全體統計，1σ 觀察／2σ 異常／3σ 重大偏離。
   不要憑印象說「偏高」。
4. **區域差異要先確認該區資料是否穩定。** 拿某一區的平均值當基準前，先用
   compliance_check_tool(action=district_stability) 看那一區的變異係數；
   若該區資料離散偏大，要在報告中註明這個比較基準本身不穩。
5. 引用法規時要寫出條號（例如「幼照法第16條第4項」），罰則也要一併寫（例如「幼照法第52條」）。
6. 欄位缺漏（unverifiable）只能說「無法檢核、待補資料」，**不得說成合規**。

# 工具與交叉驗證原則（重要）
- 需要 inst_id 卻只知道名稱時，先用 data_integration_tool(action=search) 取得 inst_id。
- 資料庫是空的（查無機構）時，先用 data_integration_tool(action=refresh) 執行 ETL。
- 只要是調查特定機構，**compliance_check_tool(action=check) 一定要跑**。它檢出的是可直接
  對照條文與罰則的違規事實，證據力比 Benford 這類初篩訊號強得多，應優先呈現。
- 法規遵循出現重大違規（severity=3）時：必須加做 social_sentiment_tool 看是否已有民眾反映，
  兩者相互印證能大幅提高稽查優先度的說服力。
- 財務異常子分數 ≥ 55，或 Benford 檢定結論為 nonconformity/marginal_conformity 時：
  必須加做 social_sentiment_tool 且 days 放大到 365，看是否有對應的民眾反映；
  並可加做 forensic_accounting_tool(analysis=benford, benford_digits=2) 做更細的前兩位數字檢定。
- 輿情出現「身體不當對待」或「不當管教」分類命中時：
  必須回頭用 compliance_check_tool 檢查師生比（分年齡層）與人員配置，
  確認是否為人力不足所導致的結構性問題，而不是單一個人行為。
- 交叉比對出現收入落差時：必須確認公告收費與在園人數是否為同一年度，避免誤判。
- 最後用 risk_scoring_tool(action=score_one) 產生可量化的總分與等級。

# 報告要求
- 用繁體中文、白話文寫，收件人是稽查人員與教育局科員，不要用術語堆疊。
- 每一個風險判斷都要附上具體數字與來源，例如「師生比 1:23.4，超過法定 1:15 約 56%」。
- 說明「為什麼」被標記為高風險，不要只丟一個分數。
- Benford's Law 的結果要註明它是低成本初篩指標，分數高只代表應優先複核原始憑證，不等於認定違規。
- 絕對不要編造工具沒有回傳的數字。工具查無資料時，就明確說這個維度沒有資料。
- 資料來源說明會在下方「本次資料來源」註明；若為示範資料集，你必須在報告開頭用一行標示清楚。
- 最後給出具體的建議處置與優先順序。

# 報告格式
用以下結構輸出（沒有資料的段落就寫「無資料」）：
【風險判定】等級與總分，一句話結論
【法規遵循】逐項列出違規事實、實際值 vs 法定值、法源條號與罰則；分年齡層呈現師生比。
            若有「合併計算掩蓋缺口」的情形，這裡要特別點出來。無違規就寫「未發現違規項目」。
【判定依據】分項列出財務／輿情／歷史三個維度的具體證據與數字，並標明各指標在全市全體統計中
            落在幾個標準差
【調查過程】你依序呼叫了哪些工具、為什麼在中途加派工具
【待補資料】無法檢核的項目（unverifiable），明確寫出缺哪個欄位
【建議處置】具體可執行的下一步與時限，區分「可直接開罰／限期改善」與「需進一步查證」
"""

MAX_TOOL_RESULT_CHARS = 24000

_PROVENANCE = {
    "live": "本次資料來源：官方公開資料 live 抓取。",
    "real": ("本次資料來源：新北市政府提供的真實文件——非營利幼兒園會計師查核財務報告"
             "（掃描影像，經 Bedrock 視覺模型 OCR 抽取）與公立學校決算書（文字型 PDF 解析）。"
             "這是實際機構的真實財務數字，引用時務必精確，不可四捨五入後當成原始值。"
             "OCR 抽取結果附有 confidence 值，低於 0.9 的頁面要在報告中註明可能有辨識誤差。"),
    "seed": ("本次資料來源：內建示範資料集（結構與官方欄位一致，官方 API 當下不可用）。"
             "報告開頭必須標示這一點，不可讓讀者誤以為是真實機構的實際數據。"),
    "unknown": "尚未執行過 ETL；若資料庫為空，請先呼叫 data_integration_tool(action=refresh)。",
}


def _system_prompt() -> str:
    mode = store.get_meta("etl_source_mode", "unknown") or "unknown"
    last = store.get_meta("etl_last_run", "尚未執行")
    return (SYSTEM_PROMPT + "\n# 本次資料來源\n"
            + _PROVENANCE.get(mode, _PROVENANCE["unknown"])
            + f"（最近一次資料整合：{last}）\n")


class GuardianAgent:
    """以 Bedrock Converse tool-use 迴圈實作的稽查代理人。"""

    def __init__(
        self,
        model_id: str | None = None,
        session_id: str | None = None,
        max_turns: int = config.MAX_AGENT_TURNS,
        temperature: float = 0.2,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.model_id = model_id or aws.resolve_model_id()
        self.session_id = session_id or f"S-{uuid.uuid4().hex[:10]}"
        self.max_turns = max_turns
        self.temperature = temperature
        self.on_event = on_event
        self.messages: list[dict[str, Any]] = []
        self.trace: list[dict[str, Any]] = []
        self._step = 0
        self._tokens = {"input": 0, "output": 0}

    # ------------------------------------------------------------ 內部
    def _emit(self, kind: str, **payload: Any) -> None:
        event = {"kind": kind, "session": self.session_id,
                 "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **payload}
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _pack(result: Any) -> str:
        s = json.dumps(result, ensure_ascii=False, default=str)
        if len(s) > MAX_TOOL_RESULT_CHARS:
            s = s[:MAX_TOOL_RESULT_CHARS] + '..."[已截斷，如需完整明細請縮小查詢範圍]"'
        return s

    def _converse(self) -> dict[str, Any]:
        return aws.bedrock_runtime().converse(
            modelId=self.model_id,
            system=[{"text": _system_prompt()}],
            messages=self.messages,
            toolConfig=toolspec.TOOL_CONFIG,
            inferenceConfig={"maxTokens": 8000, "temperature": self.temperature},
        )

    def _loop(self) -> str:
        """執行 tool-use 迴圈直到模型給出最終文字。"""
        final_text = ""
        for turn in range(self.max_turns):
            t0 = time.perf_counter()
            resp = self._converse()
            usage = resp.get("usage", {})
            self._tokens["input"] += usage.get("inputTokens", 0)
            self._tokens["output"] += usage.get("outputTokens", 0)
            msg = resp["output"]["message"]
            self.messages.append(msg)

            texts = [c["text"] for c in msg["content"] if "text" in c]
            tool_uses = [c["toolUse"] for c in msg["content"] if "toolUse" in c]

            if texts:
                thinking = "\n".join(texts).strip()
                self._emit("reasoning", turn=turn, text=thinking)
                final_text = thinking

            if resp.get("stopReason") != "tool_use" or not tool_uses:
                self._emit("final", turn=turn, text=final_text,
                           latency_ms=int((time.perf_counter() - t0) * 1000))
                return final_text

            tool_results = []
            for tu in tool_uses:
                self._step += 1
                name, args = tu["name"], tu.get("input", {}) or {}
                self._emit("tool_call", turn=turn, step=self._step, tool=name, args=args)
                out = toolspec.execute(name, args, self.session_id, self._step)
                summary = self._summarize(name, out["result"])
                self.trace.append({"step": self._step, "turn": turn, "tool": name,
                                   "args": args, "latency_ms": out["latency_ms"],
                                   "summary": summary})
                self._emit("tool_result", turn=turn, step=self._step, tool=name,
                           latency_ms=out["latency_ms"], summary=summary)
                tool_results.append({
                    "toolResult": {
                        "toolUseId": tu["toolUseId"],
                        "content": [{"text": self._pack(out["result"])}],
                        "status": "error" if isinstance(out["result"], dict)
                                  and out["result"].get("error") else "success",
                    }
                })
            self.messages.append({"role": "user", "content": tool_results})

        self._emit("max_turns", turns=self.max_turns)
        return final_text or "（已達工具呼叫上限，未取得最終結論）"

    @staticmethod
    def _summarize(tool: str, result: Any) -> str:
        """把工具回傳整理成一行摘要，供軌跡與 UI 顯示。"""
        if not isinstance(result, dict):
            return str(result)[:200]
        if result.get("error"):
            return f"錯誤：{result['error']}"
        if tool == "data_integration_tool":
            if "institutions_upserted" in result:
                return (f"ETL 完成（來源={result['source_mode']}）："
                        f"機構 {result['institutions_upserted']} 筆、決算明細 "
                        f"{result['financial_rows']} 筆、輿情貼文 {result['social_posts']} 筆")
            if "results" in result:
                names = "、".join(r["name"] for r in result["results"][:3])
                return f"搜尋「{result.get('keyword')}」命中 {result['count']} 筆：{names}"
            if "institution" in result:
                return f"取得 {result['institution']['name']} 的整合資料"
            if "institutions" in result:
                return f"列出 {result['count']} 間機構"
        if tool == "forensic_accounting_tool":
            if "financial_subscore" in result:
                s = result.get("signal_scores", {})
                return (f"{result['institution']} 財務異常子分數 {result['financial_subscore']}"
                        f"（比率 {s.get('ratio')}／Benford {s.get('benford')}／交叉比對 {s.get('cross_check')}）")
            if "MAD" in result:
                return (f"Benford 前{result.get('digits')}位：n={result['n']}, MAD={result['MAD']}, "
                        f"結論={result['conclusion']}, 分數={result['score']}")
            if "gap_pct" in result:
                return (f"交叉比對：預期 {result.get('expected_revenue'):,.0f} vs 申報 "
                        f"{result.get('reported_revenue'):,.0f}，落差 {result['gap_pct']:+.1%}")
            if "findings" in result:
                return f"財務比率分析：{len(result['findings'])} 項異常，分數 {result.get('score')}"
        if tool == "social_sentiment_tool":
            if "sentiment_subscore" in result:
                cats = "、".join(result.get("category_hits", {})) or "無負面分類"
                return (f"{result.get('institution')} 輿情子分數 {result['sentiment_subscore']}"
                        f"（{result.get('post_count')} 則貼文／負面 {result.get('negative_count')} 則／{cats}）")
        if tool == "compliance_check_tool":
            if "compliance_subscore" in result:
                names = "、".join(v["indicator"] for v in result.get("violations", [])[:3])
                return (f"{result['institution']} 法規遵循子分數 {result['compliance_subscore']}"
                        f"（違規 {result['violation_count']} 項／重大 {result['critical_count']} 項"
                        f"{'：' + names if names else ''}；"
                        f"待補資料 {len(result.get('unverifiable', []))} 項）")
            if "violation_frequency" in result:
                top = "、".join(list(result["violation_frequency"])[:3])
                return (f"全市法規遵循掃描 {result['scanned']} 間，"
                        f"{result['with_violations']} 間有違規、{result['with_critical']} 間有重大違規"
                        f"{'；最常見：' + top if top else ''}")
            if "districts" in result:
                unstable = [d["district"] for d in result["districts"]
                            if d.get("unstable_indicators")]
                return (f"區域穩定度：{len(result['districts'])} 區，"
                        f"{len(unstable)} 區資料離散偏大"
                        f"{'（' + '、'.join(unstable[:4]) + '）' if unstable else ''}")
            if "indicators" in result:
                return (f"全體統計基準：母體 {result['population']} 間，"
                        f"{len(result['indicators'])} 項指標，"
                        f"σ 門檻 {result.get('sigma_bands')}")
        if tool == "risk_scoring_tool":
            if "total_score" in result:
                return (f"{result['institution']} 風險總分 {result['total_score']}"
                        f"（{result['risk_level']}），主要來源：{result['primary_driver']}")
            if "top_20" in result:
                return (f"全市掃描 {result['scanned']} 間，等級分佈 {result['risk_distribution']}，"
                        f"自動加派深入調查 {len(result['escalated_institutions'])} 間")
            if "leaderboard" in result:
                return f"排行榜 {result['count']} 筆，首位 {result['leaderboard'][0]['name'] if result['leaderboard'] else '無'}"
            if "alerts" in result:
                return f"未處理預警 {len(result['alerts'])} 則"
            if "trained" in result:
                return ("模型訓練完成，AUC=" + str(result.get("train_auc"))
                        if result["trained"] else f"未訓練：{result.get('reason')}")
            if "history" in result:
                return f"取得 {len(result['history'])} 筆歷史分數"
        return json.dumps(result, ensure_ascii=False, default=str)[:200]

    # ------------------------------------------------------------ 對外
    def run(self, goal: str) -> dict[str, Any]:
        """給 Agent 一個任務目標，讓它自主規劃並執行。"""
        self._emit("goal", text=goal)
        self.messages.append({"role": "user", "content": [{"text": goal}]})
        t0 = time.perf_counter()
        text = self._loop()
        return {
            "session_id": self.session_id,
            "model_id": self.model_id,
            "goal": goal,
            "report": text,
            "tool_calls": len(self.trace),
            "trace": self.trace,
            "tokens": dict(self._tokens),
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        }

    def ask(self, question: str) -> dict[str, Any]:
        """稽查人員的自然語言追問；沿用同一段對話，可即時再查工具。"""
        return self.run(question)

    # -------------------------------------------------- 常用任務的包裝
    def full_scan(self, city: str = "新北市") -> dict[str, Any]:
        return self.run(
            f"任務目標：全面掃描{city}所有兒少照顧機構，找出最需要優先稽查的對象。\n"
            "請自行判斷是否需要先執行 ETL、要不要做全市批次評分，"
            "並針對排名前 3 名的機構逐一深入交叉驗證（財務三項訊號 + 輿情）。"
            "最後產出一份給稽查科的排行榜報告，說明每一間為什麼上榜、建議的稽查順序與時限。"
        )

    def investigate(self, name: str) -> dict[str, Any]:
        return self.run(
            f"任務目標：深入調查「{name}」的風險狀況。\n"
            "請先找出它的 inst_id，跑完鑑識會計三項訊號與社群輿情分析，"
            "依證據自主決定是否需要加派更深的檢定（例如前兩位數字 Benford、拉長輿情觀察窗），"
            "最後產出完整風險報告與建議處置。"
        )

    def explain_score(self, name: str) -> dict[str, Any]:
        return self.run(
            f"稽查人員提問：「{name}」為什麼分數會升高？請查出分數變化與造成變化的具體訊號，"
            "引用實際數字與原文佐證回答。"
        )


# ---------------------------------------------------------------- 感知層
def perceive(city: str = "新北市", spike_threshold: float | None = None,
             recent_days: int = 30) -> dict[str, Any]:
    """感知：掃出值得觸發調查的訊號。

    正式環境由 EventBridge 定期呼叫；訊號來源：
      1. 未處理的預警（分數達門檻／驟升／出現重大指控）
      2. 近期新增的負面輿情
      3. 最新一次評分較前次明顯上升的機構
    """
    spike = config.ALERT_DELTA_THRESHOLD if spike_threshold is None else spike_threshold
    open_alerts = store.q(
        "SELECT a.id,a.inst_id,a.kind,a.message,a.total,a.delta,a.created_at,i.name,i.district"
        " FROM alerts a LEFT JOIN institutions i ON i.inst_id=a.inst_id"
        " WHERE a.acked=0 ORDER BY a.created_at DESC LIMIT 50")

    recent_negative = store.q(
        """
        SELECT p.inst_id, i.name, COUNT(*) n
        FROM social_posts p JOIN institutions i ON i.inst_id=p.inst_id
        WHERE i.city=? AND p.sentiment IS NOT NULL AND p.sentiment <= -0.15
          AND p.posted_at >= datetime('now', ?)
        GROUP BY p.inst_id ORDER BY n DESC LIMIT 10
        """, (city, f"-{recent_days} days"))

    spikes = []
    for row in store.q(
        "SELECT inst_id FROM scores WHERE inst_id IN"
        " (SELECT inst_id FROM institutions WHERE city=?) GROUP BY inst_id HAVING COUNT(*)>=2",
        (city,)
    ):
        hist = store.q(
            "SELECT total, scored_at FROM scores WHERE inst_id=? ORDER BY scored_at DESC LIMIT 2",
            (row["inst_id"],))
        if len(hist) == 2:
            delta = hist[0]["total"] - hist[1]["total"]
            if delta >= spike:
                inst = store.get_institution(row["inst_id"]) or {}
                spikes.append({"inst_id": row["inst_id"], "name": inst.get("name"),
                               "delta": round(delta, 1), "total": round(hist[0]["total"], 1)})
    spikes.sort(key=lambda x: -x["delta"])

    triggers = [{"type": "alert", "inst_id": a["inst_id"], "name": a["name"],
                 "reason": a["message"]} for a in open_alerts[:10]]
    triggers += [{"type": "negative_buzz", "inst_id": r["inst_id"], "name": r["name"],
                  "reason": f"近 {recent_days} 天新增 {r['n']} 則負面討論"} for r in recent_negative[:10]]
    triggers += [{"type": "score_spike", "inst_id": s["inst_id"], "name": s["name"],
                  "reason": f"風險分數上升 {s['delta']:+.1f} 至 {s['total']}"} for s in spikes[:10]]

    seen: set[str] = set()
    deduped = []
    for t in triggers:
        key = f"{t['inst_id']}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(t)

    return {
        "city": city,
        "perceived_at": store.now_iso(),
        "open_alerts": len(open_alerts),
        "alerts": open_alerts[:10],
        "recent_negative_buzz": recent_negative,
        "score_spikes": spikes,
        "triggers": deduped,
        "should_investigate": [t for t in deduped[:5]],
    }


def auto_investigate(city: str = "新北市", max_targets: int = 2,
                     on_event: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """感知 → 規劃 → 行動 的全自動閉環：偵測到訊號就自己開調查。"""
    signals = perceive(city)
    targets = signals["should_investigate"][:max_targets]
    reports = []
    for t in targets:
        agent = GuardianAgent(on_event=on_event)
        r = agent.run(
            f"感知層偵測到訊號：{t['reason']}（機構：{t['name']}, inst_id={t['inst_id']}）。\n"
            "請以此為起點自主調查，交叉驗證這個訊號是否成立，並產出風險報告與建議處置。"
        )
        reports.append({"trigger": t, "session_id": r["session_id"],
                        "tool_calls": r["tool_calls"], "report": r["report"]})
    return {"signals": signals, "investigations": reports}


# ---------------------------------------------------------------- 不用 LLM 的快速路徑
def quick_scan(city: str = "新北市") -> dict[str, Any]:
    """純規則式的全市掃描（不呼叫 LLM），供排程批次與前端排行榜使用。"""
    from .tools import etl

    if store.stats()["institutions"] == 0:
        etl.refresh(city)
    result = scoring.scan_city(city)
    result["alerts"] = scoring.alerts(20)["alerts"]
    return result


def save_report(text: str, name: str) -> str:
    """把報告寫到本機 reports/，並在有設定 bucket 時同步到 S3。"""
    safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_")[:60] or "report"
    fname = f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}_{safe}.md"
    path = config.REPORT_DIR / fname
    path.write_text(text, encoding="utf-8")
    aws.s3_put(f"reports/{fname}", text.encode("utf-8"), "text/markdown; charset=utf-8")
    return str(path)


__all__ = ["GuardianAgent", "perceive", "auto_investigate", "quick_scan", "save_report",
           "forensic", "sentiment", "scoring"]
