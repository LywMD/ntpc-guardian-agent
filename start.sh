#!/usr/bin/env bash
# 小小守護員　一鍵啟動（macOS / Linux）
#
# 用法
#   ./start.sh                  檢查環境 → 必要時建資料 → 啟動儀表板
#   PORT=8080 ./start.sh        換連接埠
#   ./start.sh --rebuild        強制重跑 ETL 與全市評分
#   ./start.sh --setup-only     只裝環境與建資料，不啟動伺服器
#
# 這支腳本會自動處理
#   1. 找 Python、建立 .venv、安裝 requirements.txt
#   2. 把 export/set 格式的 AWS 認證檔轉成 boto3 讀得懂的 INI 格式
#   3. 驗證 AWS 與 Bedrock 可用
#   4. 資料庫是空的就跑 ETL + 全市評分
#   5. 啟動 API + 前端

set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
CITY="${CITY:-新北市}"
REBUILD=0
SETUP_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --rebuild)    REBUILD=1 ;;
    --setup-only) SETUP_ONLY=1 ;;
    --port=*)     PORT="${arg#*=}" ;;
    *) echo "未知參數：$arg"; exit 1 ;;
  esac
done

export PYTHONIOENCODING=utf-8
LINE=$(printf '%.0s-' {1..66})
C_CYAN='\033[36m'; C_GREEN='\033[32m'; C_YELLOW='\033[33m'; C_RED='\033[31m'; C_OFF='\033[0m'

say()  { printf '%b\n' "$1"; }
step() { printf '\n%b\n' "${C_CYAN}[$1] $2${C_OFF}"; }
die()  { printf '\n%b\n' "${C_RED}[中止] $1${C_OFF}"; exit 1; }

say "$LINE"
say "${C_CYAN}  小小守護員　兒少機構風險稽查智能代理人${C_OFF}"
say "  $(pwd)"
say "$LINE"

# ---------------------------------------------------------------- 1. Python
step "1/5" "檢查 Python 與虛擬環境"
VENV_PY=".venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
  SYS_PY=""
  for cand in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then SYS_PY="$cand"; break; fi
  done
  [ -n "$SYS_PY" ] || die "找不到 Python。請先安裝 Python 3.10 以上（macOS：brew install python）。"
  say "  以 $SYS_PY 建立虛擬環境 .venv（第一次會慢一點）"
  "$SYS_PY" -m venv .venv
  [ -x "$VENV_PY" ] || die "虛擬環境建立失敗。"
fi
say "  OK  $($VENV_PY --version 2>&1)"

# ---------------------------------------------------------------- 2. 依賴
step "2/5" "檢查依賴套件"
if ! "$VENV_PY" -c "import boto3, fastapi, uvicorn, jieba, rapidfuzz, pdfplumber, bs4, scipy" >/dev/null 2>&1; then
  say "${C_YELLOW}  安裝 requirements.txt（第一次約 2-5 分鐘）${C_OFF}"
  "$VENV_PY" -m pip install --upgrade pip --quiet
  "$VENV_PY" -m pip install -r requirements.txt || die "套件安裝失敗，請檢查網路連線。"
fi
say "  OK  依賴齊備"

# ---------------------------------------------------------------- 3. AWS
step "3/5" "檢查 AWS 認證與 Bedrock"
CRED="$HOME/.aws/credentials"
if [ ! -f "$CRED" ]; then
  say "${C_YELLOW}  找不到 $CRED${C_OFF}"
  say "${C_YELLOW}  請到 AWS 主控台複製認證貼進該檔案後重跑；三種格式都可以，腳本會自動轉換。${C_OFF}"
  die "缺少 AWS 認證。"
fi
if grep -Eq '^[[:space:]]*(set|export)[[:space:]]' "$CRED"; then
  say "${C_YELLOW}  偵測到 export/set 格式的認證檔，轉換為 INI 格式（會先備份原檔）${C_OFF}"
  "$VENV_PY" scripts/fix_aws_credentials.py || die "認證檔轉換失敗。"
fi

if ! "$VENV_PY" -u cli.py check; then
  say ""
  say "${C_YELLOW}  AWS 或 Bedrock 檢查未通過。最常見的三個原因：${C_OFF}"
  say "${C_YELLOW}    1) 臨時憑證已過期（含 session token 的憑證通常只有數小時）${C_OFF}"
  say "${C_YELLOW}       → 重新複製一份認證貼到 $CRED，再跑一次${C_OFF}"
  say "${C_YELLOW}    2) 該區域沒有開啟模型存取權${C_OFF}"
  say "${C_YELLOW}       → Bedrock 主控台 → Model access → 申請 Anthropic Claude${C_OFF}"
  say "${C_YELLOW}    3) 區域不對（預設 us-west-2）→ export AWS_REGION=us-east-1${C_OFF}"
  say ""
  say "${C_YELLOW}  可用 .venv/bin/python scripts/probe_models.py 看哪些模型能呼叫${C_OFF}"
  die "AWS 未就緒。"
fi

# ---------------------------------------------------------------- 4. 資料
step "4/5" "檢查整合資料庫"
INST=$("$VENV_PY" -c "import sys;sys.path.insert(0,'.');from guardian import store;print(store.stats()['institutions'])" | tail -1)
if [ "$REBUILD" = "1" ] || [ "$INST" = "0" ]; then
  [ "$REBUILD" = "1" ] && say "  --rebuild：重跑資料整合與全市評分" || say "  資料庫為空，執行首次建置"
  "$VENV_PY" -u cli.py etl --city "$CITY" || die "ETL 失敗。"
  "$VENV_PY" -u cli.py scan --city "$CITY" --reset-history || die "全市評分失敗。"
else
  say "  OK  已有 $INST 間機構的資料（要重建請加 --rebuild）"
fi

# ---------------------------------------------------------------- 5. 啟動
if [ "$SETUP_ONLY" = "1" ]; then
  step "5/5" "已完成環境與資料建置（--setup-only，不啟動伺服器）"
  say ""
  say "  啟動儀表板：./start.sh"
  say "  命令列用法：.venv/bin/python cli.py --help"
  exit 0
fi

step "5/5" "啟動 API 與前端"
URL="http://127.0.0.1:$PORT"
say ""
say "${C_GREEN}  儀表板： $URL${C_OFF}"
say "${C_GREEN}  停止：   在本視窗按 Ctrl+C${C_OFF}"
say ""
say "${C_YELLOW}  提醒：本服務沒有身分驗證，只綁 127.0.0.1 供本機使用。${C_OFF}"
say "${C_YELLOW}        內含機構財務與民眾投訴內容，請勿直接對外開放。${C_OFF}"
say "$LINE"

( sleep 3
  if command -v open >/dev/null 2>&1; then open "$URL"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL"
  fi ) >/dev/null 2>&1 &

exec "$VENV_PY" -u cli.py serve --port "$PORT"
