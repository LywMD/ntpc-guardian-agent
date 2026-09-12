"""小小守護員 CLI。

常用流程
  python cli.py check                     檢查 AWS / Bedrock 連線
  python cli.py etl                       執行資料整合（工具 A）
  python cli.py scan                      規則式全市評分（不呼叫 LLM，快）
  python cli.py board --top 15            風險排行榜
  python cli.py alerts                    未處理預警
  python cli.py inspect 快樂              單一機構的完整分析明細
  python cli.py agent-scan                讓 Agent 自主規劃全市掃描（呼叫 Bedrock）
  python cli.py investigate 快樂          讓 Agent 深入調查特定機構
  python cli.py why 快樂                  問 Agent「為什麼分數升高」
  python cli.py chat                      互動式問答（RAG 式追問）
  python cli.py perceive                  感知層：列出目前的觸發訊號
  python cli.py auto                       感知 → 自動開調查（全自動閉環）
  python cli.py train                     嘗試訓練監督式模型
  python cli.py serve                     啟動 API + 前端
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from guardian import agent as agent_mod  # noqa: E402
from guardian import aws, config, store  # noqa: E402
from guardian.tools import compliance, etl, forensic, scoring, sentiment  # noqa: E402

BAR = "─" * 72


def _hr(title: str = "") -> None:
    print(f"\n{BAR}\n{title}\n{BAR}" if title else BAR)


def _event_printer(ev: dict[str, Any]) -> None:
    kind = ev["kind"]
    if kind == "goal":
        _hr("任務目標")
        print(ev["text"])
        _hr("Agent 自主調查過程")
    elif kind == "reasoning":
        print(f"\n[規劃] {ev['text']}")
    elif kind == "tool_call":
        args = json.dumps(ev["args"], ensure_ascii=False)
        print(f"  → 呼叫工具 #{ev['step']} {ev['tool']} {args}")
    elif kind == "tool_result":
        print(f"    ✓ {ev['summary']}  ({ev['latency_ms']} ms)")
    elif kind == "max_turns":
        print(f"\n[警告] 已達工具呼叫上限 {ev['turns']} 輪")


# ------------------------------------------------------------------ 指令
def cmd_check(_: argparse.Namespace) -> int:
    _hr("AWS 連線")
    try:
        me = aws.whoami()
        for k, v in me.items():
            print(f"  {k:8}: {v}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [失敗] {type(exc).__name__}: {exc}")
        return 1
    _hr("Bedrock 模型")
    try:
        print(f"  可呼叫：{aws.resolve_model_id()}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [失敗] {exc}")
        return 2
    _hr("整合資料庫")
    for k, v in store.stats().items():
        print(f"  {k:16}: {v}")
    print(f"\n  S3 bucket      : {config.S3_BUCKET or '未設定（僅存本機）'}")
    print(f"  評分權重       : {config.SCORE_WEIGHTS}")
    return 0


def cmd_etl(args: argparse.Namespace) -> int:
    _hr("工具 A：資料整合（ETL）")
    r = etl.refresh(city=args.city, allow_live=not args.offline)
    print(f"  資料來源模式   : {r['source_mode']}"
          f"{'（官方 API 當下不可用，改用內建示範資料集）' if r['source_mode'] == 'seed' else ''}")
    print(f"  機構寫入       : {r['institutions_upserted']} 筆")
    print(f"  模糊比對合併   : {r['duplicates_merged_by_fuzzy_match']} 筆")
    print(f"  收費公告       : {r['fee_rows']} 筆")
    print(f"  決算明細       : {r['financial_rows']} 筆")
    print(f"  裁罰紀錄       : {r['penalty_rows']} 筆")
    print(f"  社群貼文       : {r['social_posts']} 筆")
    print(f"  S3 快照        : {r['s3_snapshot'] or '未設定 bucket'}")
    if r["notes"]:
        print("\n  來源狀態：")
        for n in r["notes"]:
            print(f"    - {n}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    if args.reset_history:
        store.conn().execute("DELETE FROM scores")
        store.conn().execute("DELETE FROM alerts")
        store.conn().commit()
        print("  （已清除既有評分與預警紀錄）")
    _hr(f"規則式全市風險掃描：{args.city}")
    r = agent_mod.quick_scan(args.city)
    print(f"  掃描機構       : {r['scanned']} 間")
    print(f"  等級分佈       : {r['risk_distribution']}")
    print(f"  自動加派深查   : {len(r['escalated_institutions'])} 間"
          f"{'（' + '、'.join(r['escalated_institutions'][:5]) + '…）' if r['escalated_institutions'] else ''}")
    _hr("風險排行榜 TOP 10")
    print(f"  {'#':>2} {'機構':<26} {'區':<5} {'總分':>5} {'等級':<6} "
          f"{'財務':>5} {'法規':>5} {'輿情':>5} {'歷史':>5} 主因")
    for row in r["top_20"][:10]:
        s = row["subscores"]
        name = row["name"].replace("新北市", "")
        print(f"  {row['rank']:>2} {name[:26]:<26} {row['district'][:3]:<5} "
              f"{row['total']:>5.1f} {row['level']:<6} "
              f"{s['financial_anomaly']:>5.1f} {s['regulatory_compliance']:>5.1f} "
              f"{s['social_sentiment']:>5.1f} {s['history_record']:>5.1f} {row['primary_driver']}")
    if r["alerts"]:
        _hr(f"主動預警（{len(r['alerts'])} 則）")
        for a in r["alerts"][:8]:
            print(f"  [{a['kind']}] {a['message']}")
    return 0


def cmd_board(args: argparse.Namespace) -> int:
    r = scoring.leaderboard(args.city, args.top)
    _hr(f"風險排行榜（{r['city']}，{r['count']} 筆）")
    for row in r["leaderboard"]:
        print(f"  {row['rank']:>2} {row['name'].replace('新北市',''):<28} "
              f"{row['total']:>5.1f} {row['level']:<6} "
              f"財務 {row['financial_sub']:>5.1f} / 法規 {(row.get('compliance_sub') or 0):>5.1f} / "
              f"輿情 {row['sentiment_sub']:>5.1f} / 歷史 {row['history_sub']:>5.1f}")
    return 0


def cmd_alerts(_: argparse.Namespace) -> int:
    r = scoring.alerts(50)
    _hr(f"未處理預警（{len(r['alerts'])} 則）")
    for a in r["alerts"]:
        print(f"  {a['created_at'][:16]} [{a['kind']:<9}] {a['message']}")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    hits = store.find_institutions(args.name, limit=5)
    if not hits:
        print(f"找不到機構：{args.name}")
        return 1
    inst = hits[0]
    if len(hits) > 1:
        print(f"（模糊比對命中 {len(hits)} 間，取第一筆：{inst['name']}）")
    iid = inst["inst_id"]

    _hr(f"機構：{inst['name']}")
    print(f"  統一 ID  : {iid}")
    print(f"  類型/區  : {inst['inst_type']} / {inst['district']}")
    print(f"  地址     : {inst['address']}")
    print(f"  在園/人員: {inst['enrolled']} 人 / {inst['staff_count']} 人")
    print(f"  評鑑     : {inst['rating']}")

    c = compliance.check(iid)
    _hr(f"工具 E：法規遵循　法規遵循子分數 {c['compliance_subscore']}"
        f"（違規 {c['violation_count']} 項／重大 {c['critical_count']} 項）")
    for v in c["violations"]:
        print(f"  [{v['severity_label']}] {v['indicator']}")
        print(f"       實際：{v['observed']}")
        print(f"       應為：{v['required']}")
        print(f"       法源：{v['citation']['law']}{v['citation']['article']}"
              + (f"　罰則：{v['penalty']['law']}{v['penalty']['article']}" if v["penalty"] else ""))
    if not c["violations"]:
        print("  未發現違規項目")
    if c["unverifiable"]:
        print(f"\n  待補資料（{len(c['unverifiable'])} 項，不得視為合規）：")
        for u in c["unverifiable"][:6]:
            print(f"    - {u['indicator']}（缺 {u['missing_fields']}）")

    f = forensic.scan(iid, include_two_digit=True)
    _hr(f"工具 B：鑑識會計　財務異常子分數 {f['financial_subscore']}")
    print(f"  訊號分數 : {f['signal_scores']}")
    ra = f["signals"]["ratio_analysis"]
    print(f"  同業母體 : {ra['peer_group']}")
    m = ra["metrics"]
    print("  師生比（依幼照法§16分年齡層，合併值僅供觀察）：")
    for b in ra.get("ratio_by_age_band", []):
        mark = "✗ 超標" if (b.get("over_legal_pct") or 0) > 0 else "✓"
        print(f"    {mark} {b['band']:<12} {b['children']} 人 / {b['staff']} 人 = "
              f"1:{b['ratio']}　法定 1:{b['legal_limit']:.0f}"
              f"　同業均 1:{b.get('peer_mean')}　z={b.get('z_score')}")
    print(f"    （全園合併 1:{m.get('blended_ratio')}　※不作為合規判定依據）")
    print(f"  人事費占比 {(m.get('hr_expense_pct') or 0):.1%}  "
          f"推估月薪 {m.get('avg_monthly_salary') or 0:,.0f}")
    bl = ra.get("baseline", {})
    if bl.get("comparisons"):
        print(f"  全體統計定位（母體 {bl.get('population')} 間，σ 門檻 {bl.get('sigma_bands')}）：")
        for row in bl["comparisons"][:5]:
            print(f"    {row['indicator']:<22} {row['value']:>10} "
                  f"vs 全市均 {row['citywide_mean']:>10}  z={row['z_score']:>6}  {row['level']}")
    b = f["signals"]["benford_first_digit"]
    print(f"  Benford  : n={b.get('n')} MAD={b.get('MAD')} χ²={b.get('chi_square')} "
          f"p={b.get('p_value')} → {b.get('conclusion')}")
    c = f["signals"]["cross_check"]
    print(f"  交叉比對 : 預期 {c.get('expected_revenue', 0):,.0f} vs 申報 "
          f"{c.get('reported_revenue', 0):,.0f}（落差 {c.get('gap_pct', 0):+.1%}）")
    print("\n  證據：")
    for e in f["evidence"]:
        print(f"    - {e}")

    s = sentiment.scan(inst_id=iid, days=365, allow_live=False)
    _hr(f"工具 C：社群輿情　輿情異常子分數 {s.get('sentiment_subscore')}")
    print(f"  貼文/負面: {s.get('post_count')} / {s.get('negative_count')}"
          f"（近 30 天負面 {s.get('negative_last_30d')} 則）")
    print(f"  平台分佈 : {s.get('platform_breakdown')}")
    print(f"  分類命中 : {s.get('category_hits')}")
    for e in s.get("top_evidence", [])[:4]:
        print(f"    - [{e['platform']} {e['posted_at']}] {'、'.join(e['categories'])}：{e['quote']}")

    r = scoring.score_institution(iid, sentiment_days=365)
    _hr(f"工具 D：風險評分　總分 {r['total_score']}（{r['risk_level']}）")
    print(f"  子分數   : {r['subscores']}")
    print(f"  權重     : {r['weights']}")
    print(f"  加權貢獻 : {r['weighted_contributions']}  主因：{r['primary_driver']}")
    print(f"  與前次差 : {r['delta_vs_previous']}")
    if r.get("level_floor_reason"):
        print(f"  等級調整 : {r['level_floor_reason']}")
    if r.get("directly_actionable"):
        print("  可直接開罰項目：")
        for a in r["directly_actionable"]:
            print(f"    - {a['indicator']}（{a['citation']}"
                  + (f"；罰則 {a['penalty']}" if a["penalty"] else "") + "）")
    print(f"  建議處置 : {r['recommended_action']}")
    return 0


def _run_agent(goal_fn, save_name: str) -> int:
    try:
        ag = agent_mod.GuardianAgent(on_event=_event_printer)
    except Exception as exc:  # noqa: BLE001
        print(f"[失敗] 無法初始化 Agent：{exc}")
        return 1
    result = goal_fn(ag)
    _hr("Agent 風險報告")
    print(result["report"])
    _hr("執行統計")
    print(f"  session   : {result['session_id']}")
    print(f"  模型      : {result['model_id']}")
    print(f"  工具呼叫  : {result['tool_calls']} 次")
    print(f"  tokens    : 輸入 {result['tokens']['input']} / 輸出 {result['tokens']['output']}")
    print(f"  耗時      : {result['elapsed_ms'] / 1000:.1f} 秒")
    path = agent_mod.save_report(result["report"], save_name)
    print(f"  報告已存  : {path}")
    return 0


def cmd_agent_scan(args: argparse.Namespace) -> int:
    return _run_agent(lambda ag: ag.full_scan(args.city), f"citywide-{args.city}")


def cmd_investigate(args: argparse.Namespace) -> int:
    return _run_agent(lambda ag: ag.investigate(args.name), f"investigate-{args.name}")


def cmd_why(args: argparse.Namespace) -> int:
    return _run_agent(lambda ag: ag.explain_score(args.name), f"why-{args.name}")


def cmd_perceive(args: argparse.Namespace) -> int:
    r = agent_mod.perceive(args.city)
    _hr(f"感知層訊號（{args.city}）")
    print(f"  未處理預警 : {r['open_alerts']} 則")
    print(f"  分數驟升   : {len(r['score_spikes'])} 間")
    print(f"  近期負面   : {len(r['recent_negative_buzz'])} 間")
    _hr("建議觸發調查")
    if not r["triggers"]:
        print("  目前沒有需要主動介入的訊號（可先執行 python cli.py scan 產生分數）")
    for t in r["triggers"][:10]:
        print(f"  [{t['type']:<14}] {t['name']}：{t['reason']}")
    return 0


def cmd_auto(args: argparse.Namespace) -> int:
    _hr("全自動閉環：感知 → 規劃 → 調查")
    r = agent_mod.auto_investigate(args.city, max_targets=args.max_targets,
                                   on_event=_event_printer)
    _hr("調查結果")
    for inv in r["investigations"]:
        print(f"\n### {inv['trigger']['name']}（觸發原因：{inv['trigger']['reason']}）")
        print(f"（工具呼叫 {inv['tool_calls']} 次）\n")
        print(inv["report"])
        agent_mod.save_report(inv["report"], f"auto-{inv['trigger']['name']}")
    if not r["investigations"]:
        print("  沒有觸發訊號，未開啟任何調查")
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    try:
        ag = agent_mod.GuardianAgent(on_event=_event_printer if args.verbose else None)
    except Exception as exc:  # noqa: BLE001
        print(f"[失敗] 無法初始化 Agent：{exc}")
        return 1
    _hr("小小守護員　對話式稽查助理")
    print("  直接用自然語言問，例如：")
    print("    - 新北市目前風險最高的三間機構是哪些？為什麼？")
    print("    - 快樂幼兒園的財報數字有沒有異常？")
    print("    - 有哪些機構的社群出現不當管教的反映？")
    print("  輸入 exit 離開\n")
    while True:
        try:
            q = input("你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not q:
            continue
        if q.lower() in ("exit", "quit", "q", "離開"):
            break
        try:
            r = ag.ask(q)
        except Exception as exc:  # noqa: BLE001
            print(f"[錯誤] {type(exc).__name__}: {exc}")
            continue
        print(f"\n守護員 > {r['report']}\n")
        print(f"（本次工具呼叫 {r['tool_calls']} 次，累計 session {ag.session_id}）\n")
    return 0


def cmd_realdata(args: argparse.Namespace) -> int:
    from guardian.ingest import loader, pdf_ocr, s3source

    _hr("S3 上的真實資料")
    try:
        objs = s3source.list_objects(args.bucket)
    except Exception as exc:  # noqa: BLE001
        print(f"  [失敗] 無法讀取 S3：{type(exc).__name__}: {exc}")
        return 1
    groups: dict[str, list] = {}
    for o in objs:
        groups.setdefault(o["key"].split("/")[0], []).append(o)
    for top, items in groups.items():
        total = sum(i["size"] for i in items)
        print(f"  {top}/　{len(items)} 檔／{total/1024/1024:,.0f} MB")

    cache = s3source.cache_summary()
    oc = pdf_ocr.cache_stats()
    print(f"\n  本機下載快取：{cache['cached_files']} 檔／"
          f"{cache['cached_bytes']/1024/1024:,.0f} MB")
    print(f"  OCR 抽取快取：{oc.get('docs', 0)} 份文件／{oc.get('files', 0)} 個頁面結果")

    extracted = Path("data/extracted")
    files = sorted(extracted.glob("*.json")) if extracted.exists() else []
    print(f"  已抽取文件　：{len(files)} 份")
    if not files:
        _hr("尚未抽取任何真實資料")
        print("  抽取流程需要呼叫 Bedrock 視覺模型（掃描影像 OCR），費時且會產生費用，")
        print("  因此不併入 start 腳本。要執行請跑：")
        print("    .\\.venv\\Scripts\\python.exe scripts\\ingest_real_data.py inventory")
        print("    .\\.venv\\Scripts\\python.exe scripts\\ingest_real_data.py nonprofit")
        print("    .\\.venv\\Scripts\\python.exe scripts\\ingest_real_data.py public --limit 1")
        return 0

    if args.load:
        _hr("寫入整合資料庫")
        res = loader.load_all(city=args.city, purge_seed_data=not args.keep_seed)
        for k, v in res.items():
            if k == "detail":
                continue
            print(f"  {k:<30}: {v}")
    else:
        _hr("已抽取的文件")
        for f in files:
            print(f"  {f.name}")
        print("\n  加上 --load 才會寫入整合資料庫")
    return 0


def cmd_compliance(args: argparse.Namespace) -> int:
    r = compliance.scan_city(args.city)
    _hr(f"法規遵循全市掃描：{args.city}")
    print(f"  受檢機構       : {r['scanned']} 間")
    print(f"  有違規         : {r['with_violations']} 間")
    print(f"  有重大違規     : {r['with_critical']} 間")
    _hr("違規項目出現頻率")
    for indicator, n in list(r["violation_frequency"].items())[:15]:
        print(f"  {n:>3} 間  {indicator}")
    _hr("法規遵循風險最高的機構")
    print(f"  {'機構':<28} {'子分數':>6} {'違規':>4} {'重大':>4}  主要項目")
    for x in r["results"][:12]:
        if not x["violation_count"]:
            continue
        print(f"  {x['name'].replace('新北市',''):<28} {x['compliance_subscore']:>6.1f} "
              f"{x['violation_count']:>4} {x['critical_count']:>4}  "
              f"{'、'.join(x['top_violations'])}")
    return 0


def cmd_baseline(args: argparse.Namespace) -> int:
    b = forensic.citywide_baseline(args.city)
    _hr(f"全體統計基準：{args.city}（母體 {b['population']} 間）")
    print(f"  σ 門檻：{b['sigma_bands']}")
    print(f"\n  {'指標':<26} {'n':>4} {'平均':>10} {'標準差':>10} {'CV':>7} "
          f"{'2σ 下界':>10} {'2σ 上界':>10}")
    for stat in b["indicators"].values():
        th = stat["thresholds"][f"異常（{config.SIGMA_BANDS['abnormal']}σ）"]
        print(f"  {stat['label']:<26} {stat['n']:>4} {stat['mean']:>10} {stat['std']:>10} "
              f"{(stat['cv'] if stat['cv'] is not None else 0):>7} "
              f"{th['lower']:>10} {th['upper']:>10}")
    print(f"\n  {b['note']}")
    return 0


def cmd_stability(args: argparse.Namespace) -> int:
    d = forensic.district_stability(args.city)
    _hr(f"區域穩定度：{args.city}")
    print(f"  區級基準最低機構數 : {d['min_institutions_for_baseline']} 間")
    print(f"  離散度門檻         : 變異係數 {d['cv_stable_threshold']}／"
          f"離散比 {d['dispersion_ratio_threshold']}")
    print(f"\n  結論：{d['recommendation']}")
    for entry in d["districts"]:
        print(f"\n  {entry['district']}（{entry['n_institutions']} 間）　{entry['assessment']}")
        if entry["underpowered"] and not args.verbose:
            continue
        for label, s in entry["indicators"].items():
            mark = "!" if not s["within_stable_range"] else " "
            print(f"    {mark} {label:<26} 區均 {s['district_mean']:>11} "
                  f"離散 {s['dispersion']:>7}  vs 全市 z={s['mean_z_vs_city']:>6}  "
                  f"{s['mean_level']}")
    print(f"\n  {d['note']}")
    return 0


def cmd_train(args: argparse.Namespace) -> int:
    _hr("監督式模型訓練（以歷史裁罰為 label）")
    r = scoring.train_supervised(args.city)
    for k, v in r.items():
        print(f"  {k:16}: {v}")
    return 0


def _is_loopback(host: str) -> bool:
    import ipaddress

    h = (host or "").strip()
    if h in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def cmd_serve(args: argparse.Namespace) -> int:
    import os

    import uvicorn

    from guardian import config

    exposed = not _is_loopback(args.host)

    # 綁到非 loopback 位址就是對外開放，這時一定要啟用來源 IP 白名單。
    # 用「預設安全」的方式處理：不是靠使用者記得加參數，而是綁定位址一旦
    # 對外就自動開啟；要關掉必須顯式指定 --no-ip-allowlist。
    if exposed and not args.no_ip_allowlist:
        os.environ["GUARDIAN_ENFORCE_IP_ALLOWLIST"] = "1"
        config.ENFORCE_IP_ALLOWLIST = True
    if args.trust_proxy:
        os.environ["GUARDIAN_TRUST_PROXY_HEADER"] = "1"
        config.TRUST_PROXY_HEADER = True

    shown_host = "127.0.0.1" if _is_loopback(args.host) else args.host
    print(f"API + 前端啟動於 http://{shown_host}:{args.port}")

    if exposed:
        print("-" * 72)
        if config.ENFORCE_IP_ALLOWLIST:
            print("  對外開放模式：已啟用來源 IP 白名單")
            for ip in config.ALLOWED_IPS:
                print(f"      允許 {ip}")
            print(f"  信任 X-Forwarded-For：{'是' if config.TRUST_PROXY_HEADER else '否'}")
            if not config.TRUST_PROXY_HEADER:
                print("      （若前面有 ALB／CloudFront，需加 --trust-proxy，"
                      "否則看到的來源會是代理位址而非用戶端）")
        else:
            print("  !! 對外開放但白名單已被停用（--no-ip-allowlist）")
            print("     本服務沒有身分驗證，等於完全公開，請確認這是你要的。")
        print("  提醒：IP 白名單不是身分驗證，無法辨識使用者、無法做權限分級。")
        print("        AWS 端請一併設定 Security Group（見 infra/network_access.py）。")
        print("-" * 72)

    uvicorn.run("api:app", host=args.host, port=args.port, reload=False,
                # 對外開放時把 uvicorn 的 proxy header 解析交給我們自己的中介層，
                # 避免兩邊各自解讀 XFF 造成判斷不一致
                forwarded_allow_ips="*" if args.trust_proxy else None)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="cli.py", description="小小守護員 風險稽查智能代理人")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="檢查 AWS / Bedrock / 資料庫").set_defaults(fn=cmd_check)

    q = sub.add_parser("etl", help="執行資料整合（工具 A）")
    q.add_argument("--city", default="新北市")
    q.add_argument("--offline", action="store_true", help="不嘗試 live 抓取")
    q.set_defaults(fn=cmd_etl)

    q = sub.add_parser("scan", help="規則式全市評分（不呼叫 LLM）")
    q.add_argument("--city", default="新北市")
    q.add_argument("--reset-history", action="store_true", dest="reset_history",
                   help="掃描前先清除舊的評分與預警紀錄")
    q.set_defaults(fn=cmd_scan)

    q = sub.add_parser("board", help="風險排行榜")
    q.add_argument("--city", default="新北市")
    q.add_argument("--top", type=int, default=20)
    q.set_defaults(fn=cmd_board)

    sub.add_parser("alerts", help="未處理預警").set_defaults(fn=cmd_alerts)

    q = sub.add_parser("inspect", help="單一機構完整分析明細（不呼叫 LLM）")
    q.add_argument("name")
    q.set_defaults(fn=cmd_inspect)

    q = sub.add_parser("agent-scan", help="Agent 自主規劃全市掃描")
    q.add_argument("--city", default="新北市")
    q.set_defaults(fn=cmd_agent_scan)

    q = sub.add_parser("investigate", help="Agent 深入調查特定機構")
    q.add_argument("name")
    q.set_defaults(fn=cmd_investigate)

    q = sub.add_parser("why", help="問 Agent 為什麼分數升高")
    q.add_argument("name")
    q.set_defaults(fn=cmd_why)

    q = sub.add_parser("perceive", help="感知層：列出觸發訊號")
    q.add_argument("--city", default="新北市")
    q.set_defaults(fn=cmd_perceive)

    q = sub.add_parser("auto", help="感知 → 自動開調查")
    q.add_argument("--city", default="新北市")
    q.add_argument("--max-targets", type=int, default=2, dest="max_targets")
    q.set_defaults(fn=cmd_auto)

    q = sub.add_parser("chat", help="互動式問答")
    q.add_argument("--verbose", action="store_true", help="顯示工具呼叫過程")
    q.set_defaults(fn=cmd_chat)

    q = sub.add_parser("realdata", help="檢視／載入 S3 上的真實資料")
    q.add_argument("--bucket", default="",
                   help="資料來源 bucket（預設讀環境變數 GUARDIAN_DATA_BUCKET）")
    q.add_argument("--city", default="新北市")
    q.add_argument("--load", action="store_true", help="把已抽取的結果寫入資料庫")
    q.add_argument("--keep-seed", action="store_true",
                   help="保留示範資料（預設會清除，避免混入母體統計）")
    q.set_defaults(fn=cmd_realdata)

    q = sub.add_parser("compliance", help="法規遵循全市掃描（工具 E）")
    q.add_argument("--city", default="新北市")
    q.set_defaults(fn=cmd_compliance)

    q = sub.add_parser("baseline", help="全體統計基準與標準差門檻")
    q.add_argument("--city", default="新北市")
    q.set_defaults(fn=cmd_baseline)

    q = sub.add_parser("stability", help="各區資料穩定度（變異係數）")
    q.add_argument("--city", default="新北市")
    q.add_argument("--verbose", action="store_true", help="連樣本過小的區也列出明細")
    q.set_defaults(fn=cmd_stability)

    q = sub.add_parser("train", help="嘗試訓練監督式模型")
    q.add_argument("--city", default="新北市")
    q.set_defaults(fn=cmd_train)

    q = sub.add_parser("serve", help="啟動 API + 前端")
    q.add_argument("--host", default="127.0.0.1",
                   help="綁定位址。0.0.0.0 = 對外開放（會自動啟用 IP 白名單）")
    q.add_argument("--trust-proxy", action="store_true",
                   help="服務在 ALB／CloudFront 後面時加此參數，才會讀 X-Forwarded-For")
    q.add_argument("--no-ip-allowlist", action="store_true",
                   help="對外開放但不啟用白名單（等於完全公開，不建議）")
    q.add_argument("--port", type=int, default=8000)
    q.set_defaults(fn=cmd_serve)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
