"""探測新北市／全國教保資訊網開放資料端點，找出真正可用的來源。

目的：把 etl.OFFICIAL_SOURCES 裡「只列 URL 但抓不到」的來源換成實際可用的端點。
輸出 data/probe_opendata.txt，逐一記錄狀態碼、Content-Type、資料筆數與欄位名。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "data" / "probe_opendata.txt"
buf: list[str] = []

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
}

# 候選端點。新北市資料開放平臺的 rid 是資料集識別碼，
# 這裡先用「資料集搜尋 API」找出幼兒園相關資料集，再抓實際資料。
CANDIDATES = [
    # 新北市資料開放平臺 — 資料集搜尋
    ("NTPC 資料集搜尋(幼兒園)",
     "https://data.ntpc.gov.tw/api/v2/datasets?q=%E5%B9%BC%E5%85%92%E5%9C%92&limit=50"),
    ("NTPC 資料集搜尋(v1)",
     "https://data.ntpc.gov.tw/api/datasets?q=%E5%B9%BC%E5%85%92%E5%9C%92"),
    ("NTPC 資料集搜尋(托嬰)",
     "https://data.ntpc.gov.tw/api/v2/datasets?q=%E6%89%98%E5%AC%B0&limit=50"),
    ("NTPC 資料集搜尋(教育局)",
     "https://data.ntpc.gov.tw/api/v2/datasets?q=%E6%95%99%E8%82%B2%E5%B1%80&limit=50"),
    # 政府資料開放平臺（全國）— 幼兒園相關
    ("政府資料開放平臺 搜尋(幼兒園)",
     "https://data.gov.tw/api/v2/rest/dataset?q=%E5%B9%BC%E5%85%92%E5%9C%92&limit=30"),
    # 全國教保資訊網 原本設定的 URL
    ("教保網 幼兒園基本資料(原設定)",
     "https://ap.ece.moe.edu.tw/webecems/OpenData/KIDBaseInfo.aspx"),
    ("教保網 裁罰(原設定)",
     "https://ap.ece.moe.edu.tw/webecems/OpenData/KIDPunish.aspx"),
    ("教保網 收費(原設定)",
     "https://ap.ece.moe.edu.tw/webecems/OpenData/KIDFee.aspx"),
]


def w(s: str = "") -> None:
    buf.append(s)


def describe(payload: object, limit: int = 3) -> None:
    """摘要 JSON 結構與欄位名。"""
    if isinstance(payload, list):
        w(f"      型別 = list，{len(payload)} 筆")
        for row in payload[:limit]:
            if isinstance(row, dict):
                w(f"      欄位 = {sorted(row.keys())}")
                w(f"      範例 = {json.dumps(row, ensure_ascii=False)[:400]}")
                break
    elif isinstance(payload, dict):
        w(f"      型別 = dict，keys = {sorted(payload.keys())[:20]}")
        for key in ("data", "result", "records", "datasets", "items"):
            if key in payload:
                inner = payload[key]
                if isinstance(inner, dict):
                    w(f"      {key} 是 dict，keys = {sorted(inner.keys())[:20]}")
                    for k2 in ("records", "results", "data", "datasets"):
                        if k2 in inner and isinstance(inner[k2], list):
                            w(f"      {key}.{k2} = {len(inner[k2])} 筆")
                            if inner[k2] and isinstance(inner[k2][0], dict):
                                w(f"      欄位 = {sorted(inner[k2][0].keys())}")
                                w(f"      範例 = "
                                  f"{json.dumps(inner[k2][0], ensure_ascii=False)[:400]}")
                            break
                elif isinstance(inner, list):
                    w(f"      {key} = {len(inner)} 筆")
                    if inner and isinstance(inner[0], dict):
                        w(f"      欄位 = {sorted(inner[0].keys())}")
                        w(f"      範例 = {json.dumps(inner[0], ensure_ascii=False)[:400]}")
                break


def main() -> None:
    try:
        import requests
    except ImportError:
        w("requests 未安裝")
        return

    for label, url in CANDIDATES:
        w("=" * 72)
        w(f"{label}")
        w(f"  {url}")
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            ctype = r.headers.get("Content-Type", "")
            w(f"    狀態 = {r.status_code}　Content-Type = {ctype}　長度 = {len(r.content)}")
            body = r.text.strip()
            if r.status_code != 200:
                w(f"    前 200 字 = {body[:200]!r}")
                continue
            if "json" in ctype.lower() or body[:1] in ("[", "{"):
                try:
                    describe(r.json())
                except Exception as exc:  # noqa: BLE001
                    w(f"    JSON 解析失敗：{exc}")
                    w(f"    前 300 字 = {body[:300]!r}")
            else:
                w(f"    非 JSON。前 300 字 = {body[:300]!r}")
        except Exception as exc:  # noqa: BLE001
            w(f"    失敗：{type(exc).__name__}: {str(exc)[:200]}")
        w()


if __name__ == "__main__":
    main()
    OUT.write_text("\n".join(buf), encoding="utf-8")
