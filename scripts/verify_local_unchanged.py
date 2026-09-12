"""確認加入白名單後，本機示範模式行為完全不變。

避免的風險：把對外管制做進去，結果本機 `python cli.py serve` 也被擋，
或反過來——綁 0.0.0.0 時忘了啟用白名單就裸奔。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "data" / "verify_local_unchanged.txt"
buf: list[str] = []
problems: list[str] = []


def w(s: str = "") -> None:
    buf.append(s)


def check(label: str, ok: bool, detail: str = "") -> None:
    w(f"  [{'OK  ' if ok else 'FAIL'}] {label}{('　' + detail) if detail else ''}")
    if not ok:
        problems.append(label)


w("=" * 72)
w("一、綁定位址判定（決定是否自動啟用白名單）")
w("=" * 72)
import cli  # noqa: E402

for host, want_loopback in (("127.0.0.1", True), ("localhost", True),
                            ("::1", True), ("", True),
                            ("0.0.0.0", False), ("10.0.1.20", False),
                            ("60.250.71.45", False)):
    got = cli._is_loopback(host)
    check(f"{host or '(空)'} → {'本機' if want_loopback else '對外'}",
          got == want_loopback, f"判定 {'本機' if got else '對外'}")

w()
w("=" * 72)
w("二、預設（未設環境變數）不啟用白名單 → 本機示範不受影響")
w("=" * 72)
os.environ.pop("GUARDIAN_ENFORCE_IP_ALLOWLIST", None)
os.environ.pop("GUARDIAN_ALLOWED_IPS", None)

# 用子行程確認乾淨載入的預設值，避免本行程既有的 import 影響結果
import subprocess  # noqa: E402

code = (
    "import sys; sys.path.insert(0,'.');"
    "from guardian import config;"
    "print(config.ENFORCE_IP_ALLOWLIST, config.TRUST_PROXY_HEADER,"
    " len(config.ALLOWED_IPS))"
)
env = {k: v for k, v in os.environ.items()
       if not k.startswith("GUARDIAN_ENFORCE") and not k.startswith("GUARDIAN_ALLOWED")}
env["PYTHONIOENCODING"] = "utf-8"
r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                   cwd=str(ROOT), env=env, timeout=120)
out = (r.stdout or "").strip()
w(f"      子行程輸出：{out!r}　stderr：{(r.stderr or '')[:200]!r}")
parts = out.split()
check("預設 ENFORCE_IP_ALLOWLIST = False", parts and parts[0] == "False", out)
check("預設 TRUST_PROXY_HEADER = False", len(parts) > 1 and parts[1] == "False", out)
check("預設白名單含 6 筆（本機 2 + 指定 4）",
      len(parts) > 2 and parts[2] == "6", out)

w()
w("=" * 72)
w("三、未啟用白名單時，任何來源都能存取（本機示範行為）")
w("=" * 72)
probe = ROOT / "data" / "_probe_local_open.py"
probe.write_text(
    "import asyncio, os, sys\n"
    "sys.path.insert(0, '.')\n"
    "os.environ.pop('GUARDIAN_ENFORCE_IP_ALLOWLIST', None)\n"
    "import api\n"
    "st = {}\n"
    "async def recv():\n"
    "    return {'type': 'http.request', 'body': b'', 'more_body': False}\n"
    "async def send(m):\n"
    "    if m['type'] == 'http.response.start':\n"
    "        st['c'] = m['status']\n"
    "scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',\n"
    "         'method': 'GET', 'scheme': 'http', 'path': '/api/health',\n"
    "         'raw_path': b'/api/health', 'query_string': b'', 'root_path': '',\n"
    "         'headers': [(b'host', b'x')], 'client': ('203.0.113.9', 1),\n"
    "         'server': ('x', 80)}\n"
    "asyncio.run(api.app(scope, recv, send))\n"
    "print(st.get('c'))\n",
    encoding="utf-8")
r2 = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True,
                    cwd=str(ROOT), env=env, timeout=300)
probe.unlink(missing_ok=True)
out2 = (r2.stdout or "").strip().splitlines()
last = out2[-1] if out2 else ""
w(f"      子行程輸出：{last!r}　stderr：{(r2.stderr or '')[-300:]!r}")
check("未啟用白名單時外部來源可存取（預設本機模式不變）", last == "200", last)

w()
w("=" * 72)
w(f"結論：{'全部通過' if not problems else f'{len(problems)} 項未通過'}")
for p in problems:
    w(f"  - {p}")
w("=" * 72)

OUT.write_text("\n".join(buf), encoding="utf-8")
raise SystemExit(1 if problems else 0)
