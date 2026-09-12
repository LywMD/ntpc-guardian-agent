"""小小守護員 Agent 的全域設定。"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
REPORT_DIR = ROOT / "reports"
DB_PATH = Path(os.getenv("GUARDIAN_DB", DATA_DIR / "guardian.db"))

for _d in (DATA_DIR, RAW_DIR, REPORT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- AWS
AWS_REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-west-2"
AWS_PROFILE = os.getenv("AWS_PROFILE") or None

# 可用 GUARDIAN_MODEL_ID 覆寫；預設走 us-* 跨區推論設定檔（us-west-2 需要）
BEDROCK_MODEL_ID = os.getenv("GUARDIAN_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
# 找不到上述模型時的候補清單（依偏好排序）
# 實測 us-west-2 / WSParticipantRole 可用（scripts/probe_models.py）
MODEL_FALLBACKS = [
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.anthropic.claude-sonnet-4-20250514-v1:0",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.amazon.nova-pro-v1:0",
    "us.amazon.nova-lite-v1:0",
]
EMBED_MODEL_ID = os.getenv("GUARDIAN_EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")

# 選用：把原始 PDF / 報告同步到 S3（未設定則只留在本機）
S3_BUCKET = os.getenv("GUARDIAN_S3_BUCKET") or None
S3_PREFIX = os.getenv("GUARDIAN_S3_PREFIX", "guardian/")

MAX_AGENT_TURNS = int(os.getenv("GUARDIAN_MAX_TURNS", "16"))

# ---------------------------------------------------------------- 資料來源政策
# 只使用 AWS 上的資料，不對外抓取任何網站。
#
# 為什麼預設關閉對外抓取：
#   1. 政府開放資料站台實測不可用——data.ntpc.gov.tw 與 ap.ece.moe.edu.tw 的
#      憑證鏈有缺陷（缺 Subject Key Identifier／未送中介憑證），
#      data.gov.tw 需要申請 API Key。抓不到卻留著呼叫只會產生誤導性的錯誤訊息。
#   2. PTT 等社群平台的搜尋頁結構隨時會變，抓回來的標題也無法保證真的在講該機構，
#      誤植到特定幼兒園身上是會傷害到具體對象的錯誤。
#
# 因此資料來源限定為：Amazon S3 上新北市政府提供的文件
#   非營利園財報/  6 份（掃描影像 → Bedrock 視覺 OCR）
#   公校/         15 冊（文字型 → pdfplumber 解析）
# 需要臨時開啟對外抓取時，設環境變數 GUARDIAN_ALLOW_EXTERNAL_FETCH=1。
ALLOW_EXTERNAL_FETCH = os.getenv("GUARDIAN_ALLOW_EXTERNAL_FETCH", "0") == "1"

# S3 上存放新北市政府文件的 bucket（唯一的真實資料來源）
DATA_BUCKET = os.getenv("GUARDIAN_DATA_BUCKET") or None

# ---------------------------------------------------------------- 對外連線白名單
# 部署到 AWS 對外開放時，只允許這些來源位址連線。
#
# 為什麼要在應用層再擋一次（Security Group 已經擋過了）：
#   Security Group 是網路層的單點防線，一旦被改寬、或服務被放到
#   ALB／CloudFront 後面而回源沒鎖好，應用就直接裸奔。這個服務沒有身分驗證，
#   且內含機構財務與民眾投訴內容，破口的代價很高，所以兩層都擋。
#
# 重要限制（部署前務必知道）：
#   IP 白名單不是身分驗證。它只能回答「連線從哪裡來」，無法回答「這是誰」，
#   因此同一個出口 IP 後面的所有人都會被視為同一個合法使用者，
#   也無法做操作稽核與權限分級。正式上線仍須加 Cognito／IAM 等身分層。
DEFAULT_ALLOWED_IPS = [
    "127.0.0.1", "::1",          # 本機
    "60.250.71.45",
    "61.222.117.53",
    "59.125.121.41",
    "60.250.71.43",
]
# 逗號分隔，支援單一位址與 CIDR（例：203.0.113.0/24）
ALLOWED_IPS = [
    s.strip() for s in os.getenv(
        "GUARDIAN_ALLOWED_IPS", ",".join(DEFAULT_ALLOWED_IPS)).split(",")
    if s.strip()
]
# 是否啟用白名單檢查。綁在 127.0.0.1 本機示範時不需要，
# 一旦綁到 0.0.0.0 對外服務就必須開啟（cli.py serve 會自動判斷並開啟）。
ENFORCE_IP_ALLOWLIST = os.getenv("GUARDIAN_ENFORCE_IP_ALLOWLIST", "0") == "1"

# 是否信任 X-Forwarded-For 取真實來源 IP。
#
# 只有「確定服務在 ALB／CloudFront／API Gateway 後面」時才可以開啟：
# 若服務直接對外，用戶端可以自行偽造這個標頭繞過白名單。
# 預設關閉＝以 TCP 連線來源位址為準，這在直連情境下才是可信的。
TRUST_PROXY_HEADER = os.getenv("GUARDIAN_TRUST_PROXY_HEADER", "0") == "1"
# 反向代理自身的位址（信任這些 hop 之後才往前取 XFF）
TRUSTED_PROXIES = [
    s.strip() for s in os.getenv("GUARDIAN_TRUSTED_PROXIES", "").split(",") if s.strip()
]

# ---------------------------------------------------------------- 評分權重
# 初期採規則式加權，權重集中在此處以便教育局依實務調整。
#
# 法規遵循子分數（compliance）為訪談後新增的第 4 個維度。理由：
#   鑑識會計三項訊號屬「異常線索」，證據力是初篩等級；
#   法規遵循檢出的是可直接對照條文與罰則的違規事實，證據力完全不同等級。
#   混在財務異常裡加權會把最硬的證據稀釋掉，因此獨立配權。
# 若要回到訪談前的三維度模型，把 W_COMPLIANCE 設為 0 並調高其他三項即可。
SCORE_WEIGHTS = {
    "financial": float(os.getenv("W_FINANCIAL", "0.32")),   # 財務異常（鑑識會計層）
    "compliance": float(os.getenv("W_COMPLIANCE", "0.28")),  # 法規遵循（可對照條文與罰則）
    "sentiment": float(os.getenv("W_SENTIMENT", "0.22")),   # 輿情異常（NLP 層）
    "history": float(os.getenv("W_HISTORY", "0.18")),       # 歷史紀錄（裁罰／評鑑）
}

# ---------------------------------------------------------------- 統計判定門檻
# 教育局／城鄉發展局訪談結論：指標合理性應參考全體統計數據，並以標準差設定異常門檻。
SIGMA_BANDS = {
    "observe": float(os.getenv("SIGMA_OBSERVE", "1.0")),    # 1σ：列入觀察
    "abnormal": float(os.getenv("SIGMA_ABNORMAL", "2.0")),  # 2σ：判定異常
    "critical": float(os.getenv("SIGMA_CRITICAL", "3.0")),  # 3σ：重大偏離
}
# 區內變異係數上限。超過即代表該區資料本身離散偏大，
# 對應訪談中「不同區域可能存在數據波動，但理想上應維持在穩定範圍內」。
DISTRICT_CV_STABLE_MAX = float(os.getenv("DISTRICT_CV_MAX", "0.25"))
# 平均值接近 0 的指標（例如結餘率）用變異係數會失真，改用「區內std ÷ 全市std」
DISTRICT_DISPERSION_RATIO_MAX = float(os.getenv("DISTRICT_DISPERSION_MAX", "1.3"))
# 區級統計量至少要有幾間機構才值得當比較基準
DISTRICT_MIN_N = int(os.getenv("DISTRICT_MIN_N", "5"))

# 財務異常子分數內部三項訊號的加權
FORENSIC_WEIGHTS = {
    "ratio": float(os.getenv("W_RATIO", "0.40")),      # 財務比率分析
    "benford": float(os.getenv("W_BENFORD", "0.25")),  # Benford's Law
    "cross": float(os.getenv("W_CROSS", "0.35")),      # 收費 vs 決算交叉比對
}

RISK_BANDS = [
    (80, "極高風險", "立即安排實地稽查"),
    (65, "高風險", "7 日內排入稽查名單"),
    (45, "中風險", "列入觀察，30 日內複核"),
    (25, "低風險", "維持例行排程"),
    (0, "正常", "無須額外處置"),
]

# 觸發預警的門檻
ALERT_TOTAL_THRESHOLD = float(os.getenv("ALERT_TOTAL", "65"))
ALERT_DELTA_THRESHOLD = float(os.getenv("ALERT_DELTA", "12"))  # 分數驟升幅度

# 交叉比對：預期收入 vs 申報收入 差異門檻
REVENUE_GAP_WARN = 0.15
REVENUE_GAP_HIGH = 0.25

# 法定師生比基準已移到 guardian/regulations.py，依年齡層分別定義並附法源條號。
# 這裡僅保留「全園合併」的粗略參考值，供趨勢圖表使用，不得用於合規判定。
BLENDED_RATIO_REFERENCE = {
    "幼兒園": 15.0,      # 實際須拆成 2歲專班 1:8 與 3歲以上 1:15
    "托嬰中心": 5.0,
    "課後照顧中心": 25.0,
    "兒少安置機構": 6.0,
}


BAND_ORDER = ["正常", "低風險", "中風險", "高風險", "極高風險"]
BAND_ACTION = {label: action for _, label, action in RISK_BANDS}
BAND_ACTION["正常"] = "無須額外處置"

# 法規遵循重大違規（可直接對照罰則）數量 → 風險等級下限。
# 理由：加權總分可能被其他維度的低分拉下來，但「已可開罰的違規」不該被歸為
# 「維持例行排程」。這是政策層面的保護欄，不是統計判斷。
CRITICAL_VIOLATION_FLOOR = [(3, "高風險"), (1, "中風險")]


def risk_band(total: float) -> tuple[str, str]:
    for threshold, label, action in RISK_BANDS:
        if total >= threshold:
            return label, action
    return "正常", BAND_ACTION["正常"]


def apply_level_floor(level: str, critical_violations: int) -> tuple[str, str, str | None]:
    """依重大違規數把風險等級拉到下限。回傳 (等級, 建議處置, 拉升原因)。"""
    for n, floor in CRITICAL_VIOLATION_FLOOR:
        if critical_violations >= n and BAND_ORDER.index(floor) > BAND_ORDER.index(level):
            return floor, BAND_ACTION[floor], (
                f"加權總分原判為「{level}」，但法規遵循檢出 {critical_violations} 項"
                f"可直接對照罰則的重大違規，依政策保護欄拉升至「{floor}」")
    return level, BAND_ACTION.get(level, "無須額外處置"), None
