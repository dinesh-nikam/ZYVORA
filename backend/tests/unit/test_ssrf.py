"""SSRF protection: URL validation, DNS/IP validation, redirect re-validation and
the connect-time guarded network backend."""

from __future__ import annotations

import httpcore
import httpx
import pytest

from app.core.errors import OutboundBlocked
from app.integrations.http import (
    GuardedNetworkBackend,
    RedirectBlocked,
    SafeHttpClient,
    is_forbidden_ip,
    validate_url_syntax,
)
from tests.conftest import PUBLIC_IP, MockWeb, make_settings

ALLOW = frozenset({"t.me", "github.com"})


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.0.0.5", "172.16.3.4", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
    "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "::ffff:169.254.169.254", "64:ff9b::a9fe:a9fe",
    "2002:7f00:0001::1", "224.0.0.1", "255.255.255.255", "198.18.0.1", "192.0.2.1", "not-an-ip",
])
def test_forbidden_ips(ip: str) -> None:
    assert is_forbidden_ip(ip)


@pytest.mark.parametrize("ip", ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"])
def test_public_ips_allowed(ip: str) -> None:
    assert not is_forbidden_ip(ip)


@pytest.mark.parametrize(("url", "reason"), [
    ("http://t.me/acme", "scheme"),
    ("file:///etc/passwd", "scheme"),
    ("gopher://t.me/_", "scheme"),
    ("https://user:pass@t.me/acme", "userinfo"),
    ("https://t.me@127.0.0.1/acme", "userinfo"),
    ("https://127.0.0.1/", "ip_literal"),
    ("https://2130706433/", "ip_literal"),
    ("https://0177.0.0.1/", "invalid|ip_literal"),
    ("https://0x7f.0.0.1/", "ip_literal"),
    ("https://127.1/", "ip_literal"),
    ("https://[::1]/", "ip_literal"),
    ("https://localhost/", "internal_host"),
    ("https://metadata.google.internal/computeMetadata/v1/", "internal_host"),
    ("https://service.svc.cluster.local/", "internal_host"),
    ("https://intranet/", "internal_host"),
    ("https://t.me:8443/acme", "port"),
    ("https://evil.example.com/t.me/acme", "not_allowlisted"),
    ("https://t.me.evil.example.com/acme", "not_allowlisted"),
    ("https://t.me/a b", "control_chars"),
    ("https://t.me\\@evil.com/", "backslash"),
    ("https://t.me/" + "a" * 3000, "too_long"),
])
def test_url_syntax_rejections(url: str, reason: str) -> None:
    with pytest.raises(OutboundBlocked) as exc:
        validate_url_syntax(url, allowed_domains=ALLOW)
    assert exc.value.details[0]["reason"] in reason.split("|")


def test_subdomain_of_allowlisted_domain_ok() -> None:
    assert validate_url_syntax("https://gist.github.com/x", allowed_domains=ALLOW).host == "gist.github.com"


async def test_dns_resolving_to_private_ip_is_blocked(tmp_path) -> None:
    web = MockWeb()
    web.resolver_map["t.me"] = ["10.0.0.7"]
    client = SafeHttpClient(make_settings(tmp_path), resolver=web.resolve, transport=web.transport())
    with pytest.raises(OutboundBlocked) as exc:
        await client.fetch("https://t.me/acme", allowed_domains=ALLOW)
    assert exc.value.details[0]["reason"] == "private_ip"
    assert web.requests == []
    await client.aclose()


async def test_any_private_address_in_rrset_blocks(tmp_path) -> None:
    web = MockWeb()
    web.resolver_map["t.me"] = [PUBLIC_IP, "127.0.0.1"]
    client = SafeHttpClient(make_settings(tmp_path), resolver=web.resolve, transport=web.transport())
    with pytest.raises(OutboundBlocked):
        await client.fetch("https://t.me/acme", allowed_domains=ALLOW)
    await client.aclose()


async def test_redirect_to_internal_host_is_blocked(tmp_path) -> None:
    web = MockWeb()
    web.add("https://t.me/acme", httpx.Response(302, headers={"location": "https://169.254.169.254/latest/meta-data"}))
    client = SafeHttpClient(make_settings(tmp_path), resolver=web.resolve, transport=web.transport())
    with pytest.raises(RedirectBlocked) as exc:
        await client.fetch("https://t.me/acme", allowed_domains=ALLOW)
    assert exc.value.reason == "ip_literal"
    assert [r.url.host for r in web.requests] == ["t.me"]
    await client.aclose()


async def test_redirect_off_allowlist_is_blocked(tmp_path) -> None:
    web = MockWeb()
    web.add("https://t.me/acme", httpx.Response(301, headers={"location": "https://attacker.example/steal"}))
    client = SafeHttpClient(make_settings(tmp_path), resolver=web.resolve, transport=web.transport())
    with pytest.raises(RedirectBlocked) as exc:
        await client.fetch("https://t.me/acme", allowed_domains=ALLOW)
    assert exc.value.reason == "not_allowlisted"
    await client.aclose()


async def test_redirect_loop_is_bounded(tmp_path) -> None:
    web = MockWeb()
    web.add("https://t.me/a", httpx.Response(302, headers={"location": "/b"}))
    web.add("https://t.me/b", httpx.Response(302, headers={"location": "/a"}))
    client = SafeHttpClient(make_settings(tmp_path, outbound_max_redirects=3), resolver=web.resolve,
                            transport=web.transport())
    with pytest.raises(RedirectBlocked) as exc:
        await client.fetch("https://t.me/a", allowed_domains=ALLOW)
    assert exc.value.reason == "too_many_redirects"
    assert len(web.requests) == 4
    await client.aclose()


async def test_response_size_is_capped(tmp_path) -> None:
    web = MockWeb()
    web.add("https://t.me/big", httpx.Response(200, content=b"x" * 50_000))
    client = SafeHttpClient(make_settings(tmp_path, outbound_max_bytes=1000), resolver=web.resolve,
                            transport=web.transport())
    result = await client.fetch("https://t.me/big", allowed_domains=ALLOW)
    assert len(result.body) == 1000
    assert result.truncated
    await client.aclose()


async def test_guarded_backend_refuses_private_resolution_at_connect_time() -> None:
    async def rebinding_resolver(host: str, port: int) -> list[str]:
        return ["127.0.0.1"]

    backend = GuardedNetworkBackend(rebinding_resolver)
    with pytest.raises(httpcore.ConnectError, match="blocked by outbound policy"):
        await backend.connect_tcp("t.me", 443)


async def test_guarded_backend_refuses_unix_sockets() -> None:
    async def resolver(host: str, port: int) -> list[str]:
        return [PUBLIC_IP]

    with pytest.raises(httpcore.ConnectError):
        await GuardedNetworkBackend(resolver).connect_unix_socket("/var/run/docker.sock")


async def test_real_transport_blocks_loopback_end_to_end(tmp_path) -> None:
    """No mock transport: the guarded backend itself must stop the connection."""
    calls: list[str] = []

    async def resolver(host: str, port: int) -> list[str]:
        calls.append(host)
        # Pre-flight sees a public IP, connect-time sees loopback (DNS rebinding).
        return [PUBLIC_IP] if len(calls) == 1 else ["127.0.0.1"]

    client = SafeHttpClient(make_settings(tmp_path, outbound_timeout_seconds=2), resolver=resolver)
    with pytest.raises(httpx.ConnectError):
        await client.fetch("https://t.me/acme", allowed_domains=ALLOW)
    assert len(calls) >= 2
    await client.aclose()
