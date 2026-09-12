"""從 S3 下載指定 PDF 並探測結構，判斷該用哪種解析策略。

用法
  python scripts/probe_pdf.py --list
  python scripts/probe_pdf.py --key "非營利園財報/113學年度/N28新樂_113學年度財務報告.pdf"
  python scripts/probe_pdf.py --key "..." --pages 1 2 3 40 41 --dump
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from guardian import config  # noqa: E402
from guardian.ingest import s3source  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", default=s3source.DEFAULT_BUCKET)
    ap.add_argument("--key")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--pages", nargs="*", type=int, default=[1, 2, 3, 4, 5])
    ap.add_argument("--dump", action="store_true", help="印出整頁文字")
    ap.add_argument("--chars", type=int, default=1200)
    args = ap.parse_args()

    if args.list or not args.key:
        objs = s3source.list_objects(args.bucket)
        print(f"bucket {args.bucket}　{len(objs)} 物件\n")
        for o in objs:
            print(f"  {o['size']:>12,}  {o['key']}")
        if not args.key:
            return 0

    path = s3source.fetch(args.bucket, args.key)
    print(f"\n本機檔案：{path}　{path.stat().st_size:,} bytes")

    import pdfplumber

    with pdfplumber.open(path) as pdf:
        n = len(pdf.pages)
        print(f"總頁數：{n}")

        # 抽樣判斷是文字型還是掃描影像
        sample_idx = sorted({0, 1, 2, n // 4, n // 2, (3 * n) // 4, n - 1})
        text_pages = 0
        total_chars = 0
        for i in sample_idx:
            pg = pdf.pages[i]
            t = pg.extract_text() or ""
            total_chars += len(t)
            if len(t.strip()) > 50:
                text_pages += 1
        print(f"抽樣 {len(sample_idx)} 頁：{text_pages} 頁可取到文字，"
              f"平均 {total_chars // max(1, len(sample_idx))} 字元／頁")
        if text_pages == 0:
            print("→ 判定：掃描影像型 PDF，pdfplumber 取不到文字，需走 OCR（Textract）")
        elif text_pages < len(sample_idx):
            print("→ 判定：混合型，部分頁面需 OCR")
        else:
            print("→ 判定：文字型 PDF，可直接用 pdfplumber 解析")

        # 表格偵測
        probe = pdf.pages[min(n - 1, max(1, n // 3))]
        tables = probe.extract_tables()
        print(f"第 {probe.page_number} 頁偵測到 {len(tables)} 個表格")
        if tables:
            t0 = tables[0]
            print(f"  第一個表格 {len(t0)} 列 × {max(len(r) for r in t0)} 欄")
            for row in t0[:6]:
                print("   |", " | ".join((c or "").replace("\n", " ")[:18] for c in row))

        print("\n" + "=" * 74)
        for p in args.pages:
            if p < 1 or p > n:
                continue
            pg = pdf.pages[p - 1]
            t = pg.extract_text() or ""
            print(f"\n--- 第 {p} 頁（{len(t)} 字元）---")
            print(t if args.dump else t[: args.chars])

        # 找關鍵字出現在哪些頁
        print("\n" + "=" * 74)
        print("關鍵字掃描（前 120 頁）")
        keywords = ["幼兒園", "教保", "非營利", "學費", "雜費", "代辦費",
                    "人事費", "收入", "支出", "決算", "在園", "師生"]
        hits: dict[str, list[int]] = {k: [] for k in keywords}
        for i, pg in enumerate(pdf.pages[:120], start=1):
            t = pg.extract_text() or ""
            for k in keywords:
                if k in t:
                    hits[k].append(i)
        for k, pages in hits.items():
            if pages:
                print(f"  {k:<6} {len(pages)} 頁，前幾頁：{pages[:12]}")
            else:
                print(f"  {k:<6} 未出現")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
