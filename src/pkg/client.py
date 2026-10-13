import asyncio
import contextvars
import gzip
import io
import json
import logging
import random
import zlib
from json import JSONDecodeError
from typing import Literal, assert_never

import httpx2 as httpx
from httpx2 import ConnectError, RequestError, TimeoutException

from config.config import get_config
from entities.exceptions import (
    PAPIAuthenticationError,
    PAPIClientError,
    PAPIClientRequestError,
    PAPIConnectionError,
    PAPIResponseError,
    PAPIServerError,
)
from pkg.util import get_papi_auth_headers

logger = logging.getLogger(__name__)

# Headers the MCP client may copy onto an outbound Cortex request. Everything
# else (cookies, custom client headers, the inbound bearer) is dropped in send().
_PASSTHROUGH_HEADERS = {
    "content-type",
    "content-length",
    "accept",
    "accept-encoding",
    "host",
    "user-agent",
    "x-is-mcp",
}

# Confirmed full jitter: sleep a uniform value in [0, min(cap, base * 2**attempt)].
# The failed attempt starts at 0, so the first retry sleeps in [0, base].
_RETRY_STATUS_CODES = {429, 503}
_RETRY_BASE_SECONDS = 0.5
_RETRY_CAP_SECONDS = 8.0


def _backoff_seconds(attempt: int) -> float:
    ceiling = min(_RETRY_CAP_SECONDS, _RETRY_BASE_SECONDS * (2**attempt))
    return random.uniform(0, ceiling)


def _cortex_error_code(body: str) -> int | str | None:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    reply = payload.get("reply")
    # A JSON null reply code is absent. Fall through to a sibling top-level code.
    if isinstance(reply, dict) and "err_code" in reply and reply["err_code"] is not None:
        code = reply["err_code"]
    elif "err_code" in payload:
        code = payload["err_code"]
    else:
        return None
    if isinstance(code, bool) or not isinstance(code, int | str):
        return None
    return code


def _status_exception(url: str, status_code: int, body: str) -> PAPIClientError:
    code = _cortex_error_code(body)
    code_text = f", Cortex error code {code}" if code is not None else ""
    detail = f"(HTTP {status_code}{code_text})"
    if status_code == 401:
        message = f"Authentication failed for request to {url} {detail}: {body}"
        error: PAPIClientError = PAPIAuthenticationError(message, status_code=status_code, cortex_error_code=code)
    elif status_code == 403:
        message = f"Authorization failed for request to {url} {detail}: {body}"
        error = PAPIAuthenticationError(message, status_code=status_code, cortex_error_code=code)
    elif 400 <= status_code < 500:
        message = f"Client error for request to {url}: {body} {detail}"
        error = PAPIClientRequestError(message, status_code=status_code, cortex_error_code=code)
    elif 500 <= status_code < 600:
        message = f"Server error for request to {url}: {body} {detail}"
        error = PAPIServerError(message, status_code=status_code, cortex_error_code=code)
    else:
        message = f"Unexpected response code for request to {url}: {body} {detail}"
        error = PAPIResponseError(message, status_code=status_code, cortex_error_code=code)
    return error


# Set while request(..., raw=True) is in flight. Catalog tools call send()
# directly and leave this false, so a zip body can be turned into a clean error
# instead of a UTF-8 traceback. Raw callers still receive the original bytes.
_preserve_binary_body: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "papi_preserve_binary_body",
    default=False,
)
_GZIP_MAGIC = b"\x1f\x8b"


class ResponseBytes(bytes):
    """Raw response body tagged with the media type and content encoding.

    ``request(..., raw=True)`` returns this. It compares equal to the payload
    bytes, and callers can read ``content_type`` and ``content_encoding``.
    """

    content_type: str
    content_encoding: str

    def __new__(
        cls,
        data: bytes,
        *,
        content_type: str,
        content_encoding: str,
    ) -> "ResponseBytes":
        body = bytes.__new__(cls, data)
        body.content_type = content_type
        body.content_encoding = content_encoding
        return body


def response_media_type(content_type: str) -> str:
    """Return the media type without parameters, or ``application/octet-stream``."""
    media = content_type.split(";", 1)[0].strip().lower()
    return media or "application/octet-stream"


def binary_response_error(mime: str, size: int) -> str:
    """Error text for a body that is not UTF-8 and is not handled as an export."""
    return f"binary response ({mime}, {size} bytes) not supported by this tool"


def _response_text(response: httpx.Response) -> str:
    try:
        return response.text
    except UnicodeError:
        mime = response_media_type(response.headers.get("content-type", ""))
        return binary_response_error(mime, len(response.content))


def _response_with_body(response: httpx.Response, body: bytes) -> httpx.Response:
    headers = httpx.Headers(response.headers)
    for name in ("content-encoding", "content-length", "transfer-encoding"):
        if name in headers:
            del headers[name]
    return httpx.Response(
        status_code=response.status_code,
        headers=headers,
        content=body,
        request=getattr(response, "_request", None),
    )


def _coerce_textual_response(response: httpx.Response) -> httpx.Response:
    """Gunzip a raw gzip body, and refuse bytes that are not UTF-8.

    httpx leaves the gzip magic in place when ``Content-Encoding`` is missing.
    FastMCP then calls ``response.json()`` / ``response.text`` and a zip or
    other binary payload becomes ``'utf-8' codec``. A decoded gzip body is
    returned in its place. Callers that asked for the original bytes skip this.
    """
    content = response.content
    if content[:2] == _GZIP_MAGIC:
        try:
            decoded = gzip.decompress(content)
        except (OSError, EOFError, zlib.error):
            pass
        else:
            content = decoded
            response = _response_with_body(response, content)
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        mime = response_media_type(response.headers.get("content-type", ""))
        raise PAPIResponseError(binary_response_error(mime, len(content))) from None
    return response


class PAPIClient(httpx.AsyncClient):
    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_key_id: str,
        key_type: Literal["standard", "advanced"] = "standard",
        timeout: int = 30,
        **kwargs,
    ):
        """
        Initialize PAPIClient as an AsyncClient.

        Auth headers are computed in send(). Advanced keys need a fresh nonce and
        timestamp on every request, including each retry, and FastMCP calls send() directly.
        """
        self._api_key = api_key
        self._api_key_id = str(api_key_id)
        self._key_type = key_type

        if "timeout" not in kwargs:
            kwargs["timeout"] = timeout

        super().__init__(base_url=base_url, headers={"X-IS-MCP": "true"}, **kwargs)

    def _auth_header_names(self) -> set[str]:
        match self._key_type:
            case "advanced":
                return {"authorization", "x-xdr-auth-id", "x-xdr-nonce", "x-xdr-timestamp"}
            case "standard":
                return {"authorization", "x-xdr-auth-id"}
            case _:
                assert_never(self._key_type)

    def _strip_untrusted_headers(self, request: httpx.Request) -> None:
        # FastMCP copies the inbound MCP client's headers onto the outbound request.
        # Drop those, including a spoofed bearer, and keep content headers. Auth
        # names stay so send() can overwrite them with a fresh signature.
        kept = self._auth_header_names()
        for key in list(request.headers.keys()):
            lowered = key.lower()
            if lowered not in _PASSTHROUGH_HEADERS and lowered not in kept:
                del request.headers[key]

    def _apply_auth_headers(self, request: httpx.Request) -> None:
        auth_headers = get_papi_auth_headers(self._api_key, self._api_key_id, self._key_type)
        for key, value in auth_headers.items():
            request.headers[key] = value

    def _clone_request(self, request: httpx.Request) -> httpx.Request:
        # httpx keeps the per-request timeout in extensions. A retry that drops
        # it falls back to the client timeout.
        return httpx.Request(
            method=request.method,
            url=request.url,
            headers=httpx.Headers(request.headers),
            content=bytes(request.content),
            extensions=dict(request.extensions),
        )

    async def _pause_before_retry(self, attempt: int) -> None:
        await asyncio.sleep(_backoff_seconds(attempt))

    async def send(self, request: httpx.Request, **kwargs) -> httpx.Response:
        await request.aread()
        # Sign after cloning the unsigned template. A 429/503 that reached the
        # tenant consumes the nonce, so each attempt needs its own advanced hash.
        self._strip_untrusted_headers(request)
        template = self._clone_request(request)
        max_retries = get_config().max_retries
        attempt = 0
        while True:
            outgoing = request if attempt == 0 else self._clone_request(template)
            self._apply_auth_headers(outgoing)
            try:
                response = await super().send(outgoing, **kwargs)
            except (ConnectError, TimeoutException, RequestError) as exc:
                if attempt >= max_retries:
                    raise
                logger.warning(
                    "Retrying %s %s after %s (retry %s of %s)",
                    outgoing.method,
                    outgoing.url,
                    type(exc).__name__,
                    attempt + 1,
                    max_retries,
                )
                await self._pause_before_retry(attempt)
                attempt += 1
                continue
            if response.status_code in _RETRY_STATUS_CODES and attempt < max_retries:
                logger.warning(
                    "Retrying %s %s after HTTP %s (retry %s of %s)",
                    outgoing.method,
                    outgoing.url,
                    response.status_code,
                    attempt + 1,
                    max_retries,
                )
                await response.aclose()
                await self._pause_before_retry(attempt)
                attempt += 1
                continue
            if kwargs.get("stream") or _preserve_binary_body.get():
                return response
            return _coerce_textual_response(response)

    def _get_default_headers(self) -> httpx.Headers:
        """Get default headers with authentication."""
        headers = self.headers
        headers.update({"Content-Type": "application/json", "X-IS-MCP": "true"})
        return headers

    def _get_download_default_headers(self) -> httpx.Headers:
        """Get default headers with authentication."""
        headers = self.headers
        headers.update(
            {
                "Content-Type": "application/zip",
            }
        )
        return headers

    async def request(  # type: ignore[override]
        self,
        method: str,
        url: str,
        *,
        content=None,
        data=None,
        files=None,
        json=None,
        params=None,
        headers=None,
        cookies=None,
        timeout=None,
        raw: bool = False,
    ) -> dict | bytes:
        """
        Send an HTTP request to the PAPI server asynchronously.

        Args:
            method (str): HTTP method (GET, POST, PUT, DELETE, etc.)
            url (str): API endpoint path to append to the base URL
            data (dict, optional): Request payload data. Will be JSON serialized.
            headers (dict, optional): Custom HTTP headers. If not provided, default
                                    headers with authentication will be used.

        Returns:
            dict: Parsed JSON response from the server

        Raises:
            PAPIConnectionError: Raised when there are network connectivity issues:
                - Connection cannot be established to the server
                - Request timeout occurs
                - General network/transport errors
                - DNS resolution failures

            PAPIAuthenticationError: Raised for authentication/authorization failures:
                - 401 Unauthorized: Invalid API key or credentials
                - 403 Forbidden: Valid credentials but insufficient permissions

            PAPIClientRequestError: Raised for client-side request errors (4xx):
                - 400 Bad Request: Invalid request format or parameters
                - 404 Not Found: Requested resource doesn't exist
                - 405 Method Not Allowed: HTTP method not supported for endpoint
                - 409 Conflict: Request conflicts with current server state
                - 422 Unprocessable Entity: Request validation failed
                - Other 4xx status codes

            PAPIServerError: Raised for server-side errors (5xx):
                - 500 Internal Server Error: Unexpected server error
                - 502 Bad Gateway: Invalid response from upstream server
                - 503 Service Unavailable: Server temporarily unavailable
                - 504 Gateway Timeout: Upstream server timeout
                - Other 5xx status codes

            PAPIResponseError: Raised for invalid or malformed responses:
                - Server returns None response
                - Invalid JSON in response body
                - Unexpected HTTP status codes outside standard ranges

            PAPIClientError: Raised for unexpected errors that don't fit other categories:
                - Unexpected exceptions during request processing
                - Programming errors or edge cases

        Example:
            >>> async with PAPIClient("https://api.example.com", "api-key", "api-key-id") as client:
            ...     try:
            ...         result = await client.request("GET", "/endpoints")
            ...     except PAPIAuthenticationError:
            ...         print("Check your API credentials")
            ...     except PAPIConnectionError:
            ...         print("Network connection issue")
            ...     except PAPIServerError:
            ...         print("Server is experiencing issues")
        """
        if headers is None:
            headers = self._get_default_headers()
        else:
            # Merge with default headers, allowing custom headers to override
            default_headers = self._get_default_headers()
            default_headers.update(headers)
            headers = default_headers

        # Multipart uploads must set their own Content-Type boundary. Forcing
        # application/json here makes the tenant reject the body.
        if files is not None:
            headers.pop("Content-Type", None)
            headers.pop("content-type", None)

        full_url = f"{self.base_url}{url}"
        logger.info(f"Sending async request to {full_url}")

        # raw=True must observe the original body. send() otherwise gunzips a
        # gzip payload and refuses non-UTF-8 bytes before this method sees them.
        token = _preserve_binary_body.set(raw)
        try:
            try:
                response = await super().request(
                    method=method,
                    url=url,
                    data=data,
                    params=params,
                    headers=headers,
                    cookies=cookies,
                    timeout=timeout if timeout else self.timeout,
                    json=None if files is not None else json,
                    files=files,
                    content=content,
                )
            except ConnectError as e:
                logger.exception(f"Connection failed for request to {url}: {e}")
                raise PAPIConnectionError(f"Failed to connect to PAPI server at {url}: {e}") from e
            except TimeoutException as e:
                logger.exception(f"Request timeout for request to {url}: {e}")
                raise PAPIConnectionError(f"Request timeout for {url}: {e}") from e
            except RequestError as e:
                logger.exception(f"Request failed for request to {url}: {e}")
                raise PAPIConnectionError(f"Request failed for {url}: {e}") from e
            except PAPIClientError:
                raise
            except Exception as e:
                logger.exception(f"Unexpected error sending request to {url}: {e}")
                raise PAPIClientError(f"Unexpected error for request to {url}: {e}") from e

            if response is None:
                err_msg = f"Received None response from server for request to {url}"
                logger.error(err_msg)
                raise PAPIResponseError(err_msg)

            if response.status_code < 200 or response.status_code >= 300:
                err = _status_exception(url, response.status_code, _response_text(response))
                logger.error(str(err))
                raise err

            if raw:
                return ResponseBytes(
                    response.content,
                    content_type=response.headers.get("content-type", ""),
                    content_encoding=response.headers.get("content-encoding", ""),
                )

            try:
                return response.json()
            except JSONDecodeError as e:
                err_msg = f"Invalid JSON response from server for request to {url}: {e}"
                logger.error(err_msg)
                raise PAPIResponseError(err_msg) from e
        finally:
            _preserve_binary_body.reset(token)

    async def stream(  # type: ignore[override]
        self,
        method: str,
        url: str,
        *,
        content=None,
        data=None,
        files=None,
        json=None,
        params=None,
        headers=None,
        cookies=None,
        timeout=None,
    ) -> io.BytesIO:
        """
        Asynchronously downloads a file from a URL using httpx streaming
        and returns it as an in-memory bytes buffer.

        This method is memory-efficient as it doesn't load the entire file
        into memory at once.

        Args:
            url: The URL of the zip file to download.
            data (dict, optional): Request payload data. Will be JSON serialized.
            headers (dict, optional): Custom HTTP headers. If not provided, default
                                    headers with authentication will be used.

        Returns:
            An io.BytesIO object containing the downloaded zip file data,
            or None if the download failed.

        Raises:
            Same exceptions as request() method for consistency.
        """
        logger.info(f"Attempting to download MCP server content from: {url}")

        if headers is None:
            headers = self._get_download_default_headers()
        else:
            # Merge with default headers, allowing custom headers to override
            default_headers = self._get_download_default_headers()
            default_headers.update(headers)
            headers = default_headers

        try:
            # Use io.BytesIO to create an in-memory binary buffer.
            zip_buffer = io.BytesIO()

            async with super().stream(
                method=method,
                url=url,
                content=content if content else data,
                params=params,
                headers=headers,
                cookies=cookies,
                timeout=timeout if timeout else self.timeout,
                json=json,
                follow_redirects=True,
            ) as response:

                # Helper function to safely get response content for error messages
                async def get_response_content() -> str:
                    try:
                        # For streaming responses, we need to read the content
                        content_bytes = b""
                        async for res_chunk in response.aiter_bytes():
                            content_bytes += res_chunk
                            # Limit content size for error messages (first 1000 chars)
                            if len(content_bytes) > get_config().http_response_error_message_max_size:
                                break
                        return content_bytes.decode("utf-8", errors="ignore")
                    except Exception:
                        return f"Unable to read response content (status: {response.status_code})"

                if response.status_code < 200 or response.status_code >= 300:
                    response_text = await get_response_content()
                    err = _status_exception(url, response.status_code, response_text)
                    logger.error(str(err))
                    raise err

                # If we get here, the response was successful (2xx)
                # Get the total file size from headers if available.
                total_size = int(response.headers.get("Content-Length", 0))
                downloaded_size = 0

                # Iterate over the response content in chunks asynchronously.
                async for chunk in response.aiter_bytes():
                    zip_buffer.write(chunk)
                    downloaded_size += len(chunk)
                    if total_size > 0:
                        # Display download progress.
                        progress = (downloaded_size / total_size) * 100
                        logger.info(f"\rDownloading... {progress:.2f}% complete")

                logger.info("\nDownload finished successfully.")

        except ConnectError as e:
            logger.exception(f"Connection failed for request to {url}: {e}")
            raise PAPIConnectionError(f"Failed to connect to PAPI server at {url}: {e}") from e
        except TimeoutException as e:
            logger.exception(f"Request timeout for request to {url}: {e}")
            raise PAPIConnectionError(f"Request timeout for {url}: {e}") from e
        except RequestError as e:
            logger.exception(f"Request failed for request to {url}: {e}")
            raise PAPIConnectionError(f"Request failed for {url}: {e}") from e
        except (PAPIAuthenticationError, PAPIClientRequestError, PAPIServerError, PAPIResponseError):
            # Re-raise our custom exceptions without wrapping
            raise
        except Exception as e:
            logger.exception(f"Unexpected error sending request to {url}: {e}")
            raise PAPIClientError(f"Unexpected error for request to {url}: {e}") from e

        # Reset the buffer's position to the beginning (0).
        # This is crucial so that other libraries (like zipfile) can read it from the start.
        zip_buffer.seek(0)
        return zip_buffer
