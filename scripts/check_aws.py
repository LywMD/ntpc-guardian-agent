"""AWS 連線與 Bedrock 模型可用性檢查。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from guardian import aws, config  # noqa: E402


def main() -> int:
    print("=" * 68)
    print("AWS 連線檢查")
    print("=" * 68)
    try:
        me = aws.whoami()
        print(f"  帳號      : {me['account']}")
        print(f"  身分 ARN  : {me['arn']}")
        print(f"  區域      : {me['region']}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [失敗] 無法取得身分：{type(exc).__name__}: {exc}")
        return 1

    print("\n可見的文字模型（前 40 筆）")
    models = aws.list_text_models()
    if not models:
        print("  （list_foundation_models 無回應或權限不足）")
    for m in models[:40]:
        print("  -", m)
    print(f"  共 {len(models)} 筆")

    print("\nBedrock Converse 實測")
    try:
        model_id = aws.resolve_model_id()
        print(f"  [OK] 可呼叫模型：{model_id}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [失敗] {exc}")
        return 2

    print("\nTitan Embeddings 實測")
    vec = aws.embed("測試")
    print(f"  {'[OK] 維度 ' + str(len(vec)) if vec else '[略過] 無法取得（不影響主流程）'}")

    print(f"\nS3 bucket 設定：{config.S3_BUCKET or '未設定（原始檔僅存本機）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
