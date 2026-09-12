"""盤點公校決算書逐園頁面的報表種類，作為 role 指派依據。

同一間園同一年度會有多張涵蓋同一筆錢的報表（收入支出表、決算與會計收支對照表、
基金來源用途餘絀表…），必須挑一張當權威來源算合計，其餘另作他用，
否則收入與支出會被重複累加。這支腳本先把種類與頁數統計出來。
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "data" / "survey_units.txt"
RAW = ROOT / "data" / "raw" / "s3" / "公校"

INST_PAT = re.compile(r"新北市立[\u4e00-\u9fff]{2,4}幼兒園"
                      r"|[\u4e00-\u9fff]{2,8}(?:國民小學|國小|國民中學|國中)附設幼兒園")

# 報表標題通常在前 3 行
TITLE_HINTS = (
    "收入支出表", "決算與會計收支對照表", "基金來源、用途及餘絀表", "基金來源用途及餘絀表",
    "基金來源明細表", "基金用途明細表", "資本資產明細表", "現金流量表",
    "資產負債表", "淨值變動表", "平衡表", "總說明", "目次", "目 次",
    "分決算", "預算數與決算數", "固定資產", "投資", "長期",
)


def main() -> None:
    try:
        import pdfplumber
    except ImportError:
        OUT.write_text("pdfplumber 未安裝", encoding="utf-8")
        return

    buf: list[str] = []
    files = sorted(RAW.rglob("*第五冊.pdf"))
    buf.append(f"掃描 {len(files)} 個第五冊檔案")

    for fp in files:
        buf.append("")
        buf.append("=" * 72)
        buf.append(fp.name)
        buf.append("=" * 72)
        titles: Counter[str] = Counter()
        inst_pages: Counter[str] = Counter()
        untitled_samples: list[str] = []
        unit_notes: Counter[str] = Counter()

        with pdfplumber.open(str(fp)) as pdf:
            total = len(pdf.pages)
            buf.append(f"  總頁數 {total}")
            for i in range(total):
                try:
                    text = pdf.pages[i].extract_text() or ""
                except Exception:  # noqa: BLE001
                    continue
                if not text:
                    continue
                names = set(INST_PAT.findall(text))
                if len(names) != 1:
                    continue
                inst = next(iter(names))
                inst_pages[inst] += 1

                head = "\n".join(text.splitlines()[:4])
                found = [h for h in TITLE_HINTS if h in head]
                if found:
                    titles[found[0]] += 1
                else:
                    titles["(未識別標題)"] += 1
                    if len(untitled_samples) < 12:
                        untitled_samples.append(
                            f"      p{i+1} {head[:150]!r}")
                if "單位：" in text:
                    m = re.search(r"單位：\s*([^\s\n]{1,12})", text)
                    if m:
                        unit_notes[m.group(1)] += 1

        buf.append(f"  逐園頁面 {sum(inst_pages.values())} 頁／涵蓋 {len(inst_pages)} 間園")
        buf.append("  報表種類分布：")
        for t, n in titles.most_common():
            buf.append(f"      {t:<24} {n} 頁")
        buf.append("  金額單位標註：")
        for u, n in unit_notes.most_common(6):
            buf.append(f"      {u:<12} {n} 頁")
        buf.append("  每園頁數（前 12 間）：")
        for inst, n in inst_pages.most_common(12):
            buf.append(f"      {inst:<24} {n} 頁")
        if untitled_samples:
            buf.append("  未識別標題樣本：")
            buf += untitled_samples

    OUT.write_text("\n".join(buf), encoding="utf-8")


if __name__ == "__main__":
    main()
