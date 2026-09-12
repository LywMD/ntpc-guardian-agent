"""診斷：列出 S3 內所有物件，確認公校決算書份數與已抽取狀況。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from guardian.ingest import s3source  # noqa: E402

OUT = ROOT / "data" / "diag_s3.txt"
EXTRACTED = ROOT / "data" / "extracted"
RAW = ROOT / "data" / "raw" / "s3"

buf: list[str] = []


def w(line: str = "") -> None:
    buf.append(line)


def main() -> None:
    bucket = os.environ.get("GUARDIAN_DATA_BUCKET", "hackathonbyteach")
    w(f"bucket = {bucket}")
    objs = s3source.list_objects(bucket)
    pdfs = [o for o in objs if o["key"].lower().endswith(".pdf")]
    w(f"物件總數 = {len(objs)}，PDF = {len(pdfs)}")
    w()

    by_prefix: dict[str, list[dict]] = {}
    for o in objs:
        prefix = o["key"].split("/")[0] if "/" in o["key"] else "(根目錄)"
        by_prefix.setdefault(prefix, []).append(o)

    for prefix, items in sorted(by_prefix.items()):
        w(f"--- {prefix}  ({len(items)} 個) ---")
        for o in sorted(items, key=lambda x: x["key"]):
            key = o["key"]
            size_mb = (o.get("size") or 0) / 1024 / 1024
            stem = Path(s3source._safe_name(key)).stem
            has_json = (EXTRACTED / f"{stem}.json").exists()
            has_raw = (RAW / s3source._safe_name(key)).exists()
            flag_json = "已抽取" if has_json else "未抽取"
            flag_raw = "已下載" if has_raw else "未下載"
            w(f"  {key}")
            w(f"      {size_mb:7.2f} MB   {flag_raw}   {flag_json}")
        w()


if __name__ == "__main__":
    main()
    OUT.write_text("\n".join(buf), encoding="utf-8")
