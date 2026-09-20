"""Outbound URL allowlist — SSRF guard for dynamic server-side egress URLs.

The two dynamic egress paths in bd-pay are:

* merchant webhook delivery (:mod:`bdpay.gateway.webhooks_out`)
* sanctions feed manual redirect following (:mod:`bdpay.connectors.identity.sanctions`)

Operator-fixed connector transports (bkash/nagad/rocket/sms/goaml/porichoy/ocr/
veridyn/vault/rtgs/card) are config-bound, already timeouted, and already use
httpx's default ``follow_redirects=False``; they are NOT required to call this
routine, but may do so safely.

Resolve-then-connect pinning
----------------------------

Validating a hostname's DNS answer and then letting the HTTP client re-resolve
it at connect time leaves a rebinding gap: the records can change between the
two resolutions. :func:`pinned_request` closes it — the request is issued
against the exact validated address (URL host rewritten to the IP), while the
original hostname is preserved in the ``Host`` header and in TLS via the httpx
``sni_hostname`` request extension, so SNI and certificate verification still
target the hostname. All dynamic egress call sites MUST send through
:func:`pinned_request` (enforced by ``tests/test_egress_wiring.py``).

Resolution bounds
-----------------

:func:`resolve_outbound_url_async` runs ``socket.getaddrinfo`` off the event
loop (executor thread) under :data:`RESOLUTION_TIMEOUT_SECONDS`; a resolver
that stalls past the bound is a validation FAILURE (fail-closed). The worker
thread itself cannot be cancelled mid-``getaddrinfo`` — it is abandoned — but
the event loop is never blocked. The sync path (:func:`validate_outbound_url`
/ :func:`resolve_outbound_url`) has no per-call bound because the OS API does
not expose one; sync callers inherit the platform resolver timeout and MUST
NOT be called from async code.
"""

from __future__ import annotations

import asyncio
import functools
import ipaddress
import socket
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bdpay.platform.errors import InvalidRequestError

if TYPE_CHECKING:
    import httpx

__all__ = [
    "RESOLUTION_TIMEOUT_SECONDS",
    "OutboundUrlNotAllowedError",
    "PinnedOutboundUrl",
    "pinned_request",
    "resolve_outbound_url",
    "resolve_outbound_url_async",
    "validate_outbound_url",
    "validate_outbound_url_async",
]


class OutboundUrlNotAllowedError(InvalidRequestError):
    """A requested outbound URL failed the SSRF allowlist."""

    default_code = "outbound_url_not_allowed"


#: Upper bound on one DNS resolution in the async path. Aligned with the
#: spec/01 webhook connect timeout (10s) — the tightest connect bound among
#: the dynamic egress callers.
RESOLUTION_TIMEOUT_SECONDS = 10.0

#: IPv4 Carrier-Grade NAT (RFC 6598) — not globally reachable.
_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")
#: IPv4 "this network" / all-zeros — not a valid egress target.
_ZEROS_V4 = ipaddress.ip_network("0.0.0.0/8")


def _is_global_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return ``True`` only for globally routable, non-special unicast addresses.

    Rejects private (RFC 1918 / unique-local), loopback, link-local, multicast,
    reserved, CGNAT, and the all-zeros IPv4 block.
    """
    if ip.is_private:
        return False
    if ip.is_loopback:
        return False
    if ip.is_link_local:
        return False
    if ip.is_multicast:
        return False
    if ip.is_reserved:
        return False
    if isinstance(ip, ipaddress.IPv4Address):
        if ip in _CGNAT_V4:
            return False
        if ip in _ZEROS_V4:
            return False
    return True


@dataclass(frozen=True)
class PinnedOutboundUrl:
    """A validated outbound target with its connect address pinned.

    ``pinned_url`` is ``url`` with the host component rewritten to
    ``address`` (the validated IP; bracketed when IPv6). For raw-IP-literal
    URLs the two are identical and no pinning metadata applies.
    """

    url: str  #: the original, validated URL
    pinned_url: str  #: the URL to actually connect to (host = validated IP)
    hostname: str  #: original hostname (lowercased, no trailing dot) or IP literal
    address: str  #: the validated IP every connection must target
    explicit_port: int | None  #: port as written in the URL, if any
    is_ip_literal: bool  #: True when the original URL carried a raw IP

    @property
    def host_header(self) -> str:
        """The ``Host`` header value preserving the original authority."""
        if self.explicit_port is not None:
            return f"{self.hostname}:{self.explicit_port}"
        return self.hostname

    def request_extensions(self) -> dict[str, Any]:
        """httpx request extensions keeping TLS bound to the hostname."""
        if self.is_ip_literal:
            return {}
        return {"sni_hostname": self.hostname}


def _split_checked(url: str, *, require_https: bool) -> tuple[Any, str, int | None]:
    """Scheme/host/userinfo/port checks shared by every entry point."""
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port  # raises ValueError on a malformed port
    except ValueError as exc:
        raise OutboundUrlNotAllowedError("outbound URL is malformed") from exc
    if require_https and parsed.scheme.lower() != "https":
        raise OutboundUrlNotAllowedError("outbound URL must use HTTPS")
    if not parsed.netloc or not hostname:
        raise OutboundUrlNotAllowedError("outbound URL must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise OutboundUrlNotAllowedError("outbound URL must not include userinfo")
    host = hostname.rstrip(".").lower()
    if not host.isascii():
        # Non-ASCII hosts are refused rather than IDNA-encoded here: the
        # exact bytes used for resolution, Host header, and TLS SNI must be
        # identical, and callers can always supply the xn-- form directly.
        raise OutboundUrlNotAllowedError(
            "outbound URL host must be ASCII (use the IDNA/punycode form)"
        )
    return parsed, host, port


def _ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None  # hostname (incl. decimal/octal forms — resolved + validated)


def _validated_addresses(infos: list) -> list[str]:
    """Validate EVERY resolved address; fail-closed if any is disallowed."""
    checked: list[str] = []
    for info in infos:
        raw_address = info[4][0]
        if not isinstance(raw_address, str):
            continue
        # Scope IDs (e.g. fe80::1%eth0) are link-local-only and cannot be
        # parsed by ipaddress; strip them before parsing.
        address = raw_address.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if not _is_global_public_ip(ip):
            raise OutboundUrlNotAllowedError(
                "outbound URL resolved to a disallowed IP"
            )
        checked.append(str(ip))
    if not checked:
        raise OutboundUrlNotAllowedError(
            "outbound URL host resolved to no usable address"
        )
    return checked


def _build_pinned(
    url: str, parsed: Any, host: str, port: int | None, address: str
) -> PinnedOutboundUrl:
    from urllib.parse import urlunsplit

    ip = ipaddress.ip_address(address)
    netloc = f"[{address}]" if ip.version == 6 else address
    if port is not None:
        netloc += f":{port}"
    pinned = urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )
    return PinnedOutboundUrl(
        url=url,
        pinned_url=pinned,
        hostname=host,
        address=address,
        explicit_port=port,
        is_ip_literal=False,
    )


def _literal_pinned(
    url: str, host: str, port: int | None, ip: ipaddress.IPv4Address | ipaddress.IPv6Address
) -> PinnedOutboundUrl:
    if not _is_global_public_ip(ip):
        raise OutboundUrlNotAllowedError("outbound URL target IP is not allowed")
    return PinnedOutboundUrl(
        url=url,
        pinned_url=url,
        hostname=host,
        address=str(ip),
        explicit_port=port,
        is_ip_literal=True,
    )


def resolve_outbound_url(url: str, *, require_https: bool = True) -> PinnedOutboundUrl:
    """Validate ``url`` and pin its connect address (sync callers only).

    Same policy as :func:`resolve_outbound_url_async` but resolves on the
    calling thread with the platform resolver timeout (see module docstring).
    """
    parsed, host, port = _split_checked(url, require_https=require_https)
    ip = _ip_literal(host)
    if ip is not None:
        return _literal_pinned(url, host, port, ip)
    try:
        infos = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        # UnicodeError: getaddrinfo's internal IDNA encoding can reject a
        # host with a ValueError subclass that would otherwise escape the
        # fail-closed contract.
        raise OutboundUrlNotAllowedError(
            "outbound URL host could not be resolved"
        ) from exc
    addresses = _validated_addresses(infos)
    return _build_pinned(url, parsed, host, port, addresses[0])


async def resolve_outbound_url_async(
    url: str,
    *,
    require_https: bool = True,
    resolution_timeout: float = RESOLUTION_TIMEOUT_SECONDS,
) -> PinnedOutboundUrl:
    """Validate ``url`` and pin its connect address without blocking the loop.

    ``socket.getaddrinfo`` runs in an executor thread bounded by
    ``resolution_timeout``; a resolver stall past the bound raises
    :class:`OutboundUrlNotAllowedError` (fail-closed).
    """
    parsed, host, port = _split_checked(url, require_https=require_https)
    ip = _ip_literal(host)
    if ip is not None:
        return _literal_pinned(url, host, port, ip)
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                functools.partial(
                    socket.getaddrinfo, host, port or 443, type=socket.SOCK_STREAM
                ),
            ),
            timeout=resolution_timeout,
        )
    except TimeoutError as exc:
        raise OutboundUrlNotAllowedError(
            "outbound URL resolution timed out"
        ) from exc
    except (OSError, UnicodeError) as exc:
        raise OutboundUrlNotAllowedError(
            "outbound URL host could not be resolved"
        ) from exc
    addresses = _validated_addresses(infos)
    return _build_pinned(url, parsed, host, port, addresses[0])


def validate_outbound_url(url: str, *, require_https: bool = True) -> str:
    """Validate that ``url`` is safe for server-side egress (sync callers only).

    Returns the original URL on success. Raises :class:`OutboundUrlNotAllowedError`
    (an :class:`~bdpay.platform.errors.InvalidRequestError`) on any unsafe or
    unresolvable destination. DNS failures are treated as BLOCK (fail-closed).

    Checks performed:

    * scheme must be ``https`` when ``require_https=True``;
    * host must be present; userinfo is refused;
    * raw-IP literals must be globally routable (no private/loopback/link-local
      /multicast/reserved/CGNAT/0.0.0.0/::1);
    * hostnames are resolved (A/AAAA); if ANY resolved address is non-public the
      URL is rejected;
    * DNS resolution failure raises (fail-closed).

    NOTE: this validates only — it does not pin the address through the
    connect. Real egress must go through :func:`pinned_request`.
    """
    return resolve_outbound_url(url, require_https=require_https).url


async def validate_outbound_url_async(
    url: str,
    *,
    require_https: bool = True,
    resolution_timeout: float = RESOLUTION_TIMEOUT_SECONDS,
) -> str:
    """Async, loop-safe variant of :func:`validate_outbound_url`."""
    pinned = await resolve_outbound_url_async(
        url, require_https=require_https, resolution_timeout=resolution_timeout
    )
    return pinned.url


async def pinned_request(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    content: bytes | None = None,
    timeout: Any | None = None,
    client: httpx.AsyncClient | None = None,
    require_https: bool = True,
    resolution_timeout: float = RESOLUTION_TIMEOUT_SECONDS,
) -> httpx.Response:
    """Validate, pin, and send — the ONE egress path for dynamic URLs.

    Resolves and validates ``url`` (bounded, off-loop), then issues the
    request against the validated IP with the original hostname kept in the
    ``Host`` header and in TLS (SNI + certificate verification) via the httpx
    ``sni_hostname`` request extension. There is no second resolution, so a
    DNS answer that changes after validation cannot re-target the connection.

    ``client`` lets callers with a long-lived connection pool (or custom TLS
    trust in tests) reuse it; when omitted a one-shot client is created with
    ``trust_env=False`` (no ambient proxy pickup) and redirects disabled.
    Any caller-supplied ``Host`` header is overwritten with the validated
    authority.
    """
    import httpx

    pinned = await resolve_outbound_url_async(
        url, require_https=require_https, resolution_timeout=resolution_timeout
    )
    request_headers = {
        key: value
        for key, value in dict(headers or {}).items()
        if key.lower() != "host"
    }
    if not pinned.is_ip_literal:
        request_headers["Host"] = pinned.host_header
    request_kwargs: dict[str, Any] = {
        "headers": request_headers,
        "extensions": pinned.request_extensions(),
    }
    if content is not None:
        request_kwargs["content"] = content
    if timeout is not None:
        request_kwargs["timeout"] = timeout
    if client is not None:
        return await client.request(method, pinned.pinned_url, **request_kwargs)
    async with httpx.AsyncClient(
        trust_env=False, follow_redirects=False
    ) as own_client:
        return await own_client.request(method, pinned.pinned_url, **request_kwargs)
