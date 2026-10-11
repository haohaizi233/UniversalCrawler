"""Updater adapters over the shared DNS-pinned public transport."""

from __future__ import annotations

import io
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from email.message import Message
from shared.network.pinned_transport import PinnedResponse, PinnedTransport
from shared.runtime_options import PUBLIC_DOMAIN_POLICY, DomainPolicyViolation

UrlValidator = Callable[..., None]


class AllowedUpdaterPublicPolicy:
    """Apply the updater host allowlist before resolving every redirect hop."""

    def __init__(
        self,
        *,
        allowed_hosts: set[str] | frozenset[str],
        validate_url: UrlValidator,
    ) -> None:
        self._allowed_hosts = frozenset(str(host).lower() for host in allowed_hosts)
        self._validate_url = validate_url

    def resolve_public_addresses(self, url: str) -> tuple[str, ...]:
        self._validate_url(url, self._allowed_hosts)
        return PUBLIC_DOMAIN_POLICY.resolve_public_addresses(url)


class PinnedUrlResponse:
    """Small stateful urllib-compatible view over one bounded pinned response."""

    def __init__(self, response: PinnedResponse) -> None:
        self._response = response
        self._stream = io.BytesIO(response.body)
        self.headers = response.headers
        self.status = response.status_code
        self.code = response.status_code

    def __enter__(self) -> "PinnedUrlResponse":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> bool:
        self.close()
        return False

    def geturl(self) -> str:
        return self._response.url

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def close(self) -> None:
        self._stream.close()


def build_updater_transport(
    *,
    timeout: float,
    max_response_bytes: int,
    allowed_hosts: set[str] | frozenset[str],
    validate_url: UrlValidator,
    transport_factory: Callable[..., PinnedTransport] = PinnedTransport,
) -> PinnedTransport:
    return transport_factory(
        policy=AllowedUpdaterPublicPolicy(
            allowed_hosts=allowed_hosts,
            validate_url=validate_url,
        ),
        timeout=timeout,
        max_response_bytes=max_response_bytes,
    )


def open_pinned_updater_url(
    request: urllib.request.Request,
    *,
    timeout: float,
    max_response_bytes: int,
    allowed_hosts: set[str] | frozenset[str],
    validate_url: UrlValidator,
    transport_factory: Callable[..., PinnedTransport] = PinnedTransport,
) -> PinnedUrlResponse:
    validate_url(request.full_url, allowed_hosts)
    try:
        transport = build_updater_transport(
            timeout=timeout,
            max_response_bytes=max_response_bytes,
            allowed_hosts=allowed_hosts,
            validate_url=validate_url,
            transport_factory=transport_factory,
        )
        headers = _request_headers(request)
        response = transport.request(
            request.get_method(),
            request.full_url,
            headers=headers,
            body=request.data,
            max_redirects=5,
        )
    except DomainPolicyViolation as exc:
        raise urllib.error.URLError("trusted updater transport rejected request") from exc
    except urllib.error.URLError:
        raise
    except Exception as exc:
        raise urllib.error.URLError("trusted updater transport failed") from exc
    if response.status_code < 200 or response.status_code >= 300:
        raise urllib.error.HTTPError(
            response.url,
            response.status_code,
            f"HTTP {response.status_code}",
            _message_headers(response.headers),
            None,
        )
    return PinnedUrlResponse(response)


def _request_headers(request: urllib.request.Request) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in request.header_items():
        if type(key) is str and type(value) is str:
            headers[key] = value
    return headers


def _message_headers(headers: Mapping[str, str]) -> Message:
    message = Message()
    for key, value in headers.items():
        if type(key) is str and type(value) is str:
            message[key] = value
    return message
