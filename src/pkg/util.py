import hashlib
import json
import secrets
import string
import time
from pathlib import Path
from typing import Literal, assert_never
from urllib.parse import urlsplit, urlunsplit

from pkg.openapi.openapi import bundle_specs
from pkg.response_envelope import ensure_formatting_metadata

MAIN_DIR = Path(__file__).parent.parent.parent
SCRIPT_DIR = MAIN_DIR / "src"
RESOURCES_DIR = SCRIPT_DIR / "entities" / "resources"
PKG_DIR = SCRIPT_DIR / "pkg"
OPENAPI_DIR = PKG_DIR / "openapi"
USECASES_DIR = SCRIPT_DIR / "usecase"
BUILTINS_DIR = USECASES_DIR / "builtin_components"
CUSTOM_DIR = USECASES_DIR / "custom_components"
REMOTE_DIR = USECASES_DIR / "remote_components"


def create_response(data: dict, is_error: bool = False) -> str:
    """
    Create a JSON response with success status indicator.

    This function takes a dictionary of data and adds a success field to indicate
    whether the operation was successful or resulted in an error. The response
    is returned as a formatted JSON string.

    Args:
        data (dict): The data dictionary to include in the response.
        is_error (bool, optional): Flag indicating if this is an error response.
                                 Defaults to False.

    Returns:
        str: A JSON string containing the data with an added 'success' field.
             Formatting instructions are not added. Set
             ``MCP_INLINE_FORMATTING_INSTRUCTIONS=true`` to include
             ``_metadata.formatting_instructions``. Existing ``_metadata`` is
             left unchanged, including a non-dict value. The JSON is formatted
             with 2-space indentation and non-ASCII characters are preserved.

    Example:
        A successful call keeps the caller's fields and adds ``success``::

            {
              "message": "Operation completed",
              "count": 5,
              "success": "true"
            }

        An error response uses ``success`` ``"false"``::

            {
              "error": "Invalid input",
              "success": "false"
            }

        >>> parsed = json.loads(create_response({"message": "Operation completed", "count": 5}))
        >>> parsed["success"]
        'true'
        >>> sorted(parsed)
        ['count', 'message', 'success']
        >>> "_metadata" in parsed
        False

        >>> error = json.loads(create_response({"error": "Invalid input"}, is_error=True))
        >>> error["success"]
        'false'
        >>> sorted(error)
        ['error', 'success']
    """
    success = "true" if not is_error else "false"
    # Copy so the caller's dict does not gain "success" or "_metadata".
    payload = dict(data)
    payload["success"] = success
    ensure_formatting_metadata(payload)
    return json.dumps(payload, indent=2, ensure_ascii=False)


def read_resource(file_path) -> str:
    """
    Read a file from the resources directory.

    This is a convenience wrapper around read_file() that specifically reads
    files from the predefined RESOURCES_DIR.

    Args:
        file_path (str): Relative path to the file within the resources directory.

    Returns:
        str: The contents of the file as a string.

    Raises:
        ValueError: If path traversal is detected in the file path or if the
                   file cannot be decoded as valid Unicode text.
        FileNotFoundError: If the specified file does not exist in the
                          resources directory.
        PermissionError: If access is denied to the specified file due to
                        insufficient permissions.
    """
    return read_file(file_path, RESOURCES_DIR)


def read_file(file_path: str, file_directory: Path) -> str:
    """
    Safely read a file from a specified directory.

    This function reads a file from the specified directory with security
    measures to prevent path traversal attacks. The file path is validated
    to ensure it stays within the directory boundary.

    Args:
        file_path (str): Relative path to the file within the target directory.
                        Must not contain path traversal sequences like '../'.
        file_directory (Path): The base directory from which to read the file.

    Returns:
        str: The contents of the file as a string.

    Raises:
        ValueError: If path traversal is detected in the file path or if the
                   file cannot be decoded as valid Unicode text.
        FileNotFoundError: If the specified file does not exist in the
                          target directory.
        PermissionError: If access is denied to the specified file due to
                        insufficient permissions.

    Example:
        >>> content = read_file("config.json", Path("/app/resources"))
        >>> print(content)
        # Contents of /app/resources/config.json

        >>> read_file("../../../etc/passwd", Path("/app/resources"))  # This will raise ValueError
        ValueError: Invalid file path: path traversal detected

    Security:
        - Prevents path traversal attacks by validating the resolved path
        - Only allows access to files within the specified directory
        - Handles encoding errors gracefully
    """
    try:
        full_path = (file_directory / file_path).resolve()
        if not full_path.is_relative_to(file_directory.resolve()):
            raise ValueError("Invalid file path: path traversal detected")

        with open(full_path) as file:
            return file.read()
    except FileNotFoundError as e:
        raise FileNotFoundError(f"Resource file not found: {file_path}") from e
    except PermissionError as e:
        raise PermissionError(f"Access denied to resource file: {file_path}") from e
    except UnicodeDecodeError as e:
        raise ValueError(f"Unable to decode file {file_path}: {e}") from e


def get_papi_auth_headers(
    api_key: str,
    api_key_id: str,
    key_type: Literal["standard", "advanced"] = "standard",
) -> dict:
    """
    Generate authentication headers for Palo Alto Networks Cortex API requests.

    Standard keys send the raw key. Advanced keys send SHA256(api_key + nonce + timestamp)
    and must be computed again for every request.
    """
    match key_type:
        case "advanced":
            nonce = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(64))
            timestamp = str(int(time.time()) * 1000)
            auth_key = f"{api_key}{nonce}{timestamp}".encode()
            return {
                "Authorization": hashlib.sha256(auth_key).hexdigest(),
                "x-xdr-auth-id": str(api_key_id),
                "x-xdr-nonce": nonce,
                "x-xdr-timestamp": timestamp,
            }
        case "standard":
            return {
                "Authorization": api_key,
                "X-XDR-AUTH-ID": str(api_key_id),
            }
        case _:
            assert_never(key_type)


def get_papi_url(papi_url_value: str) -> str:
    """
    Construct and return the public API URL from environment variables.

    Checks for custom URL override first, then falls back to the standard URL.
    Ensures the URL uses HTTPS protocol and includes the 'api-' subdomain prefix.
    The prefix is checked on the hostname only. A port is kept and trailing
    slashes are removed.

    Args:
        papi_url_value (str): The URL value to construct the URL from.

    Returns:
        str: The properly formatted public API URL with HTTPS protocol and api- prefix.

    Raises:
        ValueError: If the URL environment variable is not set, or the URL has no
            hostname or an invalid port.
    """
    url = papi_url_value
    if not url:
        raise ValueError("No public API URL provided")

    # Without a scheme, urlsplit reads "tenant.example:443" as scheme "tenant.example".
    if "://" not in url:
        url = f"https://{url}"
    parts = urlsplit(url)
    hostname = parts.hostname
    if not hostname:
        raise ValueError(f"Public API URL has no hostname: {papi_url_value}")
    if not hostname.startswith("api-"):
        hostname = f"api-{hostname}"
    netloc = hostname if parts.port is None else f"{hostname}:{parts.port}"

    return urlunsplit(("https", netloc, parts.path.rstrip("/"), parts.query, parts.fragment))


def path_segment_error(value: str, label: str) -> str | None:
    """Error text when ``value`` cannot be used as one URL path segment, else None.

    A slash, backslash, query, fragment, percent escape, or a ``.``/``..``
    segment would move the request off the documented endpoint.
    """
    if not value or not value.strip():
        return f"{label} is empty."
    if value != value.strip() or value in {".", ".."} or any(char in value for char in "/\\?#%"):
        return f"{label} cannot contain path characters."
    return None


def bundle_openapi_from_folders():
    """
    Bundle OpenAPI specifications from predefined builtin and custom folders.

    This is a convenience function that bundles OpenAPI specifications from
    the standard builtin tools and custom tools directories.

    Returns:
        dict: A dictionary containing the bundled OpenAPI specifications.

    Raises:
        ValueError: If path traversal is detected in any file paths.
        FileNotFoundError: If any required OpenAPI files are not found.
    """
    openapi_dirs = [base_dir / "openapi" for base_dir in [BUILTINS_DIR, CUSTOM_DIR, REMOTE_DIR]]
    return bundle_openapi_files(*openapi_dirs)


def bundle_openapi_files(*specs_dirs: Path) -> dict:
    """
    Bundle OpenAPI specification files from multiple directories into a single dictionary.

    This function reads the main OpenAPI template file and bundles it with
    additional specification files from the provided directories. It includes
    path traversal protection for the template file.

    Args:
        *specs_dirs (Path): Variable number of Path objects representing directories
                           containing OpenAPI specification files to bundle.

    Returns:
        dict: A dictionary containing the bundled OpenAPI specifications.

    Raises:
        ValueError: If path traversal is detected in the template file path.
        FileNotFoundError: If the OpenAPI template file is not found.

    Note:
        Uses the predefined OPENAPI_DIR for the template file location and
        the provided specs_dirs for additional specification files.
    """
    template_file = (OPENAPI_DIR / "openapi.yaml").resolve()
    if not template_file.is_relative_to(OPENAPI_DIR.resolve()):
        raise ValueError("Invalid file path: path traversal detected")

    spec = bundle_specs(template_file, *specs_dirs)
    if spec is None:
        raise RuntimeError("Failed to bundle OpenAPI specifications")
    return spec
