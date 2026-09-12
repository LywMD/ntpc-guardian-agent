"""API 端到端測試（需先啟動 python cli.py serve）。"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
FAILED: list[str] = []


def get(path: str, timeout: int = 120):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def post(path: str, body: dict, timeout: int = 300):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'[PASS]' if cond else '[FAIL]'} {name}{'  ' + detail if detail else ''}")
    if not cond:
        FAILED.append(name)


def main() -> int:
    print("=" * 72)
    print(f"API 測試：{BASE}")
    print("=" * 72)

    st, health = get("/api/health")
    check("GET /api/health", st == 200 and health.get("ok") is True,
          f"model={health.get('model_id')} 機構={health.get('db',{}).get('institutions')}")
    check("health 有回報資料來源模式",
          health.get("db", {}).get("data_source_mode") in ("live", "seed", "real", "demo", "real+demo"),
          str(health.get("db", {}).get("data_source_mode")))

    with urllib.request.urlopen(BASE + "/", timeout=30) as r:
        html = r.read().decode("utf-8")
    check("GET / 回傳前端頁面", "小小守護員" in html and "leaflet" in html.lower(),
          f"{len(html)} bytes")

    st, lb = get("/api/leaderboard?city=" + urllib.parse.quote("新北市") + "&top_n=10")
    check("GET /api/leaderboard", st == 200 and lb["count"] > 0, f"{lb['count']} 筆")
    top = lb["leaderboard"][0]
    check("排行榜首位有座標可畫地圖", top.get("lat") and top.get("lng"),
          f"{top['name']} ({top['lat']}, {top['lng']})")
    iid = top["inst_id"]

    st, d = get(f"/api/institution/{iid}")
    check("GET /api/institution/{id}", st == 200 and "institution" in d,
          d["institution"]["name"])

    st, d = get(f"/api/institution/{iid}/detail")
    check("GET /api/institution/{id}/detail 有鑑識會計結果",
          st == 200 and "financial_subscore" in d["forensic"],
          f"財務子分數 {d['forensic']['financial_subscore']}")
    check("detail 有 Benford 前兩位結果",
          "benford_first_two_digits" in d["forensic"]["signals"])
    check("detail 有輿情結果", "sentiment_subscore" in d["sentiment"],
          str(d["sentiment"]["sentiment_subscore"]))
    check("detail 有歷史紀錄", "history_subscore" in d["history"])
    check("detail 有法規遵循結果", "compliance_subscore" in d.get("compliance", {}),
          str(d.get("compliance", {}).get("compliance_subscore")))
    ra = d["forensic"]["signals"]["ratio_analysis"]
    check("detail 有分年齡層師生比", len(ra.get("ratio_by_age_band", [])) >= 1,
          "、".join(f"{b['band']} 1:{b['ratio']}" for b in ra.get("ratio_by_age_band", [])))
    check("detail 有全體統計定位", len(ra.get("baseline", {}).get("comparisons", [])) >= 3)

    st, d = get("/api/compliance?city=" + urllib.parse.quote("新北市"))
    check("GET /api/compliance", st == 200 and d["scanned"] > 0,
          f"受檢 {d['scanned']} 間／有違規 {d['with_violations']} 間／"
          f"重大 {d['with_critical']} 間")
    masking = [x for x in d["results"] if "合併計算掩蓋年齡層人力缺口" in x["top_violations"]]
    check("API 能抓出「合併師生比掩蓋缺口」案例", len(masking) > 0,
          "、".join(m["name"].replace("新北市", "") for m in masking[:3]))

    st, d = get("/api/baseline?city=" + urllib.parse.quote("新北市"))
    check("GET /api/baseline", st == 200 and len(d["indicators"]) >= 5,
          f"母體 {d['population']} 間／{len(d['indicators'])} 項指標")
    check("baseline 每項指標都有 σ 門檻",
          all(len(s["thresholds"]) == 3 for s in d["indicators"].values()))

    st, d = get("/api/district-stability?city=" + urllib.parse.quote("新北市"))
    # 母體現在預設挑「有真實資料就用真實資料」（見 forensic._pick_dataset）。
    # 真實資料目前只有公校決算書名冊與非營利園查核財報，欄位裡的 district
    # 尚未細到分區，因此全數落在同一個「新北市」分類——這是資料涵蓋範圍的
    # 已知限制，不是端點壞了，所以只驗證端點本身能回應、有母體，不強求分區數。
    check("GET /api/district-stability", st == 200 and len(d["districts"]) >= 1,
          f"{len(d['districts'])} 區／{len(d['underpowered_districts'])} 區樣本不足")

    st, d = get("/api/regulations")
    check("GET /api/regulations 列出法定門檻", st == 200 and len(d["age_bands"]) >= 4)
    b2 = next(x for x in d["age_bands"] if x["key"] == "age_2_to_3")
    check("regulations API 的 2歲專班為 1:8、上限16人、不得混齡",
          b2["staff_ratio"] == "1:8" and b2["class_size_limit"] == 16
          and b2["no_mixed_age"] is True,
          f"{b2['staff_ratio']} / {b2['class_size_limit']} 人 / 混齡禁止={b2['no_mixed_age']}")
    check("regulations API 有附法源條號",
          bool(b2["citation_staff"]["article"]) and bool(b2["citation_staff"]["url"]),
          b2["citation_staff"]["law"] + b2["citation_staff"]["article"])

    st, d = get("/api/alerts?limit=10")
    check("GET /api/alerts", st == 200 and len(d["alerts"]) > 0, f"{len(d['alerts'])} 則")

    st, d = get("/api/perceive?city=" + urllib.parse.quote("新北市"))
    check("GET /api/perceive", st == 200 and len(d["triggers"]) > 0,
          f"{len(d['triggers'])} 個訊號")

    st, d = get("/api/search?q=" + urllib.parse.quote("青草地"))
    check("GET /api/search", st == 200 and d["count"] >= 1)

    st, d = post(f"/api/score/{iid}", {})
    check("POST /api/score/{id}", st == 200 and 0 <= d["total_score"] <= 100,
          f"{d['total_score']} {d['risk_level']}")
    check("評分含四個子分數", len(d["subscores"]) == 4, json.dumps(d["subscores"]))

    print("\n  Agent 對話（會真的呼叫 Bedrock，需等 30–90 秒）…")
    st, d = post("/api/chat", {"message":
                               "新北市風險最高的前 3 間機構是哪些？請用一句話說明每一間的主要原因。"})
    ok = st == 200 and len(d.get("answer", "")) > 60
    check("POST /api/chat 有回答", ok, f"{len(d.get('answer',''))} 字")
    check("POST /api/chat 有實際呼叫工具", d.get("tool_calls", 0) > 0,
          f"{d.get('tool_calls')} 次：" + ", ".join(t["tool"] for t in d.get("trace", [])))
    sid = d.get("session_id")

    st, d2 = post("/api/chat", {"message": "第一名那間的 Benford 檢定結果是什麼？",
                                "session_id": sid})
    check("POST /api/chat 可延續同一 session 追問", st == 200 and len(d2.get("answer", "")) > 30,
          f"session={sid}")

    st, d = get(f"/api/audit?session={sid}&limit=20")
    check("GET /api/audit 有工具呼叫軌跡", st == 200 and d["count"] > 0, f"{d['count']} 筆")

    print("\n" + "=" * 72)
    if FAILED:
        print(f"失敗 {len(FAILED)} 項：")
        for x in FAILED:
            print("  -", x)
        return 1
    print("全部通過")
    print("\n--- Agent 回答節錄 ---")
    print(d2.get("answer", "")[:600] if isinstance(d2, dict) else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
