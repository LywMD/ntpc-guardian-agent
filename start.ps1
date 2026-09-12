<#
小小守護員　一鍵啟動（Windows / PowerShell）

用法
  .\start.ps1                 檢查環境 → 必要時建資料 → 啟動儀表板
  .\start.ps1 -Port 8080      換連接埠
  .\start.ps1 -Rebuild        強制重跑 ETL 與全市評分
  .\start.ps1 -SetupOnly      只裝環境與建資料，不啟動伺服器
  .\start.ps1 -SkipOpen       不自動開瀏覽器

這支腳本會自動處理
  1. 找 Python、建立 .venv、安裝 requirements.txt
  2. 把 CMD 格式（set KEY=VALUE）的 AWS 認證檔轉成 boto3 讀得懂的 INI 格式
  3. 驗證 AWS 與 Bedrock 可用
  4. 資料庫是空的就跑 ETL + 全市評分
  5. 啟動 API + 前端
#>
[CmdletBinding()]
param(
    [int]$Port = 8000,
    [switch]$Rebuild,
    [switch]$SetupOnly,
    [switch]$SkipOpen,
    [string]$City = "新北市"
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

Set-Location -Path $PSScriptRoot
$Line = "-" * 66

function Say([string]$msg, [string]$color = "White") {
    Write-Host $msg -ForegroundColor $color
}
function Step([string]$n, [string]$msg) {
    Write-Host ""
    Write-Host "[$n] $msg" -ForegroundColor Cyan
}
function Die([string]$msg) {
    Write-Host ""
    Write-Host "[中止] $msg" -ForegroundColor Red
    exit 1
}

Say $Line
Say "  小小守護員　兒少機構風險稽查智能代理人" Cyan
Say "  $(Get-Location)"
Say $Line

# ---------------------------------------------------------------- 1. Python
Step "1/5" "檢查 Python 與虛擬環境"
$VenvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $VenvPy)) {
    $sysPy = $null
    foreach ($cand in @("py", "python", "python3")) {
        $cmd = Get-Command $cand -ErrorAction SilentlyContinue
        if ($cmd) {
            # py 啟動器要能真的跑起來才算
            try {
                & $cand --version *> $null
                if ($LASTEXITCODE -eq 0) { $sysPy = $cand; break }
            } catch { }
        }
    }
    if (-not $sysPy) {
        Die "找不到 Python。請先安裝 Python 3.10 以上：https://www.python.org/downloads/`n     安裝時請勾選「Add python.exe to PATH」。"
    }
    Say "  以 $sysPy 建立虛擬環境 .venv（第一次會慢一點）"
    & $sysPy -m venv .venv
    if (-not (Test-Path $VenvPy)) { Die "虛擬環境建立失敗。" }
}
$pyVer = (& $VenvPy --version) 2>&1
Say "  OK  $pyVer"

# ---------------------------------------------------------------- 2. 依賴
Step "2/5" "檢查依賴套件"
& $VenvPy -c "import boto3, fastapi, uvicorn, jieba, rapidfuzz, pdfplumber, bs4, scipy" *> $null
if ($LASTEXITCODE -ne 0) {
    Say "  安裝 requirements.txt（第一次約 2-5 分鐘，jieba 與 scipy 較大）" Yellow
    & $VenvPy -m pip install --upgrade pip --quiet
    & $VenvPy -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { Die "套件安裝失敗，請檢查網路連線後重試。" }
    & $VenvPy -c "import boto3, fastapi, uvicorn, jieba, rapidfuzz, pdfplumber, bs4, scipy" *> $null
    if ($LASTEXITCODE -ne 0) { Die "套件安裝後仍無法匯入，請手動執行：.\.venv\Scripts\python.exe -m pip install -r requirements.txt" }
}
Say "  OK  依賴齊備"

# ---------------------------------------------------------------- 3. AWS 認證
Step "3/5" "檢查 AWS 認證與 Bedrock"
$CredPath = Join-Path $env:USERPROFILE ".aws\credentials"
if (-not (Test-Path $CredPath)) {
    Say ""
    Say "  找不到 $CredPath" Yellow
    Say "  請到 AWS 主控台（或工作坊頁面）複製認證，貼進該檔案後重跑本腳本。" Yellow
    Say "  三種格式都可以，腳本會自動轉換。" Yellow
    Die "缺少 AWS 認證。"
}

# CMD 的 set KEY=VALUE 格式 boto3 讀不到，自動轉成 INI
$credRaw = Get-Content $CredPath -Raw -Encoding UTF8
if ($credRaw -match '(?im)^\s*(set|export|\$env:)\s') {
    Say "  偵測到 CMD/export 格式的認證檔，轉換為 INI 格式（會先備份原檔）" Yellow
    & $VenvPy scripts\fix_aws_credentials.py
    if ($LASTEXITCODE -ne 0) { Die "認證檔轉換失敗。" }
}

& $VenvPy -u cli.py check
if ($LASTEXITCODE -ne 0) {
    Say ""
    Say "  AWS 或 Bedrock 檢查未通過。最常見的三個原因：" Yellow
    Say "    1) 臨時憑證已過期（含 session token 的憑證通常只有數小時）" Yellow
    Say "       → 重新複製一份認證貼到 $CredPath，再跑一次本腳本" Yellow
    Say "    2) 該區域沒有開啟模型存取權" Yellow
    Say "       → Bedrock 主控台 → Model access → 申請 Anthropic Claude" Yellow
    Say "    3) 區域不對（預設 us-west-2）" Yellow
    Say "       → 設定環境變數：`$env:AWS_REGION='us-east-1'" Yellow
    Say ""
    Say "  可用 .\.venv\Scripts\python.exe scripts\probe_models.py 看哪些模型能呼叫" Yellow
    Die "AWS 未就緒。"
}

# ---------------------------------------------------------------- 4. 資料
Step "4/5" "檢查整合資料庫"
$instCount = & $VenvPy -c "import sys;sys.path.insert(0,'.');from guardian import store;print(store.stats()['institutions'])"
$instCount = [int]($instCount | Select-Object -Last 1)

if ($Rebuild -or $instCount -eq 0) {
    if ($Rebuild) { Say "  -Rebuild：重跑資料整合與全市評分" }
    else { Say "  資料庫為空，執行首次建置" }
    & $VenvPy -u cli.py etl --city $City
    if ($LASTEXITCODE -ne 0) { Die "ETL 失敗。" }
    & $VenvPy -u cli.py scan --city $City --reset-history
    if ($LASTEXITCODE -ne 0) { Die "全市評分失敗。" }
} else {
    Say "  OK  已有 $instCount 間機構的資料（要重建請加 -Rebuild）"
}

# ---------------------------------------------------------------- 5. 啟動
if ($SetupOnly) {
    Step "5/5" "已完成環境與資料建置（-SetupOnly，不啟動伺服器）"
    Say ""
    Say "  啟動儀表板：.\start.ps1"
    Say "  命令列用法：.\.venv\Scripts\python.exe cli.py --help"
    exit 0
}

Step "5/5" "啟動 API 與前端"
$Url = "http://127.0.0.1:$Port"
Say ""
Say "  儀表板： $Url" Green
Say "  停止：   在本視窗按 Ctrl+C" Green
Say ""
Say "  提醒：本服務沒有身分驗證，只綁 127.0.0.1 供本機使用。" Yellow
Say "        內含機構財務與民眾投訴內容，請勿直接對外開放。" Yellow
Say $Line

if (-not $SkipOpen) {
    Start-Job -ScriptBlock {
        param($u)
        Start-Sleep -Seconds 3
        Start-Process $u
    } -ArgumentList $Url | Out-Null
}

& $VenvPy -u cli.py serve --port $Port
