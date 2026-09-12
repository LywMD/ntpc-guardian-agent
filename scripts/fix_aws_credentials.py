"""把 CMD `set KEY=VALUE` 格式的 AWS 認證檔轉為 boto3 可讀的 INI 格式。

- 原檔會先備份為 credentials.bak-<時間戳>
- 不會輸出任何金鑰內容，只列出被寫入的欄位名稱
- region 統一寫入 ~/.aws/config
"""
from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

AWS_DIR = Path.home() / ".aws"
CRED = AWS_DIR / "credentials"
CONF = AWS_DIR / "config"

KEY_MAP = {
    "AWS_ACCESS_KEY_ID": "aws_access_key_id",
    "AWS_SECRET_ACCESS_KEY": "aws_secret_access_key",
    "AWS_SESSION_TOKEN": "aws_session_token",
}
LINE = re.compile(r"^\s*(?:set|export|\$env:)?\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def main() -> int:
    if not CRED.exists():
        print(f"[錯誤] 找不到 {CRED}")
        return 1
    raw = CRED.read_text(encoding="utf-8-sig", errors="replace")

    profile = "default"
    profiles: dict[str, dict[str, str]] = {}
    region: str | None = None
    converted = False

    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith(";"):
            continue
        if s.startswith("[") and s.endswith("]"):
            profile = s[1:-1].strip().removeprefix("profile ").strip()
            profiles.setdefault(profile, {})
            continue
        m = LINE.match(s)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip().strip('"').strip("'")
        upper = key.upper()
        if upper in ("AWS_DEFAULT_REGION", "AWS_REGION", "REGION"):
            region = value
            continue
        target = KEY_MAP.get(upper)
        if target:
            if upper != key or s.lower().startswith(("set ", "export ", "$env:")):
                converted = True
            profiles.setdefault(profile, {})[target] = value
        elif key.lower() in KEY_MAP.values():
            profiles.setdefault(profile, {})[key.lower()] = value

    profiles = {p: kv for p, kv in profiles.items() if kv}
    if not profiles:
        print("[錯誤] 檔案中找不到任何 AWS 金鑰欄位，未做任何變更")
        return 2

    backup = CRED.with_suffix(f".bak-{datetime.now():%Y%m%d%H%M%S}")
    shutil.copy2(CRED, backup)

    out_lines: list[str] = []
    for p, kv in profiles.items():
        out_lines.append(f"[{p}]")
        for k in ("aws_access_key_id", "aws_secret_access_key", "aws_session_token"):
            if k in kv:
                out_lines.append(f"{k} = {kv[k]}")
        out_lines.append("")
    CRED.write_text("\n".join(out_lines), encoding="utf-8")

    if region:
        conf_text = CONF.read_text(encoding="utf-8-sig") if CONF.exists() else ""
        if "[default]" not in conf_text:
            conf_text = f"[default]\nregion = {region}\noutput = json\n" + conf_text
            CONF.write_text(conf_text, encoding="utf-8")
        elif "region" not in conf_text:
            conf_text = conf_text.replace("[default]", f"[default]\nregion = {region}", 1)
            CONF.write_text(conf_text, encoding="utf-8")

    print(f"備份原檔     : {backup.name}")
    print(f"格式已轉換   : {'是（原為 set/export 語法）' if converted else '否（原本已是 INI）'}")
    for p, kv in profiles.items():
        print(f"  [{p}] 寫入欄位：{', '.join(sorted(kv))}")
    print(f"region       : {region or '（沿用 ~/.aws/config 既有設定）'}")
    if any("aws_session_token" in kv for kv in profiles.values()):
        print("\n注意：這是臨時憑證（含 session token），過期後需重新取得並再跑一次本腳本。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
