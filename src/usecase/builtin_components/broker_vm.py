import base64
import binascii
import logging
from typing import Annotated, Literal, Optional

from fastmcp import Context, FastMCP
from pydantic import Field

from config.config import get_config
from pkg.broker_client import BrokerError, get_broker_client, use_new_admin_password
from pkg.util import create_response
from usecase.base_module import BaseModule

logger = logging.getLogger(__name__)

_MAX_PEM_CHARS = 1_500_000
_LOG_TIMEOUT_SECONDS = 300


def _error(message: str) -> str:
    return create_response(data={"error": message}, is_error=True)


def _non_empty(value: str, label: str) -> str | None:
    if not value or not value.strip():
        return f"{label} is empty."
    return None


def _pem_error(value: str, label: str) -> str | None:
    empty = _non_empty(value, label)
    if empty:
        return empty
    if len(value) > _MAX_PEM_CHARS:
        return f"{label} exceeds {_MAX_PEM_CHARS} characters."
    try:
        base64.b64decode("".join(value.split()), validate=True)
    except (binascii.Error, ValueError):
        return f"{label} must be base64-encoded PEM."
    return None


async def _call(action):
    try:
        return create_response(data=await action())
    except BrokerError as exc:
        logger.exception("Broker VM call failed")
        return _error(str(exc))


async def post_auth_reset_initial_password(
    ctx: Context,
    new_password: Annotated[
        str,
        Field(
            description="New admin password. The factory password stays on the server and is sent as current_password."
        ),
    ],
) -> str:
    """
    Side effects: this operation changes the Broker VM appliance (POST /public_api/v1/auth/reset-initial-password).
    It replaces the factory-default admin password. The call is not idempotent. No bearer is sent.
    Confirm the new password before calling.
    """
    if error := _non_empty(new_password, "New password"):
        return _error(error)
    try:
        client = get_broker_client()
    except BrokerError as exc:
        return _error(str(exc))
    if new_password == client.password:
        return _error("New password must differ from the factory password stored on the server.")

    async def action():
        payload = await client.request(
            "/public_api/v1/auth/reset-initial-password",
            {"current_password": client.password, "new_password": new_password},
            timeout=60,
        )
        use_new_admin_password(client, new_password)
        return {
            "reply": payload.get("reply") if isinstance(payload, dict) else None,
            "password_updated_for_this_process": True,
        }

    return await _call(action)


async def post_auth_token(ctx: Context) -> str:
    """
    Side effects: this operation changes the Broker VM appliance (POST /public_api/v1/auth/token).
    It exchanges the server's factory password for a 10-minute bearer. The token stays on the server
    and is not returned. Confirm before calling.
    """

    async def action():
        client = get_broker_client()
        await client.login()
        return {"reply": {"token_issued": True}}

    return await _call(action)


async def post_logs(ctx: Context) -> str:
    """
    Side effects: this operation changes the Broker VM appliance (POST /public_api/v1/logs).
    It collects the on-appliance log bundle. The server logs in with the factory password first.
    Confirm before calling.
    """

    async def action():
        body = await get_broker_client().authorized("/public_api/v1/logs", raw=True, timeout=_LOG_TIMEOUT_SECONDS)
        if not isinstance(body, (bytes, bytearray)):
            raise BrokerError("Broker log bundle was not a byte payload.")
        return {"reply": {"content_base64": base64.b64encode(bytes(body)).decode("ascii"), "size": len(body)}}

    return await _call(action)


async def post_network_interface(
    ctx: Context,
    name: Annotated[str, Field(description="Interface name, such as eth0.")],
    interface_type: Annotated[
        Literal["", "dhcp", "static"],
        Field(
            description="Empty string disables the interface. dhcp enables DHCP. static requires address and netmask."
        ),
    ],
    address: Annotated[
        str, Field(description="IPv4 address. Required when interface_type is static.", default="")
    ] = "",
    netmask: Annotated[
        str, Field(description="IPv4 netmask. Required when interface_type is static.", default="")
    ] = "",
    gateway: Annotated[str, Field(description="Optional default gateway.", default="")] = "",
    dns: Annotated[
        Optional[list[str]],
        Field(description="DNS servers, at most 8.", default=None),
    ] = None,
    is_admin: Annotated[
        bool, Field(description="Whether this interface serves the broker admin UI.", default=False)
    ] = False,
) -> str:
    """
    Side effects: this operation changes the Broker VM appliance (POST /public_api/v1/network/interface).
    A bad configuration can make the VM unreachable. Confirm the interface before calling.
    """
    if error := _non_empty(name, "Interface name"):
        return _error(error)
    if len(name) > 64:
        return _error("Interface name exceeds 64 characters.")
    if interface_type == "static" and (not address.strip() or not netmask.strip()):
        return _error("Static interface configuration requires address and netmask.")
    servers = dns or []
    if len(servers) > 8:
        return _error("At most 8 DNS servers are allowed.")

    async def action():
        return await get_broker_client().authorized(
            "/public_api/v1/network/interface",
            {
                "interface_type": interface_type,
                "name": name,
                "address": address,
                "netmask": netmask,
                "gateway": gateway,
                "dns": servers,
                "is_admin": is_admin,
            },
        )

    return await _call(action)


async def post_network_internal_subnet(
    ctx: Context,
    docker_subnet: Annotated[str, Field(description="Parent Docker address pool CIDR, for example 172.17.0.0/18.")],
) -> str:
    """
    Side effects: this operation changes the Broker VM appliance (POST /public_api/v1/network/internal_subnet).
    It restarts Docker and can make the broker unavailable for up to a minute. Confirm the subnet before calling.
    """
    if error := _non_empty(docker_subnet, "Docker subnet"):
        return _error(error)

    async def action():
        return await get_broker_client().authorized(
            "/public_api/v1/network/internal_subnet",
            {"docker_subnet": docker_subnet},
        )

    return await _call(action)


async def post_network_proxy(
    ctx: Context,
    proxy_type: Annotated[
        Literal["", "http", "socks4", "socks5"],
        Field(description="Empty string disables the proxy. Otherwise http, socks4, or socks5."),
    ],
    host: Annotated[str, Field(description="Proxy host. Required when proxy_type is set.", default="")] = "",
    port: Annotated[
        Optional[int],
        Field(description="Proxy port from 1 to 65535. Required when proxy_type is set.", default=None),
    ] = None,
    user: Annotated[str, Field(description="Proxy username.", default="")] = "",
    pwd: Annotated[
        Optional[str],
        Field(
            description="Password tristate: omit to keep the stored password, empty string to clear it, or a value to set it.",
            default=None,
        ),
    ] = None,
) -> str:
    """
    Side effects: this operation changes the Broker VM appliance (POST /public_api/v1/network/proxy).
    It changes the outbound proxy and restarts broker services. Confirm the proxy before calling.
    """
    if proxy_type and (not host.strip() or port is None):
        return _error("host and port are required when proxy_type is set.")
    if port is not None and not 1 <= port <= 65535:
        return _error("Proxy port must be from 1 to 65535.")

    async def action():
        return await get_broker_client().authorized(
            "/public_api/v1/network/proxy",
            {"proxy_type": proxy_type, "host": host, "port": port, "user": user, "pwd": pwd},
        )

    return await _call(action)


async def post_network_ntp(
    ctx: Context,
    ntp: Annotated[list[str], Field(description="Replacement NTP servers. One to ten hostnames or IP addresses.")],
) -> str:
    """
    Side effects: this operation changes the Broker VM appliance (POST /public_api/v1/network/ntp).
    It replaces the NTP server list. Confirm the servers before calling.
    """
    servers = [item.strip() for item in ntp if item and item.strip()]
    if not 1 <= len(servers) <= 10:
        return _error("Provide from 1 to 10 NTP servers.")

    async def action():
        return await get_broker_client().authorized("/public_api/v1/network/ntp", {"ntp": servers})

    return await _call(action)


async def post_network_ssl_certificate(
    ctx: Context,
    ssl_key: Annotated[str, Field(description="Base64-encoded PEM private key, at most about 1.4 MiB.")],
    ssl_cert: Annotated[str, Field(description="Base64-encoded PEM certificate chain, at most about 1.4 MiB.")],
    ssl_key_name: Annotated[str, Field(description="Optional display name, at most 128 characters.", default="")] = "",
) -> str:
    """
    Side effects: this operation changes the Broker VM appliance (POST /public_api/v1/network/ssl_certificate).
    It replaces the HTTPS serving certificate. A bad PEM can break the next handshake. Confirm before calling.
    """
    for label, value in (("ssl_key", ssl_key), ("ssl_cert", ssl_cert)):
        if error := _pem_error(value, label):
            return _error(error)
    if len(ssl_key_name) > 128:
        return _error("ssl_key_name exceeds 128 characters.")

    async def action():
        return await get_broker_client().authorized(
            "/public_api/v1/network/ssl_certificate",
            {"ssl_key": ssl_key, "ssl_cert": ssl_cert, "ssl_key_name": ssl_key_name},
        )

    return await _call(action)


async def post_network_trusted_ca(
    ctx: Context,
    file: Annotated[str, Field(description="Base64-encoded PEM CA bundle, at most about 1.4 MiB.")],
) -> str:
    """
    Side effects: this operation changes the Broker VM appliance (POST /public_api/v1/network/trusted_ca).
    It installs a trusted CA bundle. Confirm the bundle before calling.
    """
    if error := _pem_error(file, "CA bundle"):
        return _error(error)

    async def action():
        return await get_broker_client().authorized("/public_api/v1/network/trusted_ca", {"file": file})

    return await _call(action)


async def post_register(
    ctx: Context,
    token: Annotated[
        str,
        Field(description="Registration token from the tenant API POST /public_api/v1/brokers/registration_token/."),
    ],
) -> str:
    """
    Side effects: this operation changes the Broker VM appliance (POST /public_api/v1/register).
    It activates the broker against the Cortex tenant. Confirm the registration token before calling.
    """
    if error := _non_empty(token, "Registration token"):
        return _error(error)

    async def action():
        return await get_broker_client().authorized("/public_api/v1/register", {"token": token})

    return await _call(action)


_BROKER_TOOLS = (
    post_auth_reset_initial_password,
    post_auth_token,
    post_logs,
    post_network_interface,
    post_network_internal_subnet,
    post_network_proxy,
    post_network_ntp,
    post_network_ssl_certificate,
    post_network_trusted_ca,
    post_register,
)


class BrokerVmModule(BaseModule):
    """On-appliance Broker VM operations. They are not the tenant /brokers/ API."""

    def register_tools(self):
        if not get_config().write_tools_enabled:
            return
        for tool in _BROKER_TOOLS:
            self._add_tool(tool)

    def register_resources(self):
        pass

    def __init__(self, mcp: FastMCP):
        super().__init__(mcp)
