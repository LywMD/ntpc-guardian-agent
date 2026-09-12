"""把 S3 上的真實資料匯入整合資料庫。

子命令
  inventory              列出 S3 上的檔案與本機／OCR 快取狀態
  classify <key>         Pass 1：分類單一 PDF 的每一頁（低成本）
  extract  <key>         Pass 2：抽取表格明細（會呼叫 Bedrock 視覺模型）
  nonprofit [--limit N]  跑完非營利園財報（掃描影像 → OCR）
  public    [--limit N]  跑完公校決算書（文字型 → pdfplumber）
  load                   把已抽取的結果寫入整合資料庫
  all                    inventory → nonprofit → public → load

範例
  python scripts/ingest_real_data.py inventory
  python scripts/ingest_real_data.py classify "非營利園財報/113學年度/N28新樂_113學年度財務報告.pdf"
  python scripts/ingest_real_data.py nonprofit --limit 1
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from guardian.ingest import pdf_ocr, s3source  # noqa: E402

BAR = "-" * 72
PREFIX_NONPROFIT = "非營利園財報/"
PREFIX_PUBLIC = "公校/"


def hr(title: str) -> None:
    print(f"\n{BAR}\n{title}\n{BAR}")


def progress(stage: str, page: int, total: int) -> None:
    label = "分類" if stage == "classify" else "抽取"
    print(f"    {label} 第 {page} 頁 / 共 {total}", flush=True)


def cmd_inventory(args) -> int:
    hr(f"S3 bucket：{args.bucket}")
    objs = s3source.list_objects(args.bucket)
    cache = s3source.cache_summary()
    cached_keys = {k.split("/", 1)[1] for k in cache["keys"] if "/" in k}
    groups: dict[str, list[dict]] = {}
    for o in objs:
        top = o["key"].split("/")[0]
        groups.setdefault(top, []).append(o)
    for top, items in groups.items():
        total = sum(i["size"] for i in items)
        print(f"\n  {top}/　{len(items)} 檔／{total/1024/1024:,.0f} MB")
        for i in items:
            mark = "✓" if i["key"] in cached_keys else " "
            print(f"    {mark} {i['size']:>12,}  {i['key'].split('/', 1)[1]}")
    print(f"\n  本機快取：{cache['cached_files']} 檔／"
          f"{cache['cached_bytes']/1024/1024:,.0f} MB　{cache['cache_dir']}")
    oc = pdf_ocr.cache_stats()
    print(f"  OCR 快取：{oc.get('docs', 0)} 份文件／{oc.get('files', 0)} 個頁面結果"
          f"／{oc.get('bytes', 0)/1024:,.0f} KB")
    print("\n  ✓ = 已下載到本機")
    return 0


def cmd_classify(args) -> int:
    path = s3source.fetch(args.bucket, args.key)
    hr(f"Pass 1 分類：{path.name}（{pdf_ocr.page_count(path)} 頁）")
    res = pdf_ocr.classify_pages(path, max_pages=args.max_pages, on_progress=progress)
    counts: dict[str, int] = {}
    for c in res:
        counts[c.get("type") or "?"] = counts.get(c.get("type") or "?", 0) + 1
    print("\n  頁面類型分佈：")
    for t, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"    {n:>3} 頁  {t}")
    inst = next((c.get("institution") for c in res if c.get("institution")), None)
    print(f"\n  偵測到機構名稱：{inst or '（無）'}")
    targets = pdf_ocr.wanted_pages(res)
    print(f"  值得抽取的頁面（{len(targets)}）：{targets}")
    print("\n  各頁標題：")
    for c in res:
        print(f"    p{c.get('page'):>3}  {(c.get('type') or ''):<12} "
              f"{(c.get('title') or '')[:46]}")
    return 0


def cmd_extract(args) -> int:
    path = s3source.fetch(args.bucket, args.key)
    hr(f"Pass 2 抽取：{path.name}")
    pages = args.pages if args.pages else None
    res = pdf_ocr.extract_document(path, pages=pages, max_extract=args.max_extract,
                                   on_progress=progress)
    print(f"\n  機構：{res['institution']}")
    print(f"  總頁數：{res['total_pages']}　抽取頁面：{res['target_pages']}")
    print(f"  明細筆數：{res['line_item_count']}　耗時 {res['elapsed_s']} 秒")
    for e in res["extracted"]:
        items = e.get("line_items", [])
        print(f"\n  --- p{e.get('page')} {e.get('statement')}　"
              f"期間 {e.get('period')}　單位 {e.get('unit')}　"
              f"信心 {e.get('confidence')} ---")
        if e.get("unreadable"):
            print("      （判定無法辨識）")
        for it in items[:14]:
            print(f"      {(it.get('flow') or ''):<10} {(it.get('subject') or '')[:22]:<24} "
                  f"{it.get('amount')}")
        if len(items) > 14:
            print(f"      …另有 {len(items) - 14} 筆")
        if e.get("totals"):
            print(f"      合計：{e['totals']}")
        if e.get("headcount"):
            print(f"      人數：{e['headcount']}")
    out = Path("data/extracted"); out.mkdir(parents=True, exist_ok=True)
    fp = out / (path.stem + ".json")
    fp.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str),
                  encoding="utf-8")
    print(f"\n  結果已寫入 {fp}")
    return 0


def cmd_nonprofit(args) -> int:
    objs = [o for o in s3source.list_objects(args.bucket, PREFIX_NONPROFIT)
            if o["key"].lower().endswith(".pdf")]
    if args.limit:
        objs = objs[: args.limit]
    hr(f"非營利園財報（掃描影像 → Bedrock 視覺 OCR）　{len(objs)} 份")
    out = Path("data/extracted"); out.mkdir(parents=True, exist_ok=True)
    summary = []
    for i, o in enumerate(objs, start=1):
        print(f"\n  [{i}/{len(objs)}] {o['key']}")
        path = s3source.fetch(args.bucket, o["key"])
        res = pdf_ocr.extract_document(path, max_extract=args.max_extract,
                                       include_low_priority=args.all_pages,
                                       on_progress=progress)
        (out / (path.stem + ".json")).write_text(
            json.dumps(res, ensure_ascii=False, indent=1, default=str),
            encoding="utf-8")
        summary.append({"file": res["file"], "institution": res["institution"],
                        "pages": res["total_pages"],
                        "extracted_pages": len(res["target_pages"]),
                        "line_items": res["line_item_count"],
                        "elapsed_s": res["elapsed_s"]})
        print(f"      機構 {res['institution']}／抽取 {len(res['target_pages'])} 頁"
              f"／{res['line_item_count']} 筆明細／{res['elapsed_s']} 秒")
    hr("非營利園財報彙總")
    for s in summary:
        print(f"  {s['institution'] or s['file']:<34} {s['pages']:>3} 頁　"
              f"抽取 {s['extracted_pages']:>2} 頁　{s['line_items']:>4} 筆")
    return 0


def cmd_public(args) -> int:
    from guardian.ingest import pdf_text

    objs = [o for o in s3source.list_objects(args.bucket, PREFIX_PUBLIC)
            if o["key"].lower().endswith(".pdf")]
    out = Path("data/extracted"); out.mkdir(parents=True, exist_ok=True)

    if args.skip_done:
        pending = []
        for o in objs:
            stem = Path(s3source._safe_name(o["key"])).stem
            if (out / f"{stem}.json").exists():
                print(f"  [跳過已抽取] {o['key']}")
            else:
                pending.append(o)
        objs = pending

    if args.limit:
        objs = objs[: args.limit]
    hr(f"公校決算書（文字型 → pdfplumber）　{len(objs)} 份")
    for i, o in enumerate(objs, start=1):
        print(f"\n  [{i}/{len(objs)}] {o['key']}")
        path = s3source.fetch(args.bucket, o["key"])
        res = pdf_text.parse_settlement(path, on_progress=lambda p, n: (
            print(f"    掃描第 {p} / {n} 頁", flush=True) if p % 100 == 0 else None))
        (out / (path.stem + ".json")).write_text(
            json.dumps(res, ensure_ascii=False, indent=1, default=str),
            encoding="utf-8")
        print(f"      {res['total_pages']} 頁／命中幼兒園 {len(res['relevant_pages'])} 頁"
              f"／表格列 {res['table_row_count']} 筆／機構線索 {len(res['institutions'])} 個")
    return 0


def cmd_units(args) -> int:
    """抽取公校決算書第 5 冊的逐園分決算（各市立幼兒園自己的財務報表）。

    第 1–4 冊是全市層級彙總，只能提供名冊與補助基準；
    第 5 冊收錄「附屬單位決算之分決算」，才有逐園收支、人事費與員工人數。
    """
    from guardian.ingest import pdf_units

    objs = [o for o in s3source.list_objects(args.bucket, PREFIX_PUBLIC)
            if o["key"].lower().endswith(".pdf")]
    # 只有第 5 冊有逐園分決算
    objs = [o for o in objs if "第5冊" in o["key"] or "第五冊" in o["key"]]
    out = Path("data/extracted"); out.mkdir(parents=True, exist_ok=True)

    if args.skip_done:
        pending = []
        for o in objs:
            stem = Path(s3source._safe_name(o["key"])).stem
            if (out / f"{stem}.units.json").exists():
                print(f"  [跳過已抽取] {o['key']}")
            else:
                pending.append(o)
        objs = pending

    if args.limit:
        objs = objs[: args.limit]
    hr(f"公校逐園分決算（第5冊 → pdfplumber）　{len(objs)} 份")
    for i, o in enumerate(objs, start=1):
        print(f"\n  [{i}/{len(objs)}] {o['key']}")
        path = s3source.fetch(args.bucket, o["key"])
        res = pdf_units.parse_unit_settlements(path, on_progress=lambda p, n: (
            print(f"    掃描第 {p} / {n} 頁", flush=True) if p % 150 == 0 else None))
        fp = out / (path.stem + ".units.json")
        fp.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str),
                      encoding="utf-8")
        print(f"      {res.get('total_pages')} 頁／逐園頁面 {res.get('unit_page_count')}"
              f"／機構 {res.get('unit_count')} 間"
              f"／明細 {res.get('line_item_count')} 筆"
              f"／有員工人數 {res.get('units_with_headcount')} 間")
    return 0


def cmd_reexpand(args) -> int:
    """用目前的欄位對照規則，從 OCR 頁面快取重建 data/extracted（不呼叫 Bedrock）。

    欄位別名／多年度寬表的解析規則改動後，不需要重跑 OCR，
    只要重新展開一次快取即可，成本為零。
    """
    from guardian.ingest import pdf_ocr

    hr("從 OCR 快取重新展開抽取結果（不花 token）")
    out = Path("data/extracted"); out.mkdir(parents=True, exist_ok=True)
    objs = [o for o in s3source.list_objects(args.bucket)
            if o["key"].lower().endswith(".pdf")]
    done = 0
    for o in objs:
        local = pdf_ocr.config.RAW_DIR / "s3" / s3source._safe_name(o["key"])
        if not local.exists():
            continue
        cache_dir = pdf_ocr.OCR_CACHE / pdf_ocr._doc_id(local)
        if not cache_dir.exists():
            continue
        pages = sorted(int(p.stem.split("_p")[1])
                       for p in cache_dir.glob("extract_p*.json"))
        if not pages:
            continue
        res = pdf_ocr.extract_document(local, pages=pages)
        fp = out / (local.stem + ".json")
        fp.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str),
                      encoding="utf-8")
        with_amt = sum(
            1 for e in res["extracted"] for it in e.get("line_items", [])
            if it.get("amount") not in (None, ""))
        print(f"  {local.name}")
        print(f"    機構 {res['institution']}　頁面 {len(pages)}"
              f"　明細 {res['line_item_count']} 筆（其中有金額 {with_amt} 筆）")
        done += 1
    print(f"\n  重建 {done} 份")
    return 0


def cmd_load(args) -> int:
    from guardian.ingest import loader

    hr("寫入整合資料庫")
    res = loader.load_all(city=args.city, purge_seed_data=not args.keep_seed)
    detail = res.pop("detail", {})
    for k, v in res.items():
        print(f"  {k:<30}: {v}")
    hr("各機構明細")
    for n in detail.get("nonprofit", []):
        if n.get("skipped"):
            print(f"  [跳過] {n['skipped']}：{n.get('reason')}")
            continue
        print(f"\n  {n.get('institution')}（{n.get('inst_id')}）")
        print(f"    來源檔案 : {n.get('source_file')}")
        print(f"    年度     : {n.get('years')}")
        print(f"    財務明細 : {n.get('financial_rows')} 筆")
        print(f"    使用頁面 : {n.get('pages_used')}")
        if n.get("hr_expense_by_year"):
            print(f"    人事費   : {n['hr_expense_by_year']}")
        if n.get("headcount"):
            print(f"    人數     : {n['headcount']}")
        for s in n.get("skipped_related_entity_pages", []):
            print(f"    [排除關聯實體] p{s['page']} {s['entity']}"
                  f"（{s['statement']}，{s['line_items']} 筆）")
        for lc in n.get("low_confidence_pages", []):
            print(f"    [低信心] p{lc['page']} {lc['statement']} confidence={lc['confidence']}")
    for p in detail.get("public", []):
        print(f"\n  {p.get('file')}　年度 {p.get('year')}")
        print(f"    收費／補助基準 : {p.get('fee_standard_rows')} 筆")
        print(f"    機構名冊       : {p.get('institutions_from_roster')} 間")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="匯入 S3 上的真實資料")
    ap.add_argument("--bucket", default=s3source.DEFAULT_BUCKET)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("inventory").set_defaults(fn=cmd_inventory)

    q = sub.add_parser("classify")
    q.add_argument("key")
    q.add_argument("--max-pages", type=int)
    q.set_defaults(fn=cmd_classify)

    q = sub.add_parser("extract")
    q.add_argument("key")
    q.add_argument("--pages", nargs="*", type=int)
    q.add_argument("--max-extract", type=int)
    q.set_defaults(fn=cmd_extract)

    q = sub.add_parser("nonprofit")
    q.add_argument("--limit", type=int)
    q.add_argument("--max-extract", type=int)
    q.add_argument("--all-pages", action="store_true",
                   help="連財產目錄等低優先頁面也抽（較耗時與 token）")
    q.set_defaults(fn=cmd_nonprofit)

    q = sub.add_parser("public")
    q.add_argument("--limit", type=int)
    q.add_argument("--skip-done", action="store_true",
                   help="跳過 data/extracted 已有結果的冊次")
    q.set_defaults(fn=cmd_public)

    q = sub.add_parser("units", help="抽取公校第5冊的逐園分決算")
    q.add_argument("--limit", type=int)
    q.add_argument("--skip-done", action="store_true")
    q.set_defaults(fn=cmd_units)

    sub.add_parser("reexpand").set_defaults(fn=cmd_reexpand)

    q = sub.add_parser("load")
    q.add_argument("--city", default="新北市")
    q.add_argument("--keep-seed", action="store_true",
                   help="保留示範資料（預設清除，避免混入母體統計）")
    q.set_defaults(fn=cmd_load)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
