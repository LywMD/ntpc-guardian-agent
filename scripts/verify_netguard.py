"""驗證來源 IP 白名單：四個指定位址要通、其他要擋、且不可被標頭偽造繞過。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from guardian import netguard  # noqa: E402

OUT = ROOT / "data" / "verify_netguard.txt"
buf: list[str] = []
problems: list[str] = []

ALLOWED = ["127.0.0.1", "::1",
           "60.250.71.45", "61.222.117.53", "59.125.121.41", "60.250.71.43"]
NETS = netguard.parse_networks(ALLOWED)


def w(s: str = "") -> None:
    buf.append(s)


def check(label: str, ok: bool, detail: str = "") -> None:
    w(f"  [{'OK  ' if ok else 'FAIL'}] {label}{('　' + detail) if detail else ''}")
    if not ok:
        problems.append(label)


w("=" * 72)
w("一、指定的四個位址必須通")
w("=" * 72)
for ip in ("60.250.71.45", "61.222.117.53", "59.125.121.41", "60.250.71.43"):
    check(f"{ip} 允許", netguard.is_allowed(ip, NETS))

w()
w("=" * 72)
w("二、本機必須通（本機示範與健康檢查）")
w("=" * 72)
check("127.0.0.1 允許", netguard.is_allowed("127.0.0.1", NETS))
check("::1 允許", netguard.is_allowed("::1", NETS))
check("IPv4-mapped IPv6 ::ffff:60.250.71.45 允許",
      netguard.is_allowed("::ffff:60.250.71.45", NETS))

w()
w("=" * 72)
w("三、鄰近位址必須擋（確認是 /32 而不是整段開放）")
w("=" * 72)
for ip in ("60.250.71.44", "60.250.71.46", "60.250.71.1",
           "61.222.117.52", "59.125.121.42"):
    check(f"{ip} 拒絕", not netguard.is_allowed(ip, NETS))

w()
w("=" * 72)
w("四、其他來源必須擋")
w("=" * 72)
for ip in ("8.8.8.8", "1.1.1.1", "192.168.1.10", "10.0.0.5",
           "203.0.113.7", "", "not-an-ip"):
    check(f"{ip or '(空值)'} 拒絕", not netguard.is_allowed(ip, NETS))

w()
w("=" * 72)
w("五、X-Forwarded-For 不可用來繞過白名單")
w("=" * 72)
# 直連情境（trust_proxy=False）：偽造標頭必須無效
ip = netguard.client_ip("8.8.8.8", "60.250.71.45", trust_proxy=False)
check("直連時忽略偽造的 XFF", ip == "8.8.8.8", f"取得 {ip}")
check("→ 該來源仍被拒絕", not netguard.is_allowed(ip, NETS))

# 代理情境但未設定信任代理清單：採用 XFF 最左側
ip = netguard.client_ip("10.0.1.20", "60.250.71.45, 10.0.1.20",
                        trust_proxy=True)
check("代理模式取 XFF 最左側", ip == "60.250.71.45", f"取得 {ip}")
check("→ 該來源被允許", netguard.is_allowed(ip, NETS))

# 代理情境且指定信任代理：直連對象不是已知代理時，不可信任 XFF
proxies = netguard.parse_networks(["10.0.1.0/24"])
ip = netguard.client_ip("8.8.8.8", "60.250.71.45", trust_proxy=True,
                        trusted_proxies=proxies)
check("非信任代理送來的 XFF 被忽略", ip == "8.8.8.8", f"取得 {ip}")
check("→ 該來源仍被拒絕", not netguard.is_allowed(ip, NETS))

ip = netguard.client_ip("10.0.1.20", "60.250.71.43, 10.0.1.20",
                        trust_proxy=True, trusted_proxies=proxies)
check("信任代理送來的 XFF 被採用", ip == "60.250.71.43", f"取得 {ip}")

w()
w("=" * 72)
w("六、CIDR 網段設定可用")
w("=" * 72)
cidr_nets = netguard.parse_networks(["203.0.113.0/24"])
check("203.0.113.7 落在 /24 內", netguard.is_allowed("203.0.113.7", cidr_nets))
check("203.0.114.7 不在 /24 內", not netguard.is_allowed("203.0.114.7", cidr_nets))
bad = netguard.parse_networks(["這不是IP", "999.999.1.1", ""])
check("無效項目被安全略過而不拋錯", bad == [], str(bad))

w()
w("=" * 72)
w("七、Security Group 規則產出")
w("=" * 72)
try:
    sys.path.insert(0, str(ROOT / "infra"))
    import network_access  # noqa: E402

    srcs = network_access.allowlist()
    w(f"  來源清單 = {srcs}")
    check("四個位址都轉成 /32", sorted(srcs) == sorted([
        "60.250.71.45/32", "61.222.117.53/32",
        "59.125.121.41/32", "60.250.71.43/32"]), str(srcs))
    check("本機位址不寫進 Security Group",
          not any(s.startswith("127.") or s.startswith("::1") for s in srcs))

    perms = network_access.build_sg_permissions(srcs, [443])
    w(f"  IpPermissions = {perms}")
    check("產出 tcp/443 規則",
          bool(perms) and perms[0]["IpProtocol"] == "tcp"
          and perms[0]["FromPort"] == 443
          and len(perms[0].get("IpRanges", [])) == 4)

    pol = network_access.api_gateway_policy(srcs)
    stmts = pol["Statement"]
    check("API Gateway policy 有 Deny NotIpAddress 條款",
          any(s["Effect"] == "Deny"
              and "NotIpAddress" in s.get("Condition", {}) for s in stmts))
except Exception as exc:  # noqa: BLE001
    import traceback
    w("  !! infra/network_access.py 載入或執行失敗")
    w(traceback.format_exc())
    problems.append(f"network_access 異常：{type(exc).__name__}")

w()
w("=" * 72)
w(f"結論：{'全部通過' if not problems else f'{len(problems)} 項未通過'}")
for p in problems:
    w(f"  - {p}")
w("=" * 72)

OUT.write_text("\n".join(buf), encoding="utf-8")
raise SystemExit(1 if problems else 0)
