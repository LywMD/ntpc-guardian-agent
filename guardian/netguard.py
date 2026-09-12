"""來源 IP 白名單（應用層防線）。

與 AWS Security Group 形成兩層防護：
  網路層  Security Group / API Gateway resource policy → 擋在流量進到主機之前
  應用層  本模組的 middleware                          → 擋在進到路由之前

為什麼兩層都要：這個服務沒有身分驗證，內含機構財務與民眾投訴內容。
Security Group 若被改寬（或服務被移到 ALB 後面而回源沒鎖好），
只有單層防線就等於完全開放。應用層這道與網路設定各自獨立，
其中一邊失守時另一邊還在。

取得真實來源 IP 的原則
  直連情境：以 TCP 連線的來源位址為準（request.client.host）。
  代理情境：只有在明確設定 TRUST_PROXY_HEADER=1 時才讀 X-Forwarded-For。
            若服務直接對外卻信任該標頭，用戶端可自行偽造標頭繞過白名單——
            這是 IP 白名單最常見的實作漏洞，因此預設關閉。
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Any, Iterable

log = logging.getLogger("guardian.netguard")


def parse_networks(entries: Iterable[str]) -> list[ipaddress._BaseNetwork]:
    """把設定字串轉成網段物件。單一位址視為 /32（IPv6 為 /128）。"""
    nets: list[ipaddress._BaseNetwork] = []
    for raw in entries:
        s = (raw or "").strip()
        if not s:
            continue
        try:
            nets.append(ipaddress.ip_network(s, strict=False))
        except ValueError:
            log.warning("白名單項目無法解析，已略過：%r", s)
    return nets


def is_allowed(ip: str, networks: Iterable[ipaddress._BaseNetwork]) -> bool:
    """判斷來源位址是否落在白名單網段內。"""
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return False
    # IPv4-mapped IPv6（::ffff:60.250.71.45）還原成 IPv4 再比對，
    # 否則雙棧環境下同一台用戶端會因為表示法不同而被誤擋。
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    for net in networks:
        if addr.version != net.version:
            continue
        if addr in net:
            return True
    return False


def client_ip(
    peer_ip: str | None,
    forwarded_for: str | None,
    trust_proxy: bool,
    trusted_proxies: Iterable[ipaddress._BaseNetwork] = (),
) -> str:
    """決定要用來比對白名單的來源位址。

    trust_proxy 為 False 時一律用 TCP 來源位址，忽略 X-Forwarded-For。
    為 True 時，若有指定 trusted_proxies，會先確認直連對象真的是已知代理，
    才採用 XFF 最左側的用戶端位址；否則仍以 TCP 來源為準。
    """
    peer = (peer_ip or "").strip()
    if not trust_proxy or not forwarded_for:
        return peer
    proxies = list(trusted_proxies)
    if proxies and not is_allowed(peer, proxies):
        # 直連對象不是已知代理，這個 XFF 不可信
        log.warning("收到 X-Forwarded-For 但來源 %s 不在信任代理清單，改用連線位址", peer)
        return peer
    # XFF 格式：client, proxy1, proxy2 —— 最左側才是原始用戶端
    first = forwarded_for.split(",")[0].strip()
    return first or peer


class IPAllowlistMiddleware:
    """ASGI middleware：不在白名單內的來源一律 403。

    刻意不用 FastAPI 的依賴注入實作，而是放在 ASGI 層，
    這樣連靜態頁面與尚未定義的路由都會被擋，不會有漏網的端點。
    """

    def __init__(
        self,
        app: Any,
        allowed: Iterable[str],
        trust_proxy: bool = False,
        trusted_proxies: Iterable[str] = (),
        exempt_paths: Iterable[str] = ("/healthz",),
    ) -> None:
        self.app = app
        self.networks = parse_networks(allowed)
        self.trust_proxy = trust_proxy
        self.proxy_networks = parse_networks(trusted_proxies)
        self.exempt_paths = set(exempt_paths)
        self._denied: dict[str, int] = {}
        log.info("IP 白名單啟用，允許 %d 個網段：%s",
                 len(self.networks), [str(n) for n in self.networks])

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in self.exempt_paths:
            await self.app(scope, receive, send)
            return

        peer = (scope.get("client") or ("", 0))[0]
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        ip = client_ip(peer, headers.get("x-forwarded-for"),
                       self.trust_proxy, self.proxy_networks)

        if is_allowed(ip, self.networks):
            await self.app(scope, receive, send)
            return

        # 記錄被拒次數，方便判斷是設定漏了某個出口 IP 還是真的有人在掃
        self._denied[ip] = self._denied.get(ip, 0) + 1
        if self._denied[ip] <= 5 or self._denied[ip] % 50 == 0:
            log.warning("拒絕來源 %s（累計 %d 次）存取 %s",
                        ip or "(未知)", self._denied[ip], path)

        body = (b'{"error":"forbidden",'
                b'"detail":"\\u4f86\\u6e90\\u4f4d\\u5740\\u4e0d\\u5728\\u5141\\u8a31'
                b'\\u540d\\u55ae\\u5185"}')
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [(b"content-type", b"application/json; charset=utf-8"),
                        (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})

    @property
    def denied_counts(self) -> dict[str, int]:
        return dict(self._denied)
