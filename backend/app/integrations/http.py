"""SSRF-hardened outbound HTTP client.

Every outbound request made on behalf of a user (profile URL checks, robots.txt,
search providers, platform APIs) goes through :class:`SafeHttpClient`.

Defences:

* URL validation: https only (http only where explicitly permitted), no
  userinfo, default ports only for untrusted URLs, no IP-literal hosts
  (including integer/octal/hex encodings), no internal hostnames.
* Domain allowlisting for user-influenced URLs.
* DNS validation: every resolved address must be globally routable; loopback,
  RFC1918, link-local (cloud metadata 169.254.169.254), CGNAT, ULA, multicast,
  reserved and IPv4-mapped/NAT64 encodings of those are refused.
* Anti DNS-rebinding: the network backend re-resolves and re-validates at
  socket-connect time and connects to the vetted IP (TLS SNI/cert still bound to
  the hostname).
* Manual redirect following with full re-validation of every hop.
* Response size cap, timeouts, no proxy environment variables.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import ssl
import time
from collections.abc import Awaitable, Callable, Collection, Iterable
from dataclasses import dataclass, field
from typing import Any

import certifi
import httpcore
import httpx

from app.core.config import Settings
from app.core.errors import OutboundBlocked
from app.core.metrics import OUTBOUND_BLOCKED, OUTBOUND_REQUESTS

logger = logging.getLogger(__name__)

Resolver = Callable[[str, int], Awaitable[list[str]]]

MAX_URL_LENGTH = 2048
_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "instance-data",
        "instance-data.ec2.internal",
        "kubernetes",
        "kubernetes.default",
        "kubernetes.default.svc",
        "host.docker.internal",
        "gateway.docker.internal",
    }
)
_BLOCKED_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".lan",
    ".home.arpa",
    ".corp",
    ".intranet",
    ".svc",
    ".cluster.local",
    ".localdomain",
    ".onion",
)
_EXTRA_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "100.64.0.0/10",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "255.255.255.255/32",
        "64:ff9b:1::/48",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
        "2001:db8::/32",
        "100::/64",
    )
)
_NUMERIC_HOST = re.compile(r"^(0x[0-9a-f]+|[0-9]+)(\.(0x[0-9a-f]+|[0-9]+)){0,3}$", re.IGNORECASE)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class RedirectBlocked(OutboundBlocked):
    def __init__(self, message: str, *, chain: list[RedirectHop], reason: str) -> None:
        super().__init__(message, code="redirect_blocked")
        self.chain = chain
        self.reason = reason


@dataclass(frozen=True, slots=True)
class RedirectHop:
    url: str
    status_code: int


@dataclass(slots=True)
class FetchResult:
    requested_url: str
    final_url: str
    status_code: int
    headers: dict[str, str]
    body: bytes
    truncated: bool
    content_type: str
    redirect_chain: list[RedirectHop] = field(default_factory=list)
    elapsed_ms: int = 0

    def text(self) -> str:
        charset = "utf-8"
        match = re.search(r"charset=([\w-]+)", self.content_type, re.IGNORECASE)
        if match:
            charset = match.group(1)
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


def _block(reason: str, message: str) -> OutboundBlocked:
    OUTBOUND_BLOCKED.labels(reason=reason).inc()
    return OutboundBlocked(message, details=[{"reason": reason}])


def is_forbidden_ip(value: str) -> bool:
    """True if ``value`` is not a globally routable unicast address."""
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_forbidden_ip(str(ip.ipv4_mapped))
        if ip.sixtofour is not None and is_forbidden_ip(str(ip.sixtofour)):
            return True
        if ip.teredo is not None:
            return True
        if ip in _NAT64:
            return is_forbidden_ip(str(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)))
    if (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return True
    return any(ip in net for net in _EXTRA_BLOCKED_NETWORKS if net.version == ip.version)


def _is_ip_literal(host: str) -> bool:
    candidate = host.strip("[]")
    try:
        ipaddress.ip_address(candidate)
        return True
    except ValueError:
        pass
    if _NUMERIC_HOST.match(candidate):
        return True  # integer / octal / hex / short-dotted IPv4 encodings
    try:
        socket.inet_aton(candidate)
        return True
    except OSError:
        return False


def domain_matches(host: str, allowed: Iterable[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in allowed)


@dataclass(frozen=True, slots=True)
class ValidatedUrl:
    url: httpx.URL
    host: str
    port: int


def validate_url_syntax(
    raw: str,
    *,
    allowed_domains: Collection[str] | None,
    allow_http: bool = False,
    allow_custom_port: bool = False,
    allow_ip_literal: bool = False,
) -> ValidatedUrl:
    """Validate an outbound URL without any network access."""
    if not isinstance(raw, str) or not raw.strip():
        raise _block("empty", "URL is empty")
    raw = raw.strip()
    if len(raw) > MAX_URL_LENGTH:
        raise _block("too_long", "URL is too long")
    if any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in raw):
        raise _block("control_chars", "URL contains whitespace or control characters")
    if "\\" in raw:
        raise _block("backslash", "URL contains backslashes")
    try:
        url = httpx.URL(raw)
    except (httpx.InvalidURL, ValueError) as exc:
        raise _block("invalid", "URL could not be parsed") from exc
    schemes = {"https", "http"} if allow_http else {"https"}
    if url.scheme not in schemes:
        raise _block("scheme", f"URL scheme must be one of: {', '.join(sorted(schemes))}")
    if url.userinfo:
        raise _block("userinfo", "URLs with embedded credentials are not allowed")
    try:
        host = url.raw_host.decode("ascii").lower().rstrip(".")
    except UnicodeDecodeError as exc:
        raise _block("host_encoding", "URL host is not valid") from exc
    if not host:
        raise _block("no_host", "URL has no host")
    default_port = 443 if url.scheme == "https" else 80
    port = url.port or default_port
    if not allow_custom_port and port != default_port:
        raise _block("port", "Only default HTTP(S) ports are allowed")
    if _is_ip_literal(host):
        if not allow_ip_literal:
            raise _block("ip_literal", "IP-address hosts are not allowed")
        if is_forbidden_ip(host.strip("[]")):
            raise _block("private_ip", "URL targets a non-public address")
    elif host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_SUFFIXES) or "." not in host:
        raise _block("internal_host", "URL targets an internal host name")
    if allowed_domains is not None and not domain_matches(host, allowed_domains):
        raise _block("not_allowlisted", "URL domain is not in the allowlist")
    return ValidatedUrl(url=url, host=host, port=port)


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


async def resolve_public(host: str, port: int, resolver: Resolver, *, allow_private: bool = False) -> list[str]:
    if _is_ip_literal(host):
        addresses = [host.strip("[]")]
    else:
        try:
            addresses = await resolver(host, port)
        except (OSError, socket.gaierror) as exc:
            raise _block("dns_failure", "Host name could not be resolved") from exc
    if not addresses:
        raise _block("dns_empty", "Host name did not resolve")
    if not allow_private:
        for address in addresses:
            if is_forbidden_ip(address):
                raise _block("private_ip", "Host resolves to a non-public address")
    return addresses


class GuardedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolves + validates at connect time and connects to the vetted IP."""

    def __init__(self, resolver: Resolver, allow_private_hosts: Collection[str] = ()) -> None:
        self._resolver = resolver
        self._allow_private = frozenset(h.lower() for h in allow_private_hosts)
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        host_l = host.lower().rstrip(".")
        try:
            addresses = await resolve_public(host_l, port, self._resolver, allow_private=host_l in self._allow_private)
        except OutboundBlocked as exc:
            raise httpcore.ConnectError(f"blocked by outbound policy: {exc.message}") from exc
        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address, port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as exc:
                last_error = exc
        raise httpcore.ConnectError(f"could not connect to {host_l}") from last_error

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Iterable[Any] | None = None
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("unix sockets are not permitted")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class GuardedTransport(httpx.AsyncHTTPTransport):
    """httpx transport whose connection pool uses :class:`GuardedNetworkBackend`."""

    def __init__(self, backend: GuardedNetworkBackend, *, max_connections: int = 50) -> None:
        super().__init__(retries=0)
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        # Replace the default pool with one bound to the guarded network backend.
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            max_connections=max_connections,
            max_keepalive_connections=10,
            keepalive_expiry=30.0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=backend,
        )


class SafeHttpClient:
    def __init__(
        self,
        settings: Settings,
        *,
        resolver: Resolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        allow_private_hosts: Collection[str] = (),
    ) -> None:
        self._resolver = resolver or system_resolver
        self._allow_private = frozenset(h.lower() for h in allow_private_hosts)
        self._max_bytes = settings.outbound_max_bytes
        self._max_redirects = settings.outbound_max_redirects
        backend = GuardedNetworkBackend(self._resolver, self._allow_private)
        self._client = httpx.AsyncClient(
            transport=transport or GuardedTransport(backend),
            timeout=httpx.Timeout(
                settings.outbound_timeout_seconds,
                connect=min(5.0, settings.outbound_timeout_seconds),
            ),
            follow_redirects=False,
            trust_env=False,  # never route through env proxies (would bypass IP validation)
            headers={"User-Agent": settings.outbound_user_agent, "Accept-Language": "en"},
        )
        self.user_agent = settings.outbound_user_agent

    async def aclose(self) -> None:
        await self._client.aclose()

    async def preflight(
        self,
        url: str,
        *,
        allowed_domains: Collection[str] | None,
        allow_http: bool = False,
        trusted: bool = False,
    ) -> ValidatedUrl:
        validated = validate_url_syntax(
            url,
            allowed_domains=allowed_domains,
            allow_http=allow_http,
            allow_custom_port=trusted,
            allow_ip_literal=trusted,
        )
        await resolve_public(
            validated.host, validated.port, self._resolver, allow_private=validated.host in self._allow_private
        )
        return validated

    async def fetch(
        self,
        url: str,
        *,
        allowed_domains: Collection[str] | None,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        auth: httpx.Auth | tuple[str, str] | None = None,
        max_redirects: int | None = None,
        max_bytes: int | None = None,
        allow_http: bool = False,
        trusted: bool = False,
        target_label: str = "profile",
    ) -> FetchResult:
        """Perform a validated request, following redirects manually."""
        limit = self._max_bytes if max_bytes is None else max_bytes
        redirects_left = self._max_redirects if max_redirects is None else max_redirects
        chain: list[RedirectHop] = []
        current = (
            await self.preflight(url, allowed_domains=allowed_domains, allow_http=allow_http, trusted=trusted)
        ).url
        started = time.monotonic()
        while True:
            request = self._client.build_request(method, current, headers=headers, params=params, data=data)
            try:
                response = await self._client.send(request, stream=True, auth=auth)
            except httpx.HTTPError:
                OUTBOUND_REQUESTS.labels(target=target_label, outcome="network_error").inc()
                raise
            try:
                if response.is_redirect and "location" in response.headers:
                    chain.append(RedirectHop(url=str(current), status_code=response.status_code))
                    if redirects_left <= 0:
                        raise RedirectBlocked("Too many redirects", chain=chain, reason="too_many_redirects")
                    redirects_left -= 1
                    target = current.join(response.headers["location"])
                    try:
                        current = (
                            await self.preflight(
                                str(target), allowed_domains=allowed_domains, allow_http=allow_http, trusted=trusted
                            )
                        ).url
                    except OutboundBlocked as exc:
                        reason = exc.details[0]["reason"] if exc.details else "blocked"
                        chain.append(RedirectHop(url=str(target), status_code=0))
                        raise RedirectBlocked(f"Redirect refused: {exc.message}", chain=chain, reason=reason) from exc
                    # 303 (and 301/302 for POST) switch to GET per RFC 9110 practice.
                    if response.status_code == 303 or (response.status_code in (301, 302) and method == "POST"):
                        method, data = "GET", None
                    params = None
                    continue
                body, truncated = await _read_capped(response, limit)
                OUTBOUND_REQUESTS.labels(target=target_label, outcome=str(response.status_code // 100) + "xx").inc()
                return FetchResult(
                    requested_url=url,
                    final_url=str(current),
                    status_code=response.status_code,
                    headers={k.lower(): v for k, v in response.headers.items()},
                    body=body,
                    truncated=truncated,
                    content_type=response.headers.get("content-type", ""),
                    redirect_chain=chain,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            finally:
                await response.aclose()


async def _read_capped(response: httpx.Response, limit: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        remaining = limit - size
        if remaining <= 0:
            return b"".join(chunks), True
        chunks.append(chunk[:remaining])
        size += min(len(chunk), remaining)
        if len(chunk) > remaining:
            return b"".join(chunks), True
    return b"".join(chunks), False
