"""診斷政府網站的 TLS 憑證問題，並試出可用的取得方式。

不預設關閉憑證驗證：先確認失敗原因（憑證鏈不完整／CA 不在信任庫／TLS 攔截），
再決定正確作法。
"""

from __future__ import annotations

import json
import socket
import ssl
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "data" / "probe_ssl.txt"
buf: list[str] = []

HOSTS = [
    ("data.ntpc.gov.tw", 443),
    ("ap.ece.moe.edu.tw", 443),
    ("data.gov.tw", 443),
]


def w(s: str = "") -> None:
    buf.append(s)


def main() -> None:
    try:
        import certifi
        import requests
        w(f"certifi 版本檔案 = {certifi.where()}")
        w(f"requests 版本 = {requests.__version__}")
    except ImportError as exc:
        w(f"缺套件：{exc}")
        return

    w(f"OpenSSL = {ssl.OPENSSL_VERSION}")
    w()

    for host, port in HOSTS:
        w("=" * 72)
        w(f"{host}:{port}")
        # 1) 預設驗證
        ctx = ssl.create_default_context(cafile=certifi.where())
        try:
            with socket.create_connection((host, port), timeout=15) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as ss:
                    cert = ss.getpeercert()
                    w("  [certifi] 驗證成功")
                    w(f"    subject = {cert.get('subject')}")
                    w(f"    issuer  = {cert.get('issuer')}")
                    w(f"    notAfter= {cert.get('notAfter')}")
        except Exception as exc:  # noqa: BLE001
            w(f"  [certifi] 失敗：{type(exc).__name__}: {str(exc)[:220]}")

        # 2) 用 Windows 系統憑證庫（政府 CA 常只裝在系統信任庫）
        try:
            ctx2 = ssl.create_default_context()
            ctx2.load_default_certs(ssl.Purpose.SERVER_AUTH)
            with socket.create_connection((host, port), timeout=15) as sock:
                with ctx2.wrap_socket(sock, server_hostname=host) as ss:
                    w("  [系統憑證庫] 驗證成功")
        except Exception as exc:  # noqa: BLE001
            w(f"  [系統憑證庫] 失敗：{type(exc).__name__}: {str(exc)[:220]}")

        # 3) 不驗證，只為了看伺服器實際送出什麼憑證（純診斷）
        try:
            ctx3 = ssl._create_unverified_context()  # noqa: SLF001
            with socket.create_connection((host, port), timeout=15) as sock:
                with ctx3.wrap_socket(sock, server_hostname=host) as ss:
                    der = ss.getpeercert(binary_form=True)
                    w(f"  [不驗證] 連線成功，憑證長度 {len(der)} bytes")
                    try:
                        import ssl as _s
                        pem = _s.DER_cert_to_PEM_cert(der)
                        w(f"    PEM 前 80 字 = {pem[:80]!r}")
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as exc:  # noqa: BLE001
            w(f"  [不驗證] 仍失敗：{type(exc).__name__}: {str(exc)[:220]}")
        w()

    # 4) data.gov.tw 需要 POST
    w("=" * 72)
    w("data.gov.tw 資料集查詢（POST）")
    try:
        import requests
        r = requests.post(
            "https://data.gov.tw/api/v2/rest/dataset",
            json={"q": "幼兒園", "limit": 20},
            headers={"Content-Type": "application/json",
                     "User-Agent": "Mozilla/5.0"},
            timeout=25)
        w(f"  狀態 = {r.status_code}　長度 = {len(r.content)}")
        w(f"  前 600 字 = {r.text[:600]!r}")
    except Exception as exc:  # noqa: BLE001
        w(f"  失敗：{type(exc).__name__}: {str(exc)[:220]}")


if __name__ == "__main__":
    main()
    OUT.write_text("\n".join(buf), encoding="utf-8")
