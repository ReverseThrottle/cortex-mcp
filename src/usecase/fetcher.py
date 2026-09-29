import io
import logging
import posixpath
from typing import Any, Literal, Optional, overload

from fastmcp import Context

from config.config import get_config
from entities.MCPContext import MCPContext
from pkg.client import PAPIClient
from pkg.util import get_papi_url

logger = logging.getLogger(__name__)


class Fetcher:
    """
    Fetcher class for interacting with public API endpoints.
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        api_key_id: str,
        key_type: Literal["standard", "advanced"] = "standard",
    ) -> None:
        """
        Initialize the Fetcher with a URL and an API key for authentication.

        Args:
            url (str): The url of the public API.
            api_key (str): The API key to use with the public API
            api_key_id (str): The API key ID to use with the public API
            key_type: "standard" or "advanced", matching the Cortex API key type.
        """
        self.url = url
        self.api_key = api_key
        self.api_key_id = api_key_id
        self.key_type = key_type

    @overload
    async def send_request(
        self,
        path: str,
        method: str = ...,
        data: Optional[dict | str] = ...,
        headers: Optional[dict] = ...,
        omit_papi_prefix: bool = ...,
        *,
        stream: Literal[True],
        files: Optional[dict] = ...,
        raw: Literal[False] = ...,
        timeout: Optional[int] = ...,
    ) -> io.BytesIO: ...

    @overload
    async def send_request(
        self,
        path: str,
        method: str = ...,
        data: Optional[dict | str] = ...,
        headers: Optional[dict] = ...,
        omit_papi_prefix: bool = ...,
        *,
        stream: Literal[False] = ...,
        files: Optional[dict] = ...,
        raw: Literal[True],
        timeout: Optional[int] = ...,
    ) -> bytes: ...

    @overload
    async def send_request(
        self,
        path: str,
        method: str = ...,
        data: Optional[dict | str] = ...,
        headers: Optional[dict] = ...,
        omit_papi_prefix: bool = ...,
        stream: Literal[False] = ...,
        files: Optional[dict] = ...,
        raw: Literal[False] = ...,
        timeout: Optional[int] = ...,
    ) -> dict[str, Any]: ...

    async def send_request(
        self,
        path: str,
        method: str = "POST",
        data: Optional[dict | str] = None,
        headers: Optional[dict] = None,
        omit_papi_prefix: bool = False,
        stream: bool = False,
        files: Optional[dict] = None,
        raw: bool = False,
        timeout: Optional[int] = None,
    ) -> dict[str, Any] | io.BytesIO | bytes:
        """
        Send an HTTP request to the public API.

        Automatically prepends the public API v1 path prefix unless omit_papi_prefix is True.
        Delegates the actual request to the underlying PAPIClient.

        Args:
            path (str): The API endpoint path to send the request to.
            method (str, optional): The HTTP method to use. Defaults to "POST".
            data (dict | str, optional): The request payload data. Defaults to None.
            headers (dict, optional): Additional HTTP headers to include. Defaults to None.
            omit_papi_prefix (bool, optional): Whether to skip adding the /public_api/v1 prefix. Defaults to False.
            stream (bool, optional): Whether to stream response. Defaults to False.
            files (dict, optional): Multipart file payload. When set, the body is not sent as JSON.
            raw (bool, optional): Return the response body bytes instead of parsed JSON.
            timeout (int, optional): Request timeout in seconds. Defaults to the client timeout.

        Returns:
            dict: The response from the request.
        """
        if not omit_papi_prefix:
            # Add the API path
            if "/public_api/v1" not in path and "/public_api/v1/" not in path:
                path = posixpath.join("/public_api/v1", path.lstrip("/"))

        result: dict[str, Any] | io.BytesIO | bytes
        async with PAPIClient(
            self.url,
            self.api_key,
            self.api_key_id,
            key_type=self.key_type,
            timeout=timeout or 30,
        ) as client:
            if raw:
                result = await client.request(
                    method, path, json=data if isinstance(data, dict) else None, headers=headers, raw=True
                )
            elif files is not None:
                result = await client.request(
                    method, path, files=files, data=data if isinstance(data, dict) else None, headers=headers
                )
            elif stream:
                result = await client.stream(method, path, data=data, headers=headers)
            else:
                result = await client.request(method, path, json=data, headers=headers)

        return result


async def get_fetcher(ctx: Context) -> Fetcher:
    """
    Create and configure a Fetcher instance with authentication credentials.

    Retrieves authentication credentials from the context lifespan or environment variables,
    creates a new Fetcher instance, and stores it in the context state.

    Args:
        ctx (Context): The FastMCP context containing request and lifespan information.

    Returns:
        Fetcher: A configured Fetcher instance ready to make API requests.
    """
    config = get_config()
    url = get_papi_url(config.papi_url_env_key)
    request_context = ctx.request_context
    if request_context is None:
        raise RuntimeError("MCP request context is unavailable")
    lifespan: MCPContext = request_context.lifespan_context
    api_key = lifespan.auth_headers.get("Authorization")
    xdr_id = lifespan.auth_headers.get("X-XDR-AUTH-ID")
    if not (api_key and xdr_id):
        api_key = config.papi_auth_header_key
        xdr_id = config.papi_auth_id_key

    logger.info("Creating a new Cortex API fetcher")
    fetcher = Fetcher(url, api_key, xdr_id, key_type=config.papi_key_type)
    ctx.set_state("fetcher", fetcher)
    return fetcher
