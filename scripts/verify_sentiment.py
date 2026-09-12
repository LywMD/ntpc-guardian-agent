"""驗證輿情分析的關鍵字偵測：樣式比對要補抓插入語氣詞的寫法，且不得誤判。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from guardian.tools.sentiment import analyze_text  # noqa: E402

OUT = ROOT / "data" / "verify_sentiment.txt"
buf: list[str] = []
problems: list[str] = []


def w(s: str = "") -> None:
    buf.append(s)


def case(text: str, want_label: str, want_categories: set[str] | None = None,
         forbid_categories: set[str] | None = None) -> None:
    a = analyze_text(text)
    got_cats = set(a["categories"])
    ok = a["label"].startswith(want_label)
    detail = []
    if want_categories and not want_categories.issubset(got_cats):
        ok = False
        detail.append(f"缺分類 {want_categories - got_cats}")
    if forbid_categories and (forbid_categories & got_cats):
        ok = False
        detail.append(f"誤判分類 {forbid_categories & got_cats}")
    w(f"  [{'OK  ' if ok else 'FAIL'}] {text[:44]}")
    w(f"        情感={a['sentiment']:>7} 標籤={a['label']:<10} 分類={sorted(got_cats)}")
    if a["neg_keywords"]:
        w(f"        命中={[(h['keyword'], h.get('matched_by', 'lexicon')) for h in a['neg_keywords']]}")
    if detail:
        w(f"        !! {'；'.join(detail)}")
    if not ok:
        problems.append(text[:30])


w("=" * 72)
w("應偵測為負面（含插入語氣詞的寫法）")
w("=" * 72)
case("老師會用尺打手心，園方說是在教規矩，廚房衛生也很差", "負面",
     {"身體不當對待", "餐飲衛生"})
case("衛生真的很差，餐點也不新鮮", "負面", {"餐飲衛生"})
case("行政態度非常差，投訴都沒用", "負面", {"人員與行政"})
case("娃娃車沒有隨車人員，安全實在堪憂", "負面", {"公共安全"})
case("師資一直換，小孩剛適應又要重來", "負面", {"人員與行政"})
case("費用完全不透明，問了也沒說明", "負面", {"收費爭議"})
case("看到老師打小孩，這已經是體罰了", "負面", {"身體不當對待"})

w()
w("=" * 72)
w("不得誤判（否定語境與正面評價）")
w("=" * 72)
case("完全沒有體罰，老師很有耐心", "正面", None, {"身體不當對待"})
case("環境乾淨，餐點也用心，孩子很喜歡上學", "正面", None, {"餐飲衛生"})
case("老師很用心，衛生也做得不錯，很放心", "正面", None, {"餐飲衛生"})
case("園長會主動溝通，態度很好", "正面", None, {"人員與行政"})
case("老師處理得宜，回應很快，師資穩定", "正面", None, {"人員與行政"})

w()
w("=" * 72)
w(f"結論：{'全部正確' if not problems else f'{len(problems)} 項不符預期'}")
for p in problems:
    w(f"  - {p}")
w("=" * 72)

OUT.write_text("\n".join(buf), encoding="utf-8")
raise SystemExit(1 if problems else 0)
