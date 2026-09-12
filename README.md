# 小小守護員｜兒少機構風險稽查智能代理人

把稽查從「事後被動」提前為「事前主動示警」的 AI Agent。跑在你自己的 AWS 帳號上，
以 Amazon Bedrock 的 tool use 迴圈自主規劃調查步驟、動態呼叫四大工具、交叉驗證證據，
最後產出附引用的白話文風險報告，並支援稽查人員自然語言追問。

它不是「資料進、分數出」的固定流程。實測範例：Agent 發現財務異常子分數 90.5 後，
**自己決定**加做前兩位數字 Benford 檢定，並把輿情觀察窗從 180 天拉長到 365 天，
才做最終評分——這個加派動作沒有寫死在流程裡，是模型依證據判斷的。

---

## 目前狀態（實測結果）

環境：Windows / Python 3.14 / AWS `us-west-2`
模型：`us.anthropic.claude-sonnet-4-5-20250929-v1:0`（已驗證支援 tool use）

| 項目 | 狀態 |
|------|------|
| AWS 認證與 Bedrock Converse | ✅ 實測通過 |
| 五大工具（ETL／鑑識會計／輿情／評分／法規遵循） | ✅ 90+ 項煙霧測試全過 |
| 師生比依幼照法§16分年齡層判定 | ✅ 抓到 3 間「合併合格、2歲專班違規」案例 |
| 全體統計基準 + 1σ/2σ/3σ 門檻 | ✅ 實測通過 |
| 區域穩定度（變異係數／樣本量檢查） | ✅ 實測通過 |
| 地方自治法規從嚴覆寫（放寬會被擋） | ✅ 實測通過 |
| Agent 自主規劃與加派工具 | ✅ 實測 6–7 次工具呼叫、含自主加派 |
| 卡方檢定備援實作 vs scipy | ✅ 誤差 < 1e-9 |
| API + 前端儀表板 | ✅ 27 項 API 測試全過 |
| 對話式追問（多輪同 session） | ✅ 實測通過 |
| 部署為雲端 Bedrock Agents | ⚠️ 腳本已備妥，**尚未執行**（見下方說明） |
| 官方公開資料 live 爬取 | ⚠️ 端點目前不可用，自動退回示範資料集 |

### 法規判定基準的來源

判定邏輯依據兩份來源，都記錄在 `.kiro/steering/ntpc-regulatory-baseline.md`：

1. **新北市政府教育局／城鄉發展局訪談結論**（實地調查取得）
2. **已核對原文的法源**：幼兒教育及照顧法、幼兒教保及照顧服務實施準則、
   幼兒園行政組織及員額編制標準、兒童及少年福利機構設置標準

所有法定數字集中在 `guardian/regulations.py`，每一條都附條號與罰則，
`GET /api/regulations` 可列出程式實際採用的門檻供教育局核對。

---

## 發佈到 GitHub

```powershell
# 1. 登入（一次就好，會開瀏覽器授權）
& "C:\Program Files\GitHub CLI\gh.exe" auth login
#    選項：GitHub.com → HTTPS → Y → Login with a web browser

# 2. 發佈
.\publish.ps1              # 私有 repo（預設）
.\publish.ps1 -Public      # 公開 repo
.\publish.ps1 -DryRun      # 只檢查不動作
```

`publish.ps1` 在推送前會做兩道安全檢查：確認沒有 `data/`、`*.db`、`*.pdf`、
`reports/`、認證檔被納入版控，並掃描內容有沒有 AKIA／ASIA／PRIVATE KEY 樣式。
任何一項命中就中止，不會推上去。

### 什麼不會進版控

真實資料一律排除。`.gitignore` 明確擋掉：

| 排除項目 | 原因 |
|---------|------|
| `data/raw/` | 新北市政府提供的真實 PDF（非營利園財報、公校決算書），約 140 MB |
| `data/extracted/` | OCR 抽出的**實際機構**財務明細 |
| `*.db` | 含真實財務數字的資料庫 |
| `data/aws_inventory.json` | AWS 帳號 ID、ARN、bucket 名稱 |
| `reports/` | Agent 產出的風險報告（含真實機構名稱與判定） |

S3 bucket 名稱也不寫在原始碼裡，改用環境變數 `GUARDIAN_DATA_BUCKET`——
bucket 名稱是全域唯一且可被列舉的，公開出去等於邀人來探測。

---

## 啟動

### 一鍵啟動（建議）

```powershell
# Windows
.\start.ps1
```

```bash
# macOS / Linux
chmod +x start.sh
./start.sh
```

腳本會依序做完：找 Python → 建 `.venv` → 裝依賴 → **自動修正 AWS 認證檔格式** →
驗證 Bedrock → 資料庫空的就跑 ETL 與全市評分 → 啟動儀表板並開瀏覽器。
環境已就緒時大約 10 秒；第一次跑（要裝套件、建資料）約 3–6 分鐘。

常用參數：

| 參數 | 說明 |
|------|------|
| `-Port 8080` / `--port=8080` | 換連接埠 |
| `-Rebuild` / `--rebuild` | 強制重跑 ETL 與全市評分 |
| `-SetupOnly` / `--setup-only` | 只裝環境與建資料，不啟動伺服器 |
| `-SkipOpen` | 不自動開瀏覽器 |

停止：在該視窗按 `Ctrl+C`。

> Windows 若出現「因為這個系統上已停用指令碼執行」，用這行繞過（只對這次執行有效）：
> `powershell -ExecutionPolicy Bypass -File .\start.ps1`

### 手動啟動

```powershell
.\.venv\Scripts\python.exe cli.py check        # 確認 AWS / Bedrock / 資料庫
.\.venv\Scripts\python.exe cli.py serve        # 只開儀表板
.\.venv\Scripts\python.exe cli.py investigate 麥克   # 讓 Agent 自主調查
```

### AWS 認證格式提醒

工作坊主控台通常提供三種格式的臨時憑證。如果你貼的是 **Windows CMD** 的
`set AWS_ACCESS_KEY_ID=...`，boto3 讀不到（它要 INI 格式）。`start.ps1` 會自動偵測並轉換，
也可以手動跑：

```powershell
.\.venv\Scripts\python.exe scripts\fix_aws_credentials.py
```

會先備份原檔再轉換，不會印出金鑰內容。

**臨時憑證會過期。** 含 `aws_session_token` 的憑證通常只有數小時效期，過期後
`cli.py check` 會失敗。解法就是重新複製一份認證貼到 `~/.aws/credentials`，再跑一次啟動腳本。

---

## 在另一台裝置上執行

可以。整個專案除了 AWS Bedrock 之外沒有其他外部依賴，資料庫是本機 SQLite。

### 步驟

1. **複製專案**，但**不要複製** `.venv/`（裡面是綁定該台機器路徑的絕對路徑，換機器會壞）
   ```
   要帶走：guardian/  scripts/  infra/  web/  .kiro/
           api.py  cli.py  start.ps1  start.sh  requirements.txt  README.md
   可不帶：.venv/  __pycache__/  data/guardian.db  reports/
   ```
   用 git 的話 `.gitignore` 已經把該排除的都排除了，直接 clone 就對。

2. **裝 Python 3.10 以上**（本機實測 3.14.2）。Windows 安裝時記得勾
   「Add python.exe to PATH」。

3. **放 AWS 認證**到 `~/.aws/credentials`（Windows 是 `C:\Users\<你>\.aws\credentials`）。
   三種格式都可以，啟動腳本會轉。

4. **跑啟動腳本**。`.venv` 會自己建、依賴會自己裝、資料庫空的會自己建。

### 換機器時最容易卡的三件事

| 症狀 | 原因 | 解法 |
|------|------|------|
| `Unable to locate credentials` | 認證檔是 CMD 的 `set KEY=VALUE` 格式，或憑證已過期 | 重貼一份認證，跑 `scripts/fix_aws_credentials.py`（啟動腳本會自動做） |
| `找不到可呼叫的 Bedrock 模型` | 該區域沒開模型存取權，或區域不對 | Bedrock 主控台 → Model access 申請 Claude；或設 `AWS_REGION` |
| 中文顯示成亂碼 | 終端機不是 UTF-8 | 啟動腳本已設 `PYTHONIOENCODING=utf-8`；Windows 可再跑 `chcp 65001` |

確認模型可用性的工具：

```powershell
.\.venv\Scripts\python.exe scripts\probe_models.py
```

它會逐一實測候補模型清單，印出哪些真的能用 tool use 呼叫。

### 資料要不要一起搬？

**不用。** 示範資料集是固定亂數種子產生的，任何機器跑 `cli.py etl` 都會得到
完全一樣的 40 間機構、9,250 筆決算明細與同樣的風險分數。實測驗證過：
刪掉資料庫重建後，排行榜與各項子分數與原本完全一致。

如果你已經接了真實資料、或想保留評分歷史（`scores` 表用來算分數驟升），
就把 `data/guardian.db` 一起複製過去。SQLite 檔案跨平台通用。

### 換到不同區域或模型

```powershell
$env:AWS_REGION = "us-east-1"
$env:GUARDIAN_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"   # 想省錢用 haiku
.\start.ps1
```

模型 ID 設錯或沒權限時，`aws.resolve_model_id()` 會自動往下試 `config.MODEL_FALLBACKS`
的候補清單，不會直接掛掉。

### 要給團隊其他人連進來？

`cli.py serve --host 0.0.0.0` 可以讓同網段的人連，但**先讀安全性那一節**：
這個服務沒有任何身分驗證，裡面有機構財務與民眾投訴內容。示範場合建議還是各自跑本機。

---

## 指令一覽

```
cli.py check                檢查 AWS / Bedrock / 資料庫狀態
cli.py etl [--offline]      工具 A：資料整合
cli.py scan [--reset-history]  規則式全市評分（不呼叫 LLM，約 30 秒）
cli.py board --top 20       風險排行榜（四個子分數）
cli.py alerts               未處理預警
cli.py inspect <名稱>        單一機構完整分析明細（含分年齡層師生比與法規遵循清單）
cli.py compliance           工具 E：法規遵循全市掃描
cli.py baseline             全體統計基準與 1σ/2σ/3σ 門檻
cli.py stability [--verbose] 各區資料穩定度與樣本量檢查
cli.py agent-scan           Agent 自主規劃全市掃描
cli.py investigate <名稱>    Agent 深入調查特定機構
cli.py why <名稱>            問 Agent「為什麼分數升高」
cli.py chat [--verbose]     互動式問答
cli.py perceive             感知層：列出目前觸發訊號
cli.py auto                 感知 → 自動開調查（全自動閉環）
cli.py train                嘗試訓練監督式模型
cli.py serve                啟動 API + 前端
```

驗證腳本：

```
scripts\check_aws.py             AWS 連線與模型清單
scripts\probe_models.py          實測哪些 Bedrock 模型可用 + 支援 tool use
scripts\smoke_test.py            四大工具與評分的端到端測試（40 項）
scripts\api_test.py              API 與 Agent 對話測試（需先 serve）
scripts\fix_aws_credentials.py   修正 CMD 格式的認證檔
```

---

## Agent 的四項能力

### 感知 Perceive
`guardian/agent.py: perceive()`

掃出值得觸發調查的訊號：未處理預警、近 30 天新增的負面輿情、最新分數較前次上升超過門檻的機構。
`auto_investigate()` 把它接成閉環——偵測到訊號就自己開調查，不必等人下指令。
正式環境由 EventBridge 定期呼叫。

### 規劃 Plan
系統提示定義了交叉驗證原則，模型依此自主決策：

- 財務異常子分數 ≥ 55 或 Benford 結論為 nonconformity → **必須**加做輿情分析且觀察窗拉到 365 天
- 輿情命中「身體不當對待」或「不當管教」→ **必須**回頭查師生比與人事費占比，確認是不是人力不足導致的結構性問題
- 交叉比對出現收入落差 → 必須確認收費公告與在園人數是否同年度，避免誤判

`scoring.scan_city()` 也有批次版的加派邏輯（不需 LLM）：財務子分數 ≥ 55 就自動放大輿情窗並加做前兩位 Benford。

### 行動 Act
五大工具，schema 定義在 `guardian/toolspec.py`，同一份契約本機與雲端共用。

系統提示裡另有六條**法規判定硬規則**，Agent 不得違反，包括：
師生比一律分年齡層判定、不可用全園合併值認定合規、指標合理性以全體統計加標準差分級、
拿區級平均當基準前要先確認該區資料是否穩定、引用法規要寫條號與罰則、
欄位缺漏只能說「無法檢核」不得說成合規。

### 生成與互動 Generate & Interact
報告固定四段結構：【風險判定】【判定依據】【調查過程】【建議處置】。
每個判斷都要附具體數字。工具查無資料時要明說「這個維度沒有資料」，不准編造。
資料來源若是示範資料集，報告開頭必須標示（由 `store.meta` 記錄的 `etl_source_mode` 驅動）。

---

## 四大工具

### 工具 A：資料整合（ETL）— `guardian/tools/etl.py`

- 抓取官方公開資料：基本資料、評鑑結果、裁罰紀錄、收費明細、決算報告
- `parse_settlement_pdf()` 用 pdfplumber 把非結構化決算 PDF 轉成結構化的逐筆收支
- **統一機構 ID**：`normalize_name()` 去掉「私立／市立／財團法人／臺台異體字」等雜訊，
  `normalize_address()` 去掉郵遞區號與空白，兩者組合 SHA1 產生 `INST-XXXXXXXXXXXX`；
  新資料進來時先用 rapidfuzz 模糊比對既有機構，命中就沿用同一個 ID，不同表述合併進 `aliases`
- 寫入單一整合資料庫，作為異常偵測與評分的共用基礎

### 工具 B：鑑識會計異常偵測 — `guardian/tools/forensic.py`

三項訊號加權（可在 `config.FORENSIC_WEIGHTS` 調整，預設 40/25/35）：

**1. 財務比率分析**（與同縣市同類型同業比較，n 會標在報告裡）
- 師生比：在園幼兒數 ÷ 教保服務人員數，對照法定基準（幼兒園 1:15、托嬰中心 1:5）與同業平均 z 分數
- 人事費用占比：人事支出 ÷ 總支出，偏低可能反映師資配置不足或帳務灌水
- 收支結構：各項收入占比偏離同業 2 個標準差以上就標記
- 年增率異常：單年變動超過同業標準差 2 倍列為待複核
- 平均月薪推估：人事費 ÷ 教保人員數 ÷ 12，低於基本工資水準視為申報不實疑慮

**2. Benford's Law 數字分佈檢測**
- 首位（1 開頭約 30.1%、9 開頭約 4.6%）與前兩位數字頻率統計
- 卡方檢定 + 平均絕對偏差 MAD；判讀採 Nigrini 基準（首位 ≤0.006 高度符合、>0.015 不符合）
- scipy 不在時有純 Python 的正規化不完全 Gamma 實作備援，已驗證與 scipy 誤差 < 1e-9
- 樣本 < 25 筆直接回 `insufficient_data`，不硬做判讀
- **定位為低成本初篩**，分數高只代表應優先複核原始憑證，報告會強制註明這點

**3. 收費明細與決算交叉比對**
- 預期收入 =（公告學費＋雜費＋代收代辦費）× 2 學期 × 在園人數
- 與決算申報的學雜費收入比對，落差 ≥15% 標「待確認」、≥25% 標「異常」
- 同步檢查人事費申報與教保人員數是否相稱

### 工具 C：NLP 社群輿情分析 — `guardian/tools/sentiment.py`

補足現行制度「未納入即時輿情」的缺口。

- 爬取 PTT／Dcard／Google 評論／新聞的公開討論（`_try_live_social()`，失敗不阻斷流程）
- jieba 中文分詞，領域詞彙一次性掛進詞典
- 負面關鍵字分六類，各有嚴重度權重：身體不當對待 3.0、不當管教 2.6、餐飲衛生 2.4、
  公共安全 2.4、收費爭議 1.6、人員與行政 1.3
- 情感分析含**否定詞處理**（「園方說明從來沒有體罰」不會誤判為負面）與程度副詞加權
- 子分數 = 負面強度基底（時間衰減半衰期 6 週）+ 嚴重類別加成（上限 28）+ 近期爆發加成，
  並回傳 `score_breakdown` 讓每一分都可追溯
- 語意模糊的貼文可選擇性交給 Bedrock 複判（`use_llm=true`）

### 工具 E：法規遵循檢核 — `guardian/tools/compliance.py`

訪談後新增。它和鑑識會計的差別在**證據力等級**：鑑識會計三項訊號是「異常線索」，
屬初篩；本工具檢出的是「可直接對照條文與罰則的違規事實」。混在一起加權會把最硬的證據稀釋掉。

**核心：師生比與班級人數一律分年齡層判定**

| 年齡層 | 實質師生比 | 每班上限 | 混齡 | 法源 |
|--------|-----------|---------|------|------|
| 2歲以上未滿3歲（2歲專班） | 1:8 | 16 人 | **不得混齡** | 幼照法§16 Ⅰ、Ⅳ |
| 3歲以上至入國民小學前 | 1:15 | 30 人 | — | 幼照法§16 Ⅰ、Ⅳ |
| 未滿2歲（托嬰中心） | 1:5 | — | — | 兒少機構設置標準§25 |

幼照法§16 Ⅳ 是「逐班分級」規定，不是單純比例。但可以證明：某年齡層 N 人分 C 班、
每班上限 L、第一級門檻 t（且 L = 2t），最少應置人員 = `max(C, ceil(N / t))`。
所以 t 就是實質的法定分母。程式碼在 `regulations.AgeBandRule.min_staff()`，
煙霧測試有驗證（18 人 2歲專班 → 3 人；8 人分 3 班 → 3 人）。

**合併稀釋檢查（`BLENDED_RATIO_MASKING`）**

這是訪談指出的盲點。實測案例——新店區私立麥克幼兒園：

```
全園合併    103 人 / 10 人 = 1:10.3   加權法定基準 1:13.8  → 表面合格
2歲專班      10 人 /  1 人 = 1:10.0   法定 1:8            → 違規，短缺 1 人
3歲以上      93 人 /  9 人 = 1:10.33  法定 1:15           → 合格
```

用全園平均會直接漏掉 2 歲專班的缺口。系統會標記出來並在報告中強制說明。

**其他檢核項目**：助理教保員 1/3 上限、5歲班幼兒園教師、護理人員配置型態
（201人以上須專任）、廚工人數、專任園長、超收（逾15人即§52Ⅰ①）、
幼童專用車車齡逾10年與隨車人員、幼兒團體保險、收費報備查、2歲專班室外活動區隔；
托嬰中心另檢核 1:5 托育人員、空間面積（室內每人≥2㎡、室外每人≥1.5㎡、合計≥60㎡）、
使用樓層（1–3樓）。

**欄位缺漏一律列入 `unverifiable`，不當成合規。** 報告會明確寫「無法檢核、待補資料」。

**子分數用遞減飽和函數**：`100 × (1 − e^(−點數/55))`，重大 30 點／中度 14 點／輕微 5 點。
直接截斷在 100 會讓「10 項違規含 4 項重大」和「8 項含 2 項重大」同分，排行榜失去區分度。

### 指標合理性：全體統計 + 標準差門檻

訪談結論「應參考全體統計數據並設定標準差作為判斷異常的依據」的實作：

- `forensic.citywide_baseline()`：以**全市所有機構**為母體，算出 8 項指標的平均、標準差、
  變異係數與四分位數，換算成 1σ 觀察／2σ 異常／3σ 重大偏離三段門檻（`config.SIGMA_BANDS`）
- `forensic.baseline_comparison()`：把單一機構的每項指標定位在全體統計上，回傳 z 分數與分級
- `forensic.district_stability()`：各區平均值對全市標準差的位置，加上區內離散度

區域穩定度有兩層防護：

1. **樣本量檢查**：區內機構數低於 `DISTRICT_MIN_N`（預設 5）時標記 `underpowered`，
   明確建議「改用全市全體統計」，不讓人拿 3 間機構的平均當基準
2. **離散度量測選擇**：平均值接近 0 的指標（例如結餘率）用變異係數會失真，
   自動改用「區內標準差 ÷ 全市標準差」

實測結果：新北市 12 個區每區只有 3 間機構，全部被判為樣本不足，
系統直接建議以全市基準為主 —— 這是誠實的統計結論，不是失敗。

### 工具 D：風險評分計算 — `guardian/tools/scoring.py`

**初期：規則式加權（透明可解釋）**

```
總分 = 財務異常 × 0.32 + 法規遵循 × 0.28 + 社群輿情 × 0.22 + 歷史紀錄 × 0.18
```

權重集中在 `guardian/config.py`，教育局可依實務調整。
要回到訪談前的三維度模型，把 `W_COMPLIANCE` 設為 0 並調高其他三項即可。

歷史紀錄子分數 = 裁罰次數 × 嚴重度 × 時間衰減（2.5 年半衰）+ 評鑑等第扣分。
風險等級：極高 ≥80／高 ≥65／中 ≥45／低 ≥25／正常。

**重大違規的等級下限保護欄**（`config.apply_level_floor`）

加權總分可能被其他維度的低分拉下來，但「已可開罰的違規」不該被歸為「維持例行排程」。
所以：1 項重大違規 → 等級至少「中風險」；3 項以上 → 至少「高風險」，
並在結果中回傳 `level_floor_reason` 說明為什麼被拉升。

實測：新莊區私立快樂幼兒園加權總分 44.8（原判低風險），因 4 項重大違規拉升為「高風險」。
全市 40 間中有 7 間因此被拉升等級。

分數落地後會與前次比較，達門檻或驟升就寫入 `alerts` 表主動推播；
法規遵循的重大違規也會各自產生一則預警，內容含條號與罰則。

**後期：監督式模型**

`train_supervised()` 以歷史裁罰為 label 訓練 Logistic Regression（純 Python，含 L2）。
特徵刻意排除裁罰相關欄位避免 label 洩漏。目前 40 筆樣本訓練集 AUC ≈ 0.97，
但這是訓練集內指標，回傳結果已明確標注 caveat：上線前需時序切分驗證並與 XGBoost 比較。
正例或負例少於 8 筆時會直接拒絕訓練並說明原因，不會假裝模型可用。

---

## 架構與 AWS 對應

```
                      ┌─────────────────────────────────────┐
   稽查人員 ──對話──▶ │  GuardianAgent (Bedrock Converse)   │
                      │  感知 → 規劃 → 行動 → 生成            │
                      └──────────────┬──────────────────────┘
                                     │ tool use 迴圈
              ┌──────────────┬───────┴───────┬──────────────┐
         ┌────▼────┐   ┌─────▼─────┐   ┌─────▼─────┐  ┌─────▼─────┐
         │ 工具 A  │   │  工具 B   │   │  工具 C   │  │  工具 D   │
         │  ETL    │   │ 鑑識會計  │   │ 輿情 NLP  │  │ 風險評分  │
         └────┬────┘   └─────┬─────┘   └─────┬─────┘  └─────┬─────┘
              └──────────────┴───────┬───────┴──────────────┘
                            ┌────────▼────────┐
                            │  整合資料庫      │
                            │  + 稽核軌跡      │
                            └────────┬────────┘
                        ┌────────────▼────────────┐
                        │  FastAPI + 前端儀表板    │
                        │  排行榜／地圖／預警／對話 │
                        └─────────────────────────┘
```

| 提案規劃 | 目前實作 | 正式化路線 |
|---------|---------|-----------|
| Bedrock Agents + Action Groups | Bedrock Converse API tool use 迴圈（`guardian/agent.py`） | `infra/deploy_bedrock_agent.py` 把同一份 toolspec 轉成 Action Group |
| Lambda + API Gateway | FastAPI（`api.py`） | `infra/lambda_handler.py` 已可直接當 Action Group 執行器 |
| EventBridge 排程 | `perceive()` / `auto_investigate()` 可被任何排程器呼叫 | EventBridge Rule → Lambda |
| RDS PostgreSQL / DynamoDB | SQLite（`guardian/store.py`，SQL 為標準語法） | 換連線字串即可 |
| S3 | `aws.s3_put()`，設 `GUARDIAN_S3_BUCKET` 後自動同步原始快照與報告 | 已就緒 |
| Titan Embeddings | `aws.embed()` 已接好 | 可用於輿情語意分群 |
| React + Amplify | 單檔前端（`web/index.html`，Leaflet 地圖） | 改寫為 React 後部署 Amplify |

### 部署為雲端 Bedrock Agents

```powershell
.\.venv\Scripts\python.exe infra\deploy_bedrock_agent.py --dry-run   # 先看計畫
.\.venv\Scripts\python.exe infra\deploy_bedrock_agent.py --apply     # 真的建立
```

dry-run 已實測可跑（會列出 2 個 IAM Role、1 個 Lambda、4 個 Action Group、50 KB 部署包）。
**`--apply` 尚未執行過**：它會在你的帳號建立實體資源，而且工作坊型帳號（`WSParticipantRole`）
通常沒有 `iam:CreateRole` 權限。要跑之前請先確認權限，或改用既有角色 ARN。

---

## 環境變數

| 變數 | 預設 | 說明 |
|------|------|------|
| `AWS_REGION` | `us-west-2` | Bedrock 區域 |
| `AWS_PROFILE` | （default） | 指定 profile |
| `GUARDIAN_MODEL_ID` | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | 主模型；失敗會自動試候補清單 |
| `GUARDIAN_S3_BUCKET` | 未設定 | 設了才會把原始快照與報告同步到 S3 |
| `GUARDIAN_DB` | `data/guardian.db` | 資料庫路徑 |
| `W_FINANCIAL` / `W_SENTIMENT` / `W_HISTORY` | 0.45 / 0.30 / 0.25 | 三大子分數權重 |
| `ALERT_TOTAL` / `ALERT_DELTA` | 65 / 12 | 預警門檻與驟升幅度 |

---

## 真實資料匯入（新北市政府提供）

盤點 AWS 帳號後，在 S3（us-west-2）找到 21 個 PDF、670 MB。
bucket 名稱請用環境變數指定，不寫在原始碼裡：

```powershell
$env:GUARDIAN_DATA_BUCKET = "你放資料的-bucket-名稱"
```

| 目錄 | 內容 | 檔案 | PDF 型態 | 解析方式 |
|------|------|------|---------|---------|
| `公校/` | 112／113／114 年度決算書，各 5 冊 | 15 | **文字型**（約 752 頁/冊） | pdfplumber 直接解析 |
| `非營利園財報/` | 113 學年度會計師查核財務報告 | 6 | **掃描影像**（44–45 頁） | Bedrock 視覺模型 OCR |

盤點腳本：`scripts/discover_aws_data.py`（唯讀，會掃 S3／Glue／DynamoDB／RDS／Bedrock KB／Lambda）

### 為什麼掃描影像不用 Amazon Textract

Textract 的 `DetectDocumentText` / `AnalyzeDocument` 目前**不支援繁體中文**，對這批財報抽不出東西。
改用 Bedrock 的 Claude Sonnet 4.5 視覺能力直接讀頁面影像，繁中表格辨識實測 confidence 0.85–0.95。

### 兩段式 OCR（控制成本與時間）

```
Pass 1 定位   90 DPI，每次 4 頁批次送入 → 只做頁面分類
              44 頁 → 11 次呼叫，找出哪幾頁是收支表／人事費用／預算執行
Pass 2 抽取   200 DPI，逐頁抽表格明細 → 結構化 JSON
              只抽高優先頁面（跳過 9 頁財產目錄，省約 40% 呼叫）
```

所有結果逐頁快取在 `data/raw/ocr/`，重跑不會重複花 token。

實測一份財報（N07北大）：45 頁 → 分類後鎖定 13 頁 → 抽出 **298 筆明細**，耗時 338 秒。

### 途中修掉的兩個實際問題

**1. 回覆被 maxTokens 截斷**
第一次抽取全部回傳「無法辨識」。原因不是 OCR 讀不到（它正確認出「收支餘絀表」
「民國113年8月1日至114年7月31日」「新臺幣元」），而是長表格的 JSON 輸出被截斷、解析失敗。
兩個對策：把輸出格式從逐欄命名的物件改成緊湊陣列（省約 60% token），
並加上 `_repair_truncated()`——截斷時砍到最後一個完整元素再補括號，已讀到的明細照樣可用。

**2. 模型回的欄位名跟我預設的不同**
實際 收支餘絀表 有「預算數／決算數／比較增減／執行率」四欄，模型很合理地照原表回
`budget/actual/difference/execution_rate`，而不是我要求的簡化 `amount`。
這其實比硬套 schema 更準，所以改成在我這邊做欄位別名正規化（`_COL_ALIASES`），
把 `actual`／`決算數`／`本年度` 都對映到 `amount`。

### 抽出來的真實數字（範例）

新北市新樂非營利幼兒園（委託財團法人三之三生命教育基金會辦理）：

| 項目 | 113 學年度 | 112 學年度 |
|------|-----------|-----------|
| 教保費收入 | 24,377,000 | 23,264,830 |
| 收入合計 | 30,175,635 | 27,277,563 |
| 人事費 | 14,347,554 | 14,997,656 |
| 支出合計 | 29,190,029 | 27,618,951 |
| 餘絀 | +985,606 | **−341,388** |
| 人事費占支出比 | 49.2% | 54.3% |

收入 +10.6% 但人事費 −4.3%、人事費占比掉 5 個百分點——這正是鑑識會計要抓的訊號類型。

公校決算書（112 年度第一冊，752 頁）抽出：
- **22 間新北市立幼兒園名冊**（板橋、三重、中和、永和、新莊…烏來）
- 真實補助基準：延長照顧服務每生每學期最高 12,600 元；每生最高 3,500 元、
  暑假期間每生每月最高 2,000 元；弱勢家庭幼生午餐點心每人每月最高 650 元
- 這些寫入 `fee_standards` 表，供交叉比對工具判斷收費合理性

### 資料品質的三道防線

1. **關聯實體過濾**：財報常夾帶同一受託法人辦理的其他業務報表
   （例如 N07 的 p7 是「公共托育中心暨親子館」收支餘絀表）。這些是不同服務實體，
   混進去會直接扭曲人事費占比，因此 `_is_related_entity()` 會擋掉並記錄在
   `skipped_related_entity_pages` 供追溯。
2. **低信心標記**：OCR confidence < 0.9 的頁面列入 `low_confidence_pages`，
   Agent 的系統提示要求報告中註明可能有辨識誤差。
3. **機構名稱驗證**：決算書是連續排版，正則容易跨行誤抓
   （例如「及設備辦理非營利幼兒園」）。`is_valid_institution_name()` 會濾掉這類片段，
   被拒絕的字串記在 `rejected_name_fragments` 以便調整規則。

### 執行方式

OCR 會呼叫 Bedrock 並產生費用，所以**不併入 `start.ps1`**，要另外執行：

```powershell
# 1. 盤點 AWS 帳號有什麼資料（唯讀）
.\.venv\Scripts\python.exe scripts\discover_aws_data.py --json data\aws_inventory.json

# 2. 看 S3 檔案與快取狀態
.\.venv\Scripts\python.exe scripts\ingest_real_data.py inventory

# 3. 先分類單一檔案（便宜，確認頁面地圖）
.\.venv\Scripts\python.exe scripts\ingest_real_data.py classify "非營利園財報/113學年度/N28新樂_113學年度財務報告.pdf"

# 4. 抽取（可先指定頁碼試跑）
.\.venv\Scripts\python.exe scripts\ingest_real_data.py extract "非營利園財報/..." --pages 6 7 17

# 5. 跑完所有非營利園財報
.\.venv\Scripts\python.exe scripts\ingest_real_data.py nonprofit

# 6. 公校決算書（純文字解析，不花 token）
.\.venv\Scripts\python.exe scripts\ingest_real_data.py public --limit 1

# 7. 寫入整合資料庫
.\.venv\Scripts\python.exe scripts\ingest_real_data.py load
```

隨時檢視狀態：`cli.py realdata`（加 `--load` 才寫入資料庫）。

載入後 `data_source_mode` 會變成 `real`，Agent 的系統提示會跟著切換，
報告裡會註明資料來自真實文件而非示範資料集。
`data_integration_tool(action=data_sources)` 可查每一筆財務明細的實際出處與年度範圍。

---

## 資料來源說明

`etl.refresh()` 會先嘗試抓真實開放資料（全國教保資訊網、新北市資料開放平臺）。
本次實測這些端點**不可用**，因此自動退回 `guardian/seed.py` 的內建示範資料集：
40 間機構、9,215 筆決算逐筆金額、120 筆收費公告、26 筆裁罰、275 則社群貼文。

示範資料集是以固定亂數種子產生的，結構與官方欄位一致，並刻意埋入六種樣態
（乾淨／收入落差／財報數字不自然／師資與人事費異常／輿情負面／複合型），
用來驗證工具真的抓得出異常。Benford 對照組用 log-uniform 取樣（自然符合定律），
異常組用整數千元偏好 + 均勻首位（刻意偏離）。

來源模式會記錄在 `store.meta` 的 `etl_source_mode`，並注入 Agent 系統提示，
所以 Agent 產出的報告會自己標注「本報告使用內建示範資料集」。
要接真實資料，改 `etl.OFFICIAL_SOURCES` 的端點與 `_map_basic_row()` 的欄位對應即可。

---

## 安全性

- **`api.py` 沒有任何身分驗證**，預設只綁 `127.0.0.1`。內含機構財務與民眾投訴內容，屬敏感資料。
  對外開放前必須先加：Cognito／API Gateway Authorizer、HTTPS、CORS 白名單、稽核日誌、
  依角色限制可見機構範圍。
- 前端所有動態內容都經過 HTML escape。
- SQL 全部使用參數化查詢。
- 認證修正腳本會備份原檔，且不輸出金鑰內容。
- 爬蟲帶可識別的 User-Agent，只抓公開頁面。
- 所有工具呼叫寫入 `audit_log`，可用 `GET /api/audit` 查「Agent 到底查了什麼」。

---

## 檔案結構

```
guardian/
  regulations.py   ★ 法規基準單一真實來源：年齡層規則、法源引用、地方從嚴覆寫
  config.py        設定、權重、風險等級、σ 門檻、等級下限保護欄
  aws.py           Bedrock / S3 用戶端、模型可用性偵測、Titan embeddings
  store.py         整合資料庫 schema（含 ALTER TABLE 增量遷移）、統一機構 ID、稽核軌跡
  seed.py          示範資料集產生器（八種異常樣態，含 ratio2y 合併稀釋案例）
  toolspec.py      五大工具的 Bedrock toolConfig 與執行分派
  agent.py         Agent 主體：tool use 迴圈、法規硬規則、感知層、自動閉環
  tools/
    etl.py         工具 A：資料整合
    forensic.py    工具 B：鑑識會計（比率／Benford／交叉比對）+ 全體統計基準 + 區域穩定度
    sentiment.py   工具 C：NLP 輿情
    scoring.py     工具 D：風險評分 + 監督式模型
    compliance.py  工具 E：法規遵循檢核（分年齡層判定 + 合併稀釋檢查）
api.py             FastAPI（對應 API Gateway + Lambda）
cli.py             命令列介面
web/index.html     儀表板：排行榜／地圖熱區／預警／分年齡層師生比／違規清單／對話介面
.kiro/steering/
  ntpc-regulatory-baseline.md   ★ 訪談結論與法源的完整記錄（判定邏輯的參考依據）
infra/
  lambda_handler.py       Bedrock Agents Action Group 執行器
  deploy_bedrock_agent.py 雲端部署腳本（dry-run 已測，--apply 未執行）
scripts/           驗證與工具腳本
reports/           Agent 產出的報告
data/guardian.db   整合資料庫
```

---

## 已知限制

1. 官方開放資料端點目前不可用，`OFFICIAL_SOURCES` 的 URL 需要依實際 API 文件校正。
2. 社群 live 爬取只實作了 PTT 搜尋，Dcard／Google 評論需要各自的 API 或授權。
3. 監督式模型的 AUC 是訓練集內指標，樣本量（40）不足以支撐上線宣稱。
4. `infra/deploy_bedrock_agent.py --apply` 尚未實際執行驗證。
5. Benford 檢定對「金額分佈本身受限」的科目（例如固定月費）會有偽陽性，
   實務上應排除這類科目再檢定。
