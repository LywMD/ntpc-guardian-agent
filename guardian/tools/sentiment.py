"""工具 C：NLP 社群輿情分析工具。

補足題目點名的「未納入社群即時輿情」缺口：
  1. 爬取 PTT／Dcard／Google 評論／新聞上提及特定機構的公開討論
  2. jieba 中文分詞 + 負面關鍵字偵測（體罰、不當管教、衛生、安全、收費…）
  3. 情感分析評分，把定性輿論轉成可量化的「輿情異常子分數」
  4. 與官方資料共用同一機構 ID，寫回整合資料庫

情感判讀採「詞典 + 否定/程度修飾」為主，邏輯透明可解釋；
語意模糊的貼文可選擇性交給 Bedrock 複判（use_llm=True）。
"""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import aws, store

log = logging.getLogger("guardian.sentiment")

# 負面關鍵字：category -> (詞彙, 嚴重度權重)
NEG_LEXICON: dict[str, tuple[list[str], float]] = {
    "身體不當對待": (["體罰", "打手心", "打屁股", "打小孩", "虐童", "虐待", "推倒", "拉扯",
                 "掌摑", "捏傷", "瘀青", "受傷沒通知"], 3.0),
    "不當管教": (["不當管教", "吼小孩", "大聲吼", "罰站", "關禁閉", "關在", "羞辱", "威脅小孩",
               "言語暴力", "恐嚇", "情緒失控"], 2.6),
    "餐飲衛生": (["衛生差", "衛生很差", "不新鮮", "餿水", "蟑螂", "老鼠", "發霉", "過期",
               "食物中毒", "拉肚子", "餐點差", "廚房髒"], 2.4),
    "公共安全": (["娃娃車", "沒有隨車", "逃生", "消防", "門沒關", "走失", "跌倒", "安全堪憂",
               "監視器壞", "超收"], 2.4),
    "收費爭議": (["亂收費", "亂收", "額外收費", "巧立名目", "不退費", "退費", "拖延退費",
               "代收代辦", "費用不透明", "漲價沒通知"], 1.6),
    "人員與行政": (["師資流動", "老師一直換", "換老師", "人手不足", "態度很差", "態度差",
                "已讀不回", "推卸責任", "隱瞞", "投訴沒用", "園長很兇"], 1.3),
}
POS_WORDS = ["用心", "耐心", "很棒", "推薦", "乾淨", "放心", "喜歡上學", "進步", "細心",
             "溝通良好", "很有愛心", "貼心", "安心", "值得", "專業"]
NEGATORS = ["沒有", "不會", "不曾", "並未", "未曾", "沒", "不"]
INTENSIFIERS = {"非常": 1.6, "超": 1.5, "很": 1.3, "太": 1.4, "極": 1.7, "根本": 1.5,
                "完全": 1.5, "有點": 0.7, "稍微": 0.6, "還算": 0.5}

_ALL_NEG = [(w, cat, wt) for cat, (words, wt) in NEG_LEXICON.items() for w in words]


_jieba_ready = False


def _init_jieba():
    """把領域詞彙一次性掛進 jieba 詞典（每次呼叫都 add_word 會非常慢）。"""
    global _jieba_ready
    try:
        import jieba
    except ImportError:
        return None
    if not _jieba_ready:
        jieba.setLogLevel(logging.WARNING)  # 別把建字典的訊息混進報告輸出
        for w, _, _ in _ALL_NEG:
            jieba.add_word(w, freq=200)
        for w in POS_WORDS:
            jieba.add_word(w, freq=200)
        _jieba_ready = True
    return jieba


def _tokenize(text: str) -> list[str]:
    jieba = _init_jieba()
    if jieba is None:
        return re.findall(r"[\u4e00-\u9fff]{1,4}|[A-Za-z]+", text)
    return [t for t in jieba.lcut(text) if t.strip()]


def analyze_text(text: str) -> dict[str, Any]:
    """單篇貼文的負面關鍵字偵測 + 情感評分（-1 ~ 1）。"""
    text = text or ""
    tokens = _tokenize(text)
    joined = "".join(tokens)

    hits: list[dict[str, Any]] = []
    neg_weight = 0.0
    for word, category, weight in _ALL_NEG:
        if word not in text:
            continue
        idx = text.find(word)
        window = text[max(0, idx - 4): idx]
        negated = any(neg in window for neg in NEGATORS)
        boost = 1.0
        for inten, mul in INTENSIFIERS.items():
            if inten in window:
                boost = mul
                break
        if negated:
            continue  # 「沒有體罰」不計為負面
        neg_weight += weight * boost
        hits.append({"keyword": word, "category": category,
                     "weight": round(weight * boost, 2)})

    pos_weight = sum(1.0 for w in POS_WORDS if w in text)
    raw = pos_weight - neg_weight
    sentiment = math.tanh(raw / 3.0)

    return {
        "sentiment": round(sentiment, 3),
        "label": "負面" if sentiment <= -0.15 else ("正面" if sentiment >= 0.15 else "中性"),
        "neg_weight": round(neg_weight, 2),
        "pos_weight": pos_weight,
        "neg_keywords": hits,
        "categories": sorted({h["category"] for h in hits}),
        "token_count": len(tokens),
    }


def _llm_recheck(texts: list[str]) -> list[dict[str, Any]] | None:
    """語意模糊的貼文交由 Bedrock 複判（選用）。"""
    if not texts:
        return []
    prompt = (
        "你是兒少機構稽查的輿情分析員。針對每則家長／民眾留言，判斷是否構成對機構的"
        "負面指控，並歸類到：身體不當對待、不當管教、餐飲衛生、公共安全、收費爭議、"
        "人員與行政、無負面。只輸出 JSON 陣列，每個元素為 "
        '{"index":int,"label":"負面|中性|正面","category":"...","confidence":0~1}。\n\n'
        + "\n".join(f"[{i}] {t[:300]}" for i, t in enumerate(texts))
    )
    try:
        resp = aws.bedrock_runtime().converse(
            modelId=aws.resolve_model_id(),
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 1500, "temperature": 0.0},
        )
        out = resp["output"]["message"]["content"][0]["text"]
        m = re.search(r"\[.*\]", out, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM 複判失敗：%s", exc)
        return None


# ------------------------------------------------------------------ live 爬取
def _try_live_social(inst_name: str, timeout: float = 8.0) -> tuple[list[dict[str, Any]], list[str]]:
    """嘗試抓取真實公開討論。失敗不阻斷，只記錄原因。"""
    notes: list[str] = []
    found: list[dict[str, Any]] = []
    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError:
        return [], ["requests/beautifulsoup4 未安裝，跳過 live 爬取"]

    keyword = re.sub(r"^(新北市|台北市|臺北市)", "", inst_name)
    targets = [
        ("PTT", f"https://www.ptt.cc/bbs/BabyMother/search?q={keyword}"),
    ]
    for platform, url in targets:
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 GuardianAgent/1.0",
                                           "Cookie": "over18=1"}, timeout=timeout)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            for div in soup.select("div.r-ent")[:20]:
                a = div.select_one("div.title a")
                if not a:
                    continue
                found.append({
                    "platform": platform,
                    "url": "https://www.ptt.cc" + a.get("href", ""),
                    "author": (div.select_one("div.author").text.strip()
                               if div.select_one("div.author") else ""),
                    "content": a.text.strip(),
                    "posted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                })
            notes.append(f"{platform}：取得 {len(found)} 筆")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{platform} 搜尋失敗（{type(exc).__name__}）")
    return found, notes


# ------------------------------------------------------------------ 主流程
def scan(
    inst_id: str | None = None,
    inst_name: str | None = None,
    days: int = 180,
    allow_live: bool = True,
    use_llm: bool = False,
) -> dict[str, Any]:
    """輿情掃描：回傳輿情異常子分數與可引用的原文證據。"""
    inst = None
    if inst_id:
        inst = store.get_institution(inst_id)
    elif inst_name:
        hits = store.find_institutions(inst_name, limit=1)
        inst = hits[0] if hits else None
    if not inst:
        return {"error": f"找不到機構（inst_id={inst_id}, name={inst_name}）"}
    inst_id = inst["inst_id"]

    notes: list[str] = []
    if allow_live:
        live, live_notes = _try_live_social(inst["name"])
        notes += live_notes
        if live:
            store.insert_many("social_posts", [
                {"inst_id": inst_id, "platform": p["platform"], "posted_at": p["posted_at"],
                 "url": p["url"], "author": p["author"], "content": p["content"]}
                for p in live
            ])

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    posts = store.q(
        "SELECT id, platform, posted_at, url, author, content FROM social_posts"
        " WHERE inst_id=? AND posted_at>=? ORDER BY posted_at DESC", (inst_id, since))

    if not posts:
        return {"inst_id": inst_id, "institution": inst["name"], "window_days": days,
                "post_count": 0, "sentiment_subscore": 0.0,
                "note": "觀察期內查無公開討論；無輿情訊號不等於無風險，僅代表此維度無資料",
                "notes": notes}

    now = datetime.now(timezone.utc)
    analyzed: list[dict[str, Any]] = []
    weighted_neg = 0.0
    weight_sum = 0.0
    category_hits: dict[str, int] = {}
    ambiguous: list[int] = []

    # 單篇嚴重度上限參考值：約等於兩個重大關鍵字命中
    SEVERITY_CAP = 6.0

    for p in posts:
        a = analyze_text(p["content"])
        try:
            posted = datetime.fromisoformat(p["posted_at"])
        except (TypeError, ValueError):
            posted = now
        age_days = max(0.0, (now - posted).total_seconds() / 86400)
        recency = math.exp(-age_days / 60.0)          # 半衰期約 6 週，近期輿情權重更高
        weight_sum += recency
        if a["label"] == "負面":
            weighted_neg += recency * min(a["neg_weight"] / SEVERITY_CAP, 1.0)
        for c in a["categories"]:
            category_hits[c] = category_hits.get(c, 0) + 1
        if a["label"] == "中性" and a["neg_weight"] == 0 and len(p["content"]) > 30:
            ambiguous.append(len(analyzed))
        analyzed.append({**p, **a, "age_days": round(age_days, 1),
                         "recency_weight": round(recency, 3)})
        store.conn().execute(
            "UPDATE social_posts SET sentiment=?, neg_keywords=? WHERE id=?",
            (a["sentiment"], json.dumps([h["keyword"] for h in a["neg_keywords"]],
                                        ensure_ascii=False), p["id"]))
    store.conn().commit()

    if use_llm and ambiguous:
        rechecked = _llm_recheck([analyzed[i]["content"] for i in ambiguous[:20]])
        if rechecked:
            for item in rechecked:
                idx = ambiguous[item.get("index", 0)] if item.get("index", 0) < len(ambiguous) else None
                if idx is None:
                    continue
                if item.get("label") == "負面" and item.get("confidence", 0) >= 0.6:
                    analyzed[idx]["label"] = "負面(LLM複判)"
                    analyzed[idx]["llm_category"] = item.get("category")
                    weighted_neg += analyzed[idx]["recency_weight"] * 0.4
                    cat = item.get("category") or "其他"
                    category_hits[cat] = category_hits.get(cat, 0) + 1
            notes.append(f"Bedrock 複判 {len(rechecked)} 則語意模糊貼文")

    # 負面強度：0~1，代表「近期加權後的負面討論占比 × 單篇嚴重度」
    neg_intensity = min(1.0, weighted_neg / weight_sum) if weight_sum else 0.0
    # 討論量因子：只有 1、2 則討論時不宜等同於持續性負評
    volume_factor = min(1.0, math.log1p(len(posts)) / math.log1p(12))

    negatives = [a for a in analyzed if a["label"].startswith("負面")]
    negatives.sort(key=lambda a: (-a.get("neg_weight", 0), a["age_days"]))
    recent_30 = sum(1 for a in negatives if a["age_days"] <= 30)

    # 嚴重類別加成（上限 28 分）：兒少安全相關的指控不能被「討論量少」稀釋
    severity_boost = 0.0
    if category_hits.get("身體不當對待"):
        severity_boost += 16
    if category_hits.get("不當管教"):
        severity_boost += 10
    if category_hits.get("餐飲衛生") or category_hits.get("公共安全"):
        severity_boost += 6
    severity_boost = min(severity_boost, 28.0)
    # 近期集中爆發加成
    recency_kicker = 6.0 if recent_30 >= 3 else (3.0 if recent_30 == 2 else 0.0)

    base = neg_intensity * 62.0 * (0.6 + 0.4 * volume_factor)
    subscore = min(100.0, base + severity_boost + recency_kicker)

    return {
        "inst_id": inst_id,
        "institution": inst["name"],
        "window_days": days,
        "post_count": len(posts),
        "negative_count": len(negatives),
        "negative_last_30d": recent_30,
        "avg_sentiment": round(sum(a["sentiment"] for a in analyzed) / len(analyzed), 3),
        "negative_intensity": round(neg_intensity, 3),
        "category_hits": category_hits,
        "sentiment_subscore": round(subscore, 1),
        "score_breakdown": {
            "base_from_intensity": round(base, 1),
            "severity_boost": round(severity_boost, 1),
            "recency_kicker": recency_kicker,
            "volume_factor": round(volume_factor, 3),
        },
        "platform_breakdown": {
            p: sum(1 for a in analyzed if a["platform"] == p)
            for p in sorted({a["platform"] for a in analyzed})
        },
        "top_evidence": [
            {"platform": a["platform"], "posted_at": a["posted_at"][:10], "url": a["url"],
             "categories": a["categories"], "sentiment": a["sentiment"],
             "quote": a["content"][:160]}
            for a in negatives[:6]
        ],
        "notes": notes,
    }


def bulk_scan(city: str = "新北市", days: int = 180, limit: int = 500) -> dict[str, Any]:
    """全市輿情掃描（不做 live 爬取，供排行榜批次使用）。"""
    rows = store.q("SELECT inst_id, name FROM institutions WHERE city=? LIMIT ?", (city, limit))
    results = []
    for r in rows:
        s = scan(inst_id=r["inst_id"], days=days, allow_live=False)
        results.append({"inst_id": r["inst_id"], "name": r["name"],
                        "sentiment_subscore": s.get("sentiment_subscore", 0.0),
                        "negative_count": s.get("negative_count", 0)})
    results.sort(key=lambda x: -x["sentiment_subscore"])
    return {"city": city, "window_days": days, "count": len(results), "results": results}
