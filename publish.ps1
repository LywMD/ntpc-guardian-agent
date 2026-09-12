<#
發佈到 GitHub。

前置：先做一次 GitHub 登入（互動式，需要瀏覽器授權）
  & "C:\Program Files\GitHub CLI\gh.exe" auth login

  選項依序選：
    GitHub.com  →  HTTPS  →  Y（用 gh 認證 git）  →  Login with a web browser
  它會給你一組 8 碼，按 Enter 開瀏覽器貼上即可。

然後執行本腳本
  .\publish.ps1                    建私有 repo（預設）
  .\publish.ps1 -Public            建公開 repo
  .\publish.ps1 -Name my-repo      指定 repo 名稱
  .\publish.ps1 -DryRun            只檢查，不建立不推送

本腳本會做的事
  1. 讀取你的 GitHub 身分，把 commit 作者改成你（目前是預留值）
  2. 再次確認沒有敏感檔案被納入版控
  3. 建立 repo 並推送
  4. 印出可直接打開的網址
#>
[CmdletBinding()]
param(
    [string]$Name = "ntpc-guardian-agent",
    [switch]$Public,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location -Path $PSScriptRoot

$GH = "C:\Program Files\GitHub CLI\gh.exe"
$Line = "-" * 66

function Say([string]$m, [string]$c = "White") { Write-Host $m -ForegroundColor $c }
function Step([string]$n, [string]$m) { Write-Host ""; Write-Host "[$n] $m" -ForegroundColor Cyan }
function Die([string]$m) { Write-Host ""; Write-Host "[中止] $m" -ForegroundColor Red; exit 1 }

Say $Line
Say "  發佈「小小守護員」到 GitHub" Cyan
Say $Line

if (-not (Test-Path $GH)) {
    Die "找不到 gh CLI。請先安裝：winget install --id GitHub.cli"
}

# ---------------------------------------------------------------- 1. 認證
Step "1/5" "檢查 GitHub 登入狀態"
# gh 未登入時會寫 stderr，在 ErrorActionPreference=Stop 下會直接中斷，
# 所以這裡暫時放寬，改用結束碼判斷。
$prevEA = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $GH auth status *> $null
$authExit = $LASTEXITCODE
$ErrorActionPreference = $prevEA
if ($authExit -ne 0) {
    Say ""
    Say "  尚未登入 GitHub。請先執行這一行（會開瀏覽器要你授權）：" Yellow
    Say ""
    Say "    & `"$GH`" auth login" Green
    Say ""
    Say "  選項依序選：GitHub.com → HTTPS → Y → Login with a web browser" Yellow
    Say "  完成後再跑一次 .\publish.ps1" Yellow
    exit 1
}
$login = (& $GH api user --jq .login).Trim()
$fullName = (& $GH api user --jq '.name // .login').Trim()
$email = (& $GH api user --jq '.email // ""').Trim()
if (-not $email) { $email = "$login@users.noreply.github.com" }
Say "  OK  已登入為 $login（$fullName / $email）"

# ---------------------------------------------------------------- 2. 安全檢查
Step "2/5" "確認沒有敏感檔案被納入版控"
$tracked = git ls-files
$bad = $tracked | Where-Object {
    $_ -match '(^|/)data/' -or $_ -match '\.db($|-)' -or $_ -match '\.pdf$' -or
    $_ -match '\.log$' -or $_ -match 'aws_inventory' -or $_ -match '(^|/)reports/' -or
    $_ -match '^\.venv/' -or $_ -match '(^|/)credentials'
}
if ($bad) {
    Say ""
    Say "  以下檔案不該進版控：" Red
    $bad | ForEach-Object { Say "    $_" Red }
    Die "請先從索引移除（git rm --cached <檔案>）後再發佈。"
}
Say "  OK  追蹤中 $($tracked.Count) 個檔案，無敏感內容"

# 順手掃一下內容有沒有金鑰樣式（git grep 找不到東西會回非 0，屬正常）
$prevEA = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$leaks = git grep -nIE 'AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY' -- . 2>$null
$ErrorActionPreference = $prevEA
if ($leaks) {
    Say ""
    Say "  偵測到可能的金鑰字串：" Red
    $leaks | ForEach-Object { Say "    $_" Red }
    Die "請先移除後再發佈。"
}
Say "  OK  內容未偵測到金鑰樣式"

# ---------------------------------------------------------------- 3. 修正作者
Step "3/5" "把 commit 作者改成你的 GitHub 身分"
$currentAuthor = git log -1 --format='%an <%ae>'
Say "  目前：$currentAuthor"
Say "  改為：$fullName <$email>"
if (-not $DryRun) {
    git config --local user.name $fullName
    git config --local user.email $email
    git -c "user.name=$fullName" -c "user.email=$email" commit --amend --no-edit --reset-author | Out-Null
    Say "  OK  $(git log -1 --format='%an <%ae>')"
}

# ---------------------------------------------------------------- 4. 建 repo
$visibility = if ($Public) { "public" } else { "private" }
Step "4/5" "建立 GitHub repo（$visibility）"

$prevEA = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $GH repo view "$login/$Name" --json name *> $null
$repoExists = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $prevEA
if ($repoExists) {
    Say "  repo 已存在：$login/$Name" Yellow
    $remote = git remote get-url origin 2>$null
    if (-not $remote) {
        if (-not $DryRun) { git remote add origin "https://github.com/$login/$Name.git" }
        Say "  已加上 origin"
    }
} else {
    if ($DryRun) {
        Say "  [dry-run] 會建立 $login/$Name（$visibility）"
    } else {
        & $GH repo create $Name "--$visibility" --source=. --remote=origin `
            --description "小小守護員：以 Amazon Bedrock 打造的兒少機構風險稽查智能代理人（新北市）"
        if ($LASTEXITCODE -ne 0) { Die "建立 repo 失敗。" }
        Say "  OK  已建立 $login/$Name"
    }
}

# ---------------------------------------------------------------- 5. 推送
Step "5/5" "推送到 GitHub"
if ($DryRun) {
    Say "  [dry-run] 會執行 git push -u origin main"
} else {
    git push -u origin main
    if ($LASTEXITCODE -ne 0) { Die "推送失敗。若是認證問題，請重跑 gh auth login。" }
}

$url = "https://github.com/$login/$Name"
Say ""
Say $Line
Say "  完成" Green
Say ""
Say "  repo 網址： $url" Green
Say "  README ：  $url#readme" Green
Say ""
if (-not $Public) {
    Say "  這是私有 repo，只有你（登入狀態）打得開。" Yellow
    Say "  要改成公開：& `"$GH`" repo edit $login/$Name --visibility public --accept-visibility-change-consequences" Yellow
}
Say $Line
