"""針對特定頁面驗證逐園分決算解析器（快速，不掃整冊）。

已人工核對的基準（112年決算書第五冊）：
  p720 新北市立烏來幼兒園 收入支出表
        收入 8,561,107（上年度 9,549,064）／政府撥入收入 7,846,468
  p722 新北市立烏來幼兒園 決算與會計收支對照表
        基金來源 8,495,375／用人費用 7,897,081／教學收入 648,400
  p302 新北市立樹林幼兒園 決算與會計收支對照表
        基金來源 24,738,982／政府撥入收入 22,078,861
        教學收入 2,657,800／用人費用 22,453,843
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from guardian.ingest import pdf_units  # noqa: E402

OUT = ROOT / "data" / "test_units_pages.txt"
TARGET = (ROOT / "data" / "raw" / "s3" / "公校" / "112年度決算書" / "第5冊"
          / "112年決算書第五冊.pdf")

PAGES = [302, 720, 721, 722, 123]     # 含樹林對照表、烏來三張表、板橋員工人數

buf: list[str] = []
problems: list[str] = []


def w(s: str = "") -> None:
    buf.append(s)


def check(label: str, ok: bool, detail: str = "") -> None:
    w(f"  [{'OK  ' if ok else 'FAIL'}] {label}{('　' + detail) if detail else ''}")
    if not ok:
        problems.append(label)


def find(items, subject, statement=None, flow=None):
    for it in items:
        if it["subject"] != subject:
            continue
        if statement is not None and it["statement"] != statement:
            continue
        if flow is not None and it["flow"] != flow:
            continue
        return it
    return None


def main() -> None:
    res = pdf_units.parse_unit_settlements(TARGET, pages=PAGES)
    if res.get("error"):
        w(str(res["error"]))
        problems.append("解析失敗")
        return

    w(f"解析頁面 {PAGES}")
    w(f"機構 {res['unit_count']} 間　明細 {res['line_item_count']} 筆"
      f"　年度 民國 {res['year_roc']}")
    w()

    for inst, u in res["units"].items():
        w(f"--- {inst} ---")
        w(f"    頁面 {u['pages']}　報表 {u['statements']}")
        hc = {k: v for k, v in (u.get("headcount") or {}).items()
              if not k.startswith("_")}
        if hc:
            w(f"    員工人數 {hc}")
        for it in u["line_items"]:
            w(f"      p{it['page']} [{it['role']:<11}] {it['statement']:<18}"
              f" {it['subject']:<16} {it['flow']:<8} {it['amount']:>16,.0f}"
              f"{('　上年度 ' + format(it['prior_amount'], ',.0f')) if it.get('prior_amount') else ''}")
        w()

    w("=" * 72)
    w("對照人工核對基準")
    w("=" * 72)

    wulai = res["units"].get("新北市立烏來幼兒園")
    check("抓到烏來", wulai is not None)
    if wulai:
        it = wulai["line_items"]
        inc = find(it, "收入", "收入支出表")
        check("烏來 收入 8,561,107 / 上年度 9,549,064",
              inc and abs(inc["amount"] - 8561107) < 1
              and inc.get("prior_amount") and abs(inc["prior_amount"] - 9549064) < 1,
              f"{inc and inc['amount']} / {inc and inc.get('prior_amount')}")
        gov = find(it, "政府撥入收入", "收入支出表")
        check("烏來 政府撥入收入 7,846,468",
              gov and abs(gov["amount"] - 7846468) < 1, str(gov and gov["amount"]))
        hr = find(it, "用人費用", "決算與會計收支對照表")
        check("烏來 用人費用 7,897,081（決算與會計收支對照表）",
              hr and abs(hr["amount"] - 7897081) < 1, str(hr and hr["amount"]))
        check("烏來 用人費用歸類為支出", hr and hr["flow"] == "expense",
              str(hr and hr["flow"]))
        src = find(it, "基金來源", "決算與會計收支對照表")
        check("烏來 基金來源 8,495,375",
              src and abs(src["amount"] - 8495375) < 1, str(src and src["amount"]))

    shulin = res["units"].get("新北市立樹林幼兒園")
    check("抓到樹林", shulin is not None)
    if shulin:
        it = shulin["line_items"]
        for subject, want in (("基金來源", 24738982), ("政府撥入收入", 22078861),
                              ("教學收入", 2657800), ("用人費用", 22453843)):
            got = find(it, subject, "決算與會計收支對照表")
            check(f"樹林 {subject} {want:,}",
                  got and abs(got["amount"] - want) < 1, str(got and got["amount"]))

    banqiao = res["units"].get("新北市立板橋幼兒園")
    if banqiao:
        hc = banqiao.get("headcount") or {}
        check("板橋 有員工人數合計", bool(hc.get("total")), str(hc.get("total")))

    w()
    w("=" * 72)
    w(f"結論：{'全部通過' if not problems else f'{len(problems)} 項未通過'}")
    for p in problems:
        w(f"  - {p}")
    w("=" * 72)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        import traceback
        buf.append(traceback.format_exc())
        problems.append("腳本異常")
    OUT.write_text("\n".join(buf), encoding="utf-8")
    raise SystemExit(1 if problems else 0)
