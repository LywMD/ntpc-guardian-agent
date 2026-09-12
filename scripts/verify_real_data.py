"""驗證真實資料載入後，五大工具是否都能正常運作。

會用獨立的暫存資料庫，不影響 data/guardian.db。

用法
  python scripts/verify_real_data.py
  python scripts/verify_real_data.py --keep   # 保留暫存 DB 以便檢查
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TMP_DB = ROOT / "data" / "verify_real.db"
os.environ["GUARDIAN_DB"] = str(TMP_DB)

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'[PASS]' if cond else '[FAIL]'} {name}{'  ' + detail if detail else ''}")
    if not cond:
        FAILED.append(name)


def hr(t: str) -> None:
    print("\n" + "=" * 74)
    print(t)
    print("=" * 74)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    for suffix in ("", "-wal", "-shm"):
        p = Path(str(TMP_DB) + suffix)
        if p.exists():
            p.unlink()

    from guardian import store  # noqa: E402
    from guardian.ingest import loader  # noqa: E402
    from guardian.tools import compliance, forensic, scoring  # noqa: E402

    hr(f"載入真實資料到暫存資料庫\n{TMP_DB}")
    res = loader.load_all(city="新北市", purge_seed_data=True)
    if res.get("error"):
        print(f"  [失敗] {res['error']}")
        return 1
    detail = res.pop("detail", {})
    for k, v in res.items():
        print(f"  {k:<30}: {v}")

    check("有載入非營利園財報", res["nonprofit_reports_loaded"] > 0,
          f"{res['nonprofit_reports_loaded']} 份")
    check("財務明細有寫入", res["nonprofit_financial_rows"] > 0,
          f"{res['nonprofit_financial_rows']} 筆")
    check("資料來源模式標記為 real", res["data_source_mode"] == "real",
          res["data_source_mode"])

    hr("各機構載入明細")
    insts: list[dict] = []
    for n in detail.get("nonprofit", []):
        if n.get("skipped"):
            print(f"  [跳過] {n['skipped']}：{n.get('reason')}")
            continue
        insts.append(n)
        print(f"\n  {n['institution']}（{n['inst_id']}）")
        print(f"    來源     : {n.get('source_file')}")
        print(f"    年度     : {n.get('years')}")
        print(f"    明細筆數 : {n.get('financial_rows')}")
        print(f"    人事費   : {n.get('hr_expense_by_year')}")
        for s in n.get("skipped_related_entity_pages", []):
            print(f"    [排除關聯實體] p{s['page']} {s['entity']}（{s['line_items']} 筆）")
        for lc in n.get("low_confidence_pages", []):
            print(f"    [低信心] p{lc['page']} {lc['statement']} conf={lc['confidence']}")

    check("每間機構都有明細", all(n.get("financial_rows", 0) > 0 for n in insts))
    check("有排除關聯實體的頁面（避免污染比率）",
          any(n.get("skipped_related_entity_pages") for n in insts),
          "；".join(f"{n['institution']}: "
                    + "、".join(s["entity"] for s in n["skipped_related_entity_pages"])
                    for n in insts if n.get("skipped_related_entity_pages")) or "無")

    if not insts:
        print("\n  沒有任何機構載入成功，中止後續驗證")
        return 1

    hr("工具 B：鑑識會計（跑在真實財報上）")
    for n in insts:
        iid = n["inst_id"]
        f = forensic.scan(iid)
        m = f["signals"]["ratio_analysis"]["metrics"]
        b = f["signals"]["benford_first_digit"]
        print(f"\n  {n['institution']}　財務異常子分數 {f['financial_subscore']}")
        print(f"    收入合計   : {m.get('income_total'):,.0f}" if m.get("income_total")
              else "    收入合計   : 無")
        print(f"    支出合計   : {m.get('expense_total'):,.0f}" if m.get("expense_total")
              else "    支出合計   : 無")
        print(f"    人事費     : {m.get('hr_expense'):,.0f}" if m.get("hr_expense")
              else "    人事費     : 無")
        pct = m.get("hr_expense_pct")
        print(f"    人事費占比 : {pct:.1%}" if pct else "    人事費占比 : 無")
        print(f"    Benford    : n={b.get('n')} MAD={b.get('MAD')} "
              f"p={b.get('p_value')} → {b.get('conclusion')}")
        for e in f["evidence"][:4]:
            print(f"    - {e[:120]}")

    first = insts[0]["inst_id"]
    fscan = forensic.scan(first)
    check("財務異常子分數可算出", 0 <= fscan["financial_subscore"] <= 100,
          str(fscan["financial_subscore"]))
    mm = fscan["signals"]["ratio_analysis"]["metrics"]
    check("真實財報算得出收支合計",
          bool(mm.get("income_total")) and bool(mm.get("expense_total")),
          f"收入 {mm.get('income_total')} / 支出 {mm.get('expense_total')}")
    check("真實財報算得出人事費占比", mm.get("hr_expense_pct") is not None,
          f"{(mm.get('hr_expense_pct') or 0):.1%}")
    bb = fscan["signals"]["benford_first_digit"]
    check("Benford 樣本數足夠（真實逐筆金額）", bb.get("n", 0) >= 25, f"n={bb.get('n')}")

    hr("工具 E：法規遵循（真實資料缺年齡層欄位，應誠實回報待補）")
    cc = compliance.check(first)
    print(f"  {cc['institution']}　子分數 {cc['compliance_subscore']}"
          f"／違規 {cc['violation_count']}／待補資料 {len(cc['unverifiable'])}")
    for u in cc["unverifiable"][:8]:
        print(f"    [待補] {u['indicator']}（缺 {u['missing_fields']}）")
    check("缺欄位有列入待補而非誤判合規", len(cc["unverifiable"]) > 0,
          f"{len(cc['unverifiable'])} 項")

    hr("工具 D：風險評分")
    for n in insts:
        r = scoring.score_institution(n["inst_id"], sentiment_days=365)
        print(f"  {r['institution']:<28} 總分 {r['total_score']:>5.1f}  {r['risk_level']:<6}"
              f"  {r['subscores']}")
    lb = scoring.leaderboard("新北市", 20)
    check("排行榜可產生", lb["count"] > 0, f"{lb['count']} 筆")

    hr("全體統計基準（母體已換成真實機構）")
    base = forensic.citywide_baseline("新北市")
    print(f"  母體 {base['population']} 間　指標 {len(base['indicators'])} 項")
    for stat in base["indicators"].values():
        print(f"    {stat['label']:<26} n={stat['n']:<3} 平均 {stat['mean']:>14,.4f} "
              f"標準差 {stat['std']:>14,.4f}")
    check("統計基準以真實機構為母體", base["population"] >= len(insts),
          f"{base['population']} 間")

    hr("收費／補助基準（公校決算書抽出）")
    fs = store.q("SELECT year, category, amount, source FROM fee_standards"
                 " ORDER BY amount DESC LIMIT 12")
    for r in fs:
        print(f"  {r['year']}　{r['category']:<6} {r['amount']:>10,.0f} 元"
              f"　{(r['source'] or '')[:40]}")
    check("有抽到收費／補助基準", len(fs) > 0, f"{len(fs)} 筆")

    hr("資料出處追溯")
    from guardian.tools import etl

    ds = etl.data_sources()
    print(f"  模式：{ds['data_source_mode']}　{ds['mode_meaning']}")
    for s in ds["financial_sources"][:10]:
        print(f"    {s['rows']:>5} 筆　{s['year_range']}　{s['source'][:56]}")
    check("財務明細來源可追溯到頁面", len(ds["financial_sources"]) > 0)
    check("真實檔案清單有記錄", bool(ds.get("real_data_files")))

    print("\n" + "=" * 74)
    if FAILED:
        print(f"失敗 {len(FAILED)} 項：")
        for x in FAILED:
            print("  -", x)
        return 1
    print("全部通過")
    if not args.keep:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(TMP_DB) + suffix)
            if p.exists():
                p.unlink()
        print(f"（已清除暫存資料庫；要保留請加 --keep）")
    else:
        print(f"暫存資料庫保留於 {TMP_DB}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
