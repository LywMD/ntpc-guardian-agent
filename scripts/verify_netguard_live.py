"""端到端驗證：實際啟一個啟用白名單的伺服器，確認會擋、也會放行。

用 ASGI transport 直接打 app，不佔用連接埠，也不受本機防火牆影響。
重點是驗證 middleware 真的掛在 ASGI 最外層——連靜態首頁與不存在的路由
都要被擋掉，不能只擋到有定義的 API。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "data" / "verify_netguard_live.txt"
buf: list[str] = []
problems: list[str] = []


def w(s: str = "") -> None:
    buf.append(s)


def check(label: str, ok: bool, detail: str = "") -> None:
    w(f"  [{'OK  ' if ok else 'FAIL'}] {label}{('　' + detail) if detail else ''}")
    if not ok:
        problems.append(label)


async def call(app, path: str, peer: str, headers: list[tuple[bytes, bytes]] | None = None):
    """直接以 ASGI 介面呼叫，模擬指定來源位址。"""
    status = {"code": None}
    chunks: list[bytes] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            status["code"] = msg["status"]
        elif msg["type"] == "http.response.body":
            chunks.append(msg.get("body") or b"")

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "",
        "headers": headers or [(b"host", b"testserver")],
        "client": (peer, 12345), "server": ("testserver", 80),
    }
    await app(scope, receive, send)
    return status["code"], b"".join(chunks)


def main() -> None:
    # 必須在 import api 之前設定，middleware 是在模組載入時掛上的
    os.environ["GUARDIAN_ENFORCE_IP_ALLOWLIST"] = "1"
    os.environ["GUARDIAN_ALLOWED_IPS"] = (
        "127.0.0.1,::1,60.250.71.45,61.222.117.53,59.125.121.41,60.250.71.43")
    os.environ.pop("GUARDIAN_TRUST_PROXY_HEADER", None)

    import api  # noqa: E402

    app = api.app
    w("=" * 72)
    w("一、四個指定位址可以開啟程式（首頁與 API）")
    w("=" * 72)
    for ip in ("60.250.71.45", "61.222.117.53", "59.125.121.41", "60.250.71.43"):
        code, _ = asyncio.run(call(app, "/", ip))
        check(f"{ip} 開啟首頁", code == 200, f"HTTP {code}")
    code, body = asyncio.run(call(app, "/api/health", "60.250.71.45"))
    check("允許來源可呼叫 /api/health", code == 200, f"HTTP {code}")

    w()
    w("=" * 72)
    w("二、未授權來源一律 403（含首頁與未定義路由）")
    w("=" * 72)
    for path in ("/", "/api/health", "/api/leaderboard", "/does-not-exist"):
        code, body = asyncio.run(call(app, path, "8.8.8.8"))
        check(f"8.8.8.8 存取 {path} 被拒", code == 403, f"HTTP {code}")
    code, body = asyncio.run(call(app, "/", "60.250.71.44"))
    check("鄰近位址 60.250.71.44 被拒（確認 /32）", code == 403, f"HTTP {code}")

    w()
    w("=" * 72)
    w("三、健康檢查端點不受白名單限制（供 ALB 使用）")
    w("=" * 72)
    code, body = asyncio.run(call(app, "/healthz", "10.0.3.55"))
    check("VPC 內部位址可存取 /healthz", code == 200, f"HTTP {code}")
    check("/healthz 不含機構資料",
          b"inst" not in body.lower() and b"leaderboard" not in body.lower(),
          body[:80].decode("utf-8", "replace"))

    w()
    w("=" * 72)
    w("四、偽造 X-Forwarded-For 無法繞過（未啟用代理信任）")
    w("=" * 72)
    hdrs = [(b"host", b"testserver"), (b"x-forwarded-for", b"60.250.71.45")]
    code, _ = asyncio.run(call(app, "/", "8.8.8.8", hdrs))
    check("外部來源偽造 XFF 仍被拒", code == 403, f"HTTP {code}")

    w()
    w("=" * 72)
    w("五、連線管制設定可自我確認")
    w("=" * 72)
    code, body = asyncio.run(call(app, "/api/access-policy", "60.250.71.45"))
    check("/api/access-policy 可讀", code == 200, f"HTTP {code}")
    if code == 200:
        d = json.loads(body.decode("utf-8"))
        w(f"      {json.dumps(d, ensure_ascii=False)[:400]}")
        check("回報白名單已啟用", d.get("ip_allowlist_enforced") is True)
        check("回報四個允許來源",
              all(ip in d.get("allowed_sources", []) for ip in
                  ("60.250.71.45", "61.222.117.53", "59.125.121.41", "60.250.71.43")))
        check("明確標示沒有身分驗證", d.get("authentication") == "none")

    w()
    w("=" * 72)
    w(f"結論：{'全部通過' if not problems else f'{len(problems)} 項未通過'}")
    for p in problems:
        w(f"  - {p}")
    w("=" * 72)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        import traceback
        buf.append(traceback.format_exc())
        problems.append("腳本異常")
    OUT.write_text("\n".join(buf), encoding="utf-8")
    raise SystemExit(1 if problems else 0)
