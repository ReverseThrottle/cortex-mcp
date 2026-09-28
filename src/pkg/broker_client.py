import logging
import time

import httpx

from config.config import get_config

logger = logging.getLogger(__name__)

_TOKEN_TTL_SECONDS = 9 * 60
_TOKEN_PATH = "/public_api/v1/auth/token"


class BrokerError(Exception):
    """The on-appliance broker call could not be completed."""


def broker_base_url(raw: str) -> str:
    """HTTPS origin of the broker appliance. A bare host is prefixed with https://."""
    value = (raw or "").strip().rstrip("/")
    if not value:
        raise BrokerError("CORTEX_MCP_BROKER_URL is not set.")
    if value.startswith("http://"):
        raise BrokerError("CORTEX_MCP_BROKER_URL must use https. The factory password is not sent in clear text.")
    if not value.startswith("https://"):
        value = "https://" + value
    return value


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted[:500]


class BrokerClient:
    """Calls the broker appliance with a server-side password and its own bearer.

    Inbound MCP headers are never copied onto these requests.
    """

    def __init__(self, base_url: str, password: str, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url
        self.password = password
        self._transport = transport
        self._token: str | None = None
        self._token_deadline = 0.0

    def clear_token(self) -> None:
        self._token = None
        self._token_deadline = 0.0

    async def request(
        self,
        path: str,
        json_body: dict | None = None,
        *,
        token: str | None = None,
        raw: bool = False,
        timeout: int = 120,
    ) -> dict | bytes:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=timeout,
                transport=self._transport,
            ) as client:
                response = await client.post(path, json=json_body, headers=headers)
        except httpx.HTTPError as exc:
            logger.exception("Broker request to %s failed", path)
            raise BrokerError(f"Broker request to {path} failed: {exc}") from exc

        if response.status_code < 200 or response.status_code >= 300:
            detail = _redact(response.text, (self.password, token or "", self._token or ""))
            raise BrokerError(f"Broker {path} returned {response.status_code}: {detail}")
        if raw:
            return response.content
        try:
            parsed = response.json()
        except ValueError as exc:
            raise BrokerError(f"Broker {path} returned a non-JSON success body.") from exc
        if not isinstance(parsed, dict):
            raise BrokerError(f"Broker {path} returned a non-object JSON body.")
        return parsed

    async def login(self) -> str:
        """Exchange the server password for a short-lived broker bearer. The token stays here."""
        logger.info("Exchanging the broker factory password for a short-lived token")
        payload = await self.request(_TOKEN_PATH, {"password": self.password}, timeout=60)
        if not isinstance(payload, dict):
            raise BrokerError("Broker token response was not a JSON object.")
        reply = payload.get("reply")
        api_key = reply.get("api_key") if isinstance(reply, dict) else None
        if not isinstance(api_key, str) or not api_key:
            raise BrokerError("Broker token response did not include an api_key.")
        self._token = api_key
        self._token_deadline = time.monotonic() + _TOKEN_TTL_SECONDS
        return api_key

    async def token(self) -> str:
        if self._token and time.monotonic() < self._token_deadline:
            return self._token
        return await self.login()

    async def authorized(self, path: str, json_body: dict | None = None, *, raw: bool = False, timeout: int = 120):
        bearer = await self.token()
        return await self.request(path, json_body, token=bearer, raw=raw, timeout=timeout)


_client: BrokerClient | None = None
_client_key: tuple[str, str] | None = None


def reset_broker_client() -> None:
    global _client, _client_key
    _client = None
    _client_key = None


def get_broker_client() -> BrokerClient:
    """Return the process-wide broker client built from server settings."""
    global _client, _client_key
    config = get_config()
    url = broker_base_url(config.broker_url)
    password = config.broker_factory_password
    if not password:
        raise BrokerError("CORTEX_MCP_BROKER_FACTORY_PASSWORD is not set.")
    key = (url, password)
    if _client is None or _client_key != key:
        _client = BrokerClient(url, password)
        _client_key = key
    return _client


def use_new_admin_password(client: BrokerClient, new_password: str) -> None:
    """Keep the new admin password on this client. Later token exchanges in this process use it."""
    global _client_key
    client.password = new_password
    client.clear_token()
    if _client is client:
        _client_key = (client.base_url, new_password)
