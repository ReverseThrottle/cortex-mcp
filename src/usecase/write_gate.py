import functools
import logging
from collections.abc import Awaitable, Callable
from typing import ParamSpec

from config.config import get_config
from pkg.util import create_response

logger = logging.getLogger(__name__)

_P = ParamSpec("_P")


def require_flag(flag_name: str) -> Callable[[Callable[_P, Awaitable[str]]], Callable[_P, Awaitable[str]]]:
    """
    Gate an MCP tool function behind a boolean feature flag on the config Settings object.

    If the named flag is falsy, the wrapped function is not called and a
    create_response(is_error=True) JSON string is returned instead, so the tool
    can stay registered (visible in tools/list) while refusing to execute.

    Args:
        flag_name: Attribute name on the Settings instance returned by get_config(),
                   e.g. "write_tools_enabled" for MCP_WRITE_TOOLS_ENABLED.

    Example:
        @require_flag("write_tools_enabled")
        async def update_case(ctx: Context, ...) -> str:
            ...
    """

    def decorator(fn: Callable[_P, Awaitable[str]]) -> Callable[_P, Awaitable[str]]:
        @functools.wraps(fn)
        async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> str:
            config = get_config()
            if not getattr(config, flag_name, False):
                logger.info(f"Refusing to call {fn.__name__}: {flag_name} is disabled")
                return create_response(
                    data={
                        "error": f"This tool is disabled because the '{flag_name}' feature flag is not enabled. See the README for the matching MCP_* environment variable."
                    },
                    is_error=True,
                )
            return await fn(*args, **kwargs)

        return wrapper

    return decorator
