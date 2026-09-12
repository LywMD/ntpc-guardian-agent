#!/bin/bash
# 在已經啟動好的 EC2（Amazon Linux 2023）上安裝依賴、抓正式資料庫、
# 用 systemd 常駐啟動服務。scripts/deploy_ec2.py 的 user-data 第一次跑
# 失敗時（例如依賴版本跟系統 Python 對不上），可以 git pull 到修好的版本後
# 直接執行本腳本重跑，不用整台機器重建。
#
# 用法（在 EC2 上，repo 根目錄）：
#   sudo bash infra/setup_remote.sh
set -euxo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
    python3.11 -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

mkdir -p data
python3.11 - <<'PYEOF'
import boto3
boto3.client("s3", region_name="us-west-2").download_file(
    "hackathonbyteach", "deploy/guardian.db", "data/guardian.db")
PYEOF

cat > /etc/systemd/system/guardian.service <<UNIT
[Unit]
Description=Guardian API + dashboard
After=network.target

[Service]
WorkingDirectory=$(pwd)
Environment=PYTHONIOENCODING=utf-8
Environment=GUARDIAN_DB=$(pwd)/data/guardian.db
Environment=GUARDIAN_ALLOWED_IPS=127.0.0.1,::1,49.216.93.140,60.250.71.45,61.222.117.53,59.125.121.41,60.250.71.43
ExecStart=$(pwd)/.venv/bin/python cli.py serve --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable guardian
systemctl restart guardian
sleep 3
systemctl status guardian --no-pager
