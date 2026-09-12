"""探查公校決算書第五冊：那幾百頁「命中幼兒園」的內容到底是什麼。

關鍵問題：裡面有沒有「逐園」的金額明細？
若有 → 公校附幼／市立幼兒園可以有真實財務數字，不再只是名冊。
若沒有 → 必須誠實說明公校資料的上限。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "data" / "probe_vol5.txt"
buf: list[str] = []

TARGET = ROOT / "data" / "raw" / "s3" / "公校" / "112年度決算書" / "第5冊" / "112年決算書第五冊.pdf"
EXTRACTED = ROOT / "data" / "extracted" / "112年決算書第五冊.json"

# 市立幼兒園名稱樣式，用來判斷某頁是否針對「單一機構」
INST_PAT = re.compile(r"新北市立[\u4e00-\u9fff]{2,4}幼兒園|[\u4e00-\u9fff]{2,6}"
                      r"(?:國民小學|國小|國中)附設幼兒園")
MONEY = re.compile(r"[\d,]{5,}")


def w(s: str = "") -> None:
    buf.append(s)


def main() -> None:
    if not EXTRACTED.exists():
        w(f"找不到 {EXTRACTED}")
        return
    d = json.loads(EXTRACTED.read_text(encoding="utf-8"))
    rel = d.get("relevant_pages") or []
    w(f"抽取結果：{d.get('total_pages')} 頁，命中 {len(rel)} 頁")
    w(f"命中頁碼前 40 = {rel[:40]}")
    w(f"表格列 {d.get('table_row_count')} 筆、補助基準 {len(d.get('fee_standards') or [])} 筆")
    w(f"名冊 {len(d.get('institutions') or {})} 間")
    w()

    if not TARGET.exists():
        w(f"找不到原始 PDF：{TARGET}")
        return

    try:
        import pdfplumber
    except ImportError:
        w("pdfplumber 未安裝")
        return

    # 取命中頁中分散的幾頁，看實際文字長什麼樣
    sample_pages = rel[:3] + rel[len(rel) // 3: len(rel) // 3 + 3] + rel[-3:]
    sample_pages = sorted(set(sample_pages))

    single_inst_pages = 0
    with pdfplumber.open(str(TARGET)) as pdf:
        w("=" * 72)
        w("抽樣頁面實際內容")
        w("=" * 72)
        for pno in sample_pages:
            try:
                text = pdf.pages[pno - 1].extract_text() or ""
            except Exception as exc:  # noqa: BLE001
                w(f"  第 {pno} 頁讀取失敗：{exc}")
                continue
            names = set(INST_PAT.findall(text))
            monies = MONEY.findall(text)
            w(f"\n  --- 第 {pno} 頁　機構名 {len(names)} 個　金額樣式 {len(monies)} 個 ---")
            if names:
                w(f"      機構：{sorted(names)[:6]}")
            w("      前 600 字：")
            for line in text[:600].splitlines()[:16]:
                w(f"        {line}")

        # 統計：有多少命中頁「只提到一間機構」（代表是逐園頁）
        w()
        w("=" * 72)
        w("逐園頁面統計（掃描全部命中頁）")
        w("=" * 72)
        by_count: dict[int, int] = {}
        pages_with_one: list[tuple[int, str]] = []
        for pno in rel:
            try:
                text = pdf.pages[pno - 1].extract_text() or ""
            except Exception:  # noqa: BLE001
                continue
            names = sorted(set(INST_PAT.findall(text)))
            by_count[len(names)] = by_count.get(len(names), 0) + 1
            if len(names) == 1:
                single_inst_pages += 1
                if len(pages_with_one) < 25:
                    pages_with_one.append((pno, names[0]))
        w(f"  命中頁的機構名數量分布 = {dict(sorted(by_count.items()))}")
        w(f"  只提到 1 間機構的頁數 = {single_inst_pages}")
        if pages_with_one:
            w("  範例（頁碼 → 機構）：")
            for pno, nm in pages_with_one:
                w(f"      p{pno} → {nm}")


if __name__ == "__main__":
    main()
    OUT.write_text("\n".join(buf), encoding="utf-8")
