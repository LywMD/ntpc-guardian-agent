"""測試逐園分決算解析器：抽出的金額要能對上 PDF 原文。

已人工核對的基準（112年決算書第五冊）：
  新北市立烏來幼兒園  收入支出表 p720
      收入(本年度) 8,561,107　收入(上年度) 9,549,064　比較增減 -10.35%
      政府撥入收入 7,846,468
  新北市立烏來幼兒園  決算與會計收支對照表 p722
      基金來源 8,495,375　用人費用 7,897,081
  新北市立樹林幼兒園  決算與會計收支對照表 p302
      基金來源 24,738,982　政府撥入收入 22,078,861
      教學收入 2,657,800　用人費用 22,453,843
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from guardian.ingest import pdf_units  # noqa: E402

OUT = ROOT / "data" / "test_pdf_units.txt"
TARGET = (ROOT / "data" / "raw" / "s3" / "公校" / "112年度決算書" / "第5冊"
          / "112年決算書第五冊.pdf")

buf: list[str] = []
problems: list[str] = []


def w(s: str = "") -> None:
    buf.append(s)


def check(label: str, ok: bool, detail: str = "") -> None:
    w(f"  [{'OK  ' if ok else 'FAIL'}] {label}{('　' + detail) if detail else ''}")
    if not ok:
        problems.append(label)


def find(items: list[dict], subject: str, statement: str | None = None):
    for it in items:
        if it["subject"] == subject and (statement is None
                                         or it["statement"] == statement):
            return it
    return None


def main() -> None:
    if not TARGET.exists():
        w(f"找不到 {TARGET}")
        problems.append("缺少測試檔案")
        return

    res = pdf_units.parse_unit_settlements(
        TARGET,
        on_progress=lambda p, n: (print(f"  {p}/{n}", flush=True)
                                  if p % 150 == 0 else None))
    if res.get("error"):
        w(str(res["error"]))
        problems.append("解析失敗")
        return

    w(f"檔案 {res['file']}")
    w(f"總頁數 {res['total_pages']}　逐園頁面 {res['unit_page_count']}"
      f"　機構 {res['unit_count']} 間　明細 {res['line_item_count']} 筆")
    w(f"年度 民國 {res['year_roc']} 年（西元 {res['year_ad']}）")
    check("有抽到逐園機構", res["unit_count"] >= 20, f"{res['unit_count']} 間")
    check("年度正確辨識為民國112年", res["year_roc"] == 112, str(res["year_roc"]))

    w()
    w("各園頁數與報表種類（前 8 間）：")
    for inst, u in list(res["units"].items())[:8]:
        w(f"  {inst}　{len(u['pages'])} 頁　{u['statements']}")

    w()
    w("=" * 72)
    w("對照人工核對基準")
    w("=" * 72)

    wulai = res["units"].get("新北市立烏來幼兒園")
    check("抓到 新北市立烏來幼兒園", wulai is not None)
    if wulai:
        items = wulai["line_items"]
        inc = find(items, "收入", "收入支出表")
        w(f"  烏來 收入支出表『收入』= {inc}")
        check("烏來 本年度收入 8,561,107",
              inc is not None and abs(inc["amount"] - 8561107) < 1,
              str(inc and inc["amount"]))
        check("烏來 上年度收入 9,549,064",
              inc is not None and inc.get("prior_amount") is not None
              and abs(inc["prior_amount"] - 9549064) < 1,
              str(inc and inc.get("prior_amount")))
        gov = find(items, "政府撥入收入", "收入支出表")
        check("烏來 政府撥入收入 7,846,468",
              gov is not None and abs(gov["amount"] - 7846468) < 1,
              str(gov and gov["amount"]))
        hr = find(items, "用人費用", "決算與會計收支對照表")
        check("烏來 用人費用 7,897,081",
              hr is not None and abs(hr["amount"] - 7897081) < 1,
              str(hr and hr["amount"]))

    shulin = res["units"].get("新北市立樹林幼兒園")
    check("抓到 新北市立樹林幼兒園", shulin is not None)
    if shulin:
        items = shulin["line_items"]
        src = find(items, "基金來源", "決算與會計收支對照表")
        check("樹林 基金來源 24,738,982",
              src is not None and abs(src["amount"] - 24738982) < 1,
              str(src and src["amount"]))
        teach = find(items, "教學收入", "決算與會計收支對照表")
        check("樹林 教學收入 2,657,800",
              teach is not None and abs(teach["amount"] - 2657800) < 1,
              str(teach and teach["amount"]))
        hr = find(items, "用人費用", "決算與會計收支對照表")
        check("樹林 用人費用 22,453,843",
              hr is not None and abs(hr["amount"] - 22453843) < 1,
              str(hr and hr["amount"]))

    w()
    w("=" * 72)
    w("員工人數（公校幼兒園原本完全沒有人員數欄位）")
    w("=" * 72)
    w(f"  有抓到人數的機構：{res.get('units_with_headcount')} / {res['unit_count']}")
    for inst, u in list(res["units"].items())[:8]:
        hc = {k: v for k, v in (u.get("headcount") or {}).items()
              if not k.startswith("_")}
        w(f"      {inst:<24} {hc}")
    check("有抽到員工人數", (res.get("units_with_headcount") or 0) > 0,
          str(res.get("units_with_headcount")))

    w()
    w("=" * 72)
    w("收支方向與 role 分布")
    w("=" * 72)
    roles: dict[str, int] = {}
    flows: dict[str, int] = {}
    for u in res["units"].values():
        for it in u["line_items"]:
            roles[it["role"]] = roles.get(it["role"], 0) + 1
            flows[it["flow"]] = flows.get(it["flow"], 0) + 1
    w(f"  role = {roles}")
    w(f"  flow = {flows}")
    check("primary 報表有抽到資料", roles.get("primary", 0) > 0,
          str(roles.get("primary")))
    check("收入與支出都有", flows.get("income", 0) > 0 and flows.get("expense", 0) > 0,
          str(flows))

    # 存一份樣本供人工檢視
    sample = {k: v for k, v in list(res["units"].items())[:2]}
    (ROOT / "data" / "test_pdf_units_sample.json").write_text(
        json.dumps(sample, ensure_ascii=False, indent=1), encoding="utf-8")

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
