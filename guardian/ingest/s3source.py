"""從 S3 取得原始資料檔，含本機快取。

快取策略：以 ETag 判斷是否需要重新下載，避免每次都拉 670 MB。
下載中斷可續傳（先寫 .part 再改名）。
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from .. import aws, config

log = logging.getLogger("guardian.ingest.s3")

# 放原始資料的 S3 bucket。
# 刻意不寫死在原始碼裡：bucket 名稱是全域唯一且可被列舉的，公開出去等於邀人來探測。
# 請用環境變數指定，或在指令加 --bucket。
#   PowerShell : $env:GUARDIAN_DATA_BUCKET = "你的-bucket-名稱"
#   bash       : export GUARDIAN_DATA_BUCKET=你的-bucket-名稱
DEFAULT_BUCKET = os.getenv("GUARDIAN_DATA_BUCKET", "")


def require_bucket(bucket: str | None = None) -> str:
    b = bucket or DEFAULT_BUCKET
    if not b:
        raise RuntimeError(
            "未指定資料來源 bucket。請設定環境變數 GUARDIAN_DATA_BUCKET，"
            "或在指令加上 --bucket <名稱>。")
    return b

CACHE_DIR = config.RAW_DIR / "s3"
INDEX_PATH = CACHE_DIR / "_cache_index.json"


def _index() -> dict[str, Any]:
    if INDEX_PATH.exists():
        try:
            return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _save_index(idx: dict[str, Any]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(idx, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def _safe_name(key: str) -> str:
    """S3 key → 本機檔名，保留目錄結構但去掉不合法字元。"""
    parts = [re.sub(r'[<>:"|?*]', "_", p) for p in key.split("/")]
    return str(Path(*parts))


def list_objects(bucket: str = "", prefix: str = "") -> list[dict[str, Any]]:
    bucket = require_bucket(bucket)
    s3 = aws.s3()
    out: list[dict[str, Any]] = []
    token = None
    while True:
        kw: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        for o in resp.get("Contents", []):
            out.append({
                "key": o["Key"],
                "size": o.get("Size", 0),
                "etag": (o.get("ETag") or "").strip('"'),
                "modified": str(o.get("LastModified", ""))[:19],
            })
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    out.sort(key=lambda x: x["key"])
    return out


def fetch(bucket: str = "", key: str = "", force: bool = False) -> Path:
    """下載單一物件到本機快取，回傳路徑。ETag 相同就不重抓。"""
    bucket = require_bucket(bucket)
    if not key:
        raise ValueError("key 不可為空")
    local = CACHE_DIR / _safe_name(key)
    local.parent.mkdir(parents=True, exist_ok=True)
    idx = _index()
    s3 = aws.s3()

    head = s3.head_object(Bucket=bucket, Key=key)
    etag = (head.get("ETag") or "").strip('"')
    size = head.get("ContentLength", 0)

    cached = idx.get(f"{bucket}/{key}")
    if (not force and local.exists() and cached
            and cached.get("etag") == etag and local.stat().st_size == size):
        log.info("使用快取 %s", local.name)
        return local

    tmp = local.with_suffix(local.suffix + ".part")
    log.info("下載 s3://%s/%s（%.1f MB）", bucket, key, size / 1024 / 1024)
    s3.download_file(bucket, key, str(tmp))
    tmp.replace(local)
    idx[f"{bucket}/{key}"] = {"etag": etag, "size": size,
                              "local": str(local), "fetched_at": None}
    _save_index(idx)
    return local


def fetch_all(bucket: str = "", prefix: str = "",
              force: bool = False, on_progress=None) -> list[Path]:
    """批次下載某個前綴下的所有物件。"""
    bucket = require_bucket(bucket)
    objs = list_objects(bucket, prefix)
    paths: list[Path] = []
    total = len(objs)
    for i, o in enumerate(objs, start=1):
        if on_progress:
            on_progress(i, total, o)
        paths.append(fetch(bucket, o["key"], force=force))
    return paths


def cache_summary() -> dict[str, Any]:
    idx = _index()
    files = [Path(v["local"]) for v in idx.values() if Path(v["local"]).exists()]
    return {
        "cache_dir": str(CACHE_DIR),
        "cached_files": len(files),
        "cached_bytes": sum(f.stat().st_size for f in files),
        "keys": sorted(idx),
    }
