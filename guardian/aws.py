"""AWS 連線層：Bedrock / S3 用戶端與模型可用性偵測。"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from . import config

log = logging.getLogger("guardian.aws")

_BOTO_CFG = Config(
    region_name=config.AWS_REGION,
    retries={"max_attempts": 5, "mode": "adaptive"},
    read_timeout=300,
    connect_timeout=15,
)


@lru_cache(maxsize=1)
def session() -> boto3.Session:
    if config.AWS_PROFILE:
        return boto3.Session(profile_name=config.AWS_PROFILE, region_name=config.AWS_REGION)
    return boto3.Session(region_name=config.AWS_REGION)


@lru_cache(maxsize=1)
def bedrock_runtime():
    return session().client("bedrock-runtime", config=_BOTO_CFG)


@lru_cache(maxsize=1)
def bedrock_control():
    return session().client("bedrock", config=_BOTO_CFG)


@lru_cache(maxsize=1)
def s3():
    return session().client("s3", config=_BOTO_CFG)


def whoami() -> dict[str, Any]:
    sts = session().client("sts", config=_BOTO_CFG)
    ident = sts.get_caller_identity()
    return {
        "account": ident["Account"],
        "arn": ident["Arn"],
        "user_id": ident["UserId"],
        "region": config.AWS_REGION,
    }


@lru_cache(maxsize=1)
def resolve_model_id() -> str:
    """挑一個帳號真的能呼叫的模型 ID。

    先試設定值，失敗就依 MODEL_FALLBACKS 逐一以極短提問實測，
    避免掃描時才發現沒有模型存取權。
    """
    candidates = [config.BEDROCK_MODEL_ID] + [
        m for m in config.MODEL_FALLBACKS if m != config.BEDROCK_MODEL_ID
    ]
    errors: list[str] = []
    for model_id in candidates:
        try:
            bedrock_runtime().converse(
                modelId=model_id,
                messages=[{"role": "user", "content": [{"text": "ok"}]}],
                inferenceConfig={"maxTokens": 8, "temperature": 0.0},
            )
            log.info("使用 Bedrock 模型：%s", model_id)
            return model_id
        except ClientError as exc:
            errors.append(f"{model_id}: {exc.response['Error'].get('Code')}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{model_id}: {type(exc).__name__}")
    raise RuntimeError(
        "在區域 "
        + config.AWS_REGION
        + " 找不到可呼叫的 Bedrock 模型，請到 Bedrock 主控台開啟模型存取權。細節：\n  "
        + "\n  ".join(errors)
    )


def list_text_models() -> list[str]:
    """列出帳號在本區域可見的文字生成模型（含跨區推論設定檔）。"""
    out: list[str] = []
    try:
        resp = bedrock_control().list_foundation_models(byOutputModality="TEXT")
        out += [m["modelId"] for m in resp.get("modelSummaries", [])]
    except Exception as exc:  # noqa: BLE001
        log.warning("list_foundation_models 失敗：%s", exc)
    try:
        resp = bedrock_control().list_inference_profiles(maxResults=100)
        out += [p["inferenceProfileId"] for p in resp.get("inferenceProfileSummaries", [])]
    except Exception:  # noqa: BLE001
        pass
    return sorted(set(out))


def embed(text: str) -> list[float] | None:
    """Titan Embeddings：供輿情語意比對使用（取不到時回傳 None）。"""
    try:
        resp = bedrock_runtime().invoke_model(
            modelId=config.EMBED_MODEL_ID,
            body=json.dumps({"inputText": text[:8000]}),
        )
        return json.loads(resp["body"].read())["embedding"]
    except Exception as exc:  # noqa: BLE001
        log.debug("embed 失敗：%s", exc)
        return None


def s3_put(key: str, body: bytes, content_type: str = "application/octet-stream") -> str | None:
    """把檔案放上 S3；未設定 bucket 時回傳 None（不阻斷流程）。"""
    if not config.S3_BUCKET:
        return None
    full_key = config.S3_PREFIX + key.lstrip("/")
    try:
        s3().put_object(
            Bucket=config.S3_BUCKET, Key=full_key, Body=body, ContentType=content_type
        )
        return f"s3://{config.S3_BUCKET}/{full_key}"
    except Exception as exc:  # noqa: BLE001
        log.warning("S3 上傳失敗 %s：%s", full_key, exc)
        return None
