"""HLS 上游 curl 请求：公网地址钉死，代理路径拒绝主机名。"""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING, Any

from shared.network.pinned_transport import (
    canonicalize_request_target,
    curl_resolve_options as _pinned_curl_resolve_options,
)
from shared.runtime_options import PUBLIC_DOMAIN_POLICY, DomainPolicyViolation

if TYPE_CHECKING:
    from shared.runtime_options import DomainPolicyEngine


def curl_resolve_options(url: str, addresses: tuple[str, ...]) -> dict[Any, Any]:
    """把 curl 固定到已通过策略校验的公网地址，防止解析结果漂移。"""
    target = canonicalize_request_target(url)
    options = _pinned_curl_resolve_options(target, addresses, disable_proxy=True)
    from curl_cffi.const import CurlOpt

    options.update(
        {
            CurlOpt.NOSIGNAL: 1,
            CurlOpt.FRESH_CONNECT: 1,
            CurlOpt.FORBID_REUSE: 1,
        }
    )
    return options


def prepare_hls_curl_request(
    url: str,
    domain_policy: "DomainPolicyEngine | None",
    upstream_proxy: str | None,
) -> tuple[str, dict[Any, Any] | None]:
    target = canonicalize_request_target(url)
    request_url = target.url
    policy = domain_policy or PUBLIC_DOMAIN_POLICY
    if upstream_proxy:
        # HTTP 代理自己解析 CONNECT 里的主机名，CURLOPT_RESOLVE 钉不住这条连接。
        # 只有 URL 本身已是公网 IP 时才允许走代理；否则拒绝，不能校验后把主机名交出去。
        # 拒绝先于任何 DNS 解析，避免把主机名泄露给解析器。
        try:
            ipaddress.ip_address(target.host)
        except ValueError as exc:
            raise DomainPolicyViolation(
                "HLS 上游代理无法钉死 DNS，已拒绝把主机名交给代理解析"
            ) from exc
        # 公网校验对 IP 字面量同样逐跳执行。
        addresses = policy.resolve_public_addresses(request_url)
        pinned = _pinned_curl_resolve_options(target, addresses, disable_proxy=False)
        return request_url, pinned or None
    # 每一跳都解析。非公网地址在这里失败。
    addresses = policy.resolve_public_addresses(request_url)
    return request_url, curl_resolve_options(request_url, addresses)
