from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from pkg.util import REMOTE_DIR


class Settings(BaseSettings):
    """
    A simple, flat configuration model using Pydantic.
    It loads settings from environment variables.
    """

    # --- Server Settings ---
    mcp_transport: str = Field("stdio", validation_alias="MCP_TRANSPORT")
    mcp_host: str = Field("127.0.0.1", validation_alias="MCP_HOST")
    mcp_port: int = Field(8080, validation_alias="MCP_PORT")
    mcp_path: str = Field("/api/v1/stream/mcp", validation_alias="MCP_PATH")
    mcp_auth_token: str = Field("", validation_alias="MCP_AUTH_TOKEN")
    # "*" accepts the public Host header a container or Fly proxy sends.
    # Origin is still checked. An empty origin list allows a missing Origin
    # and same-origin or loopback browser calls.
    mcp_allowed_hosts: str = Field("*", validation_alias="MCP_ALLOWED_HOSTS")
    mcp_allowed_origins: str = Field("", validation_alias="MCP_ALLOWED_ORIGINS")
    tool_calls_per_second: float = Field(10.0, validation_alias="MCP_TOOL_CALLS_PER_SECOND")
    tool_call_burst: int = Field(20, validation_alias="MCP_TOOL_CALL_BURST")
    elicitation_enabled: bool = Field(False, validation_alias="MCP_ELICITATION_ENABLED")
    write_tools_enabled: bool = Field(False, validation_alias="MCP_WRITE_TOOLS_ENABLED")
    isolate_endpoint_tool_enabled: bool = Field(False, validation_alias="MCP_ISOLATE_ENDPOINT_TOOL_ENABLED")
    http_response_error_message_max_size: int = Field(1000, validation_alias="CORTEX_MCP_RESPONSE_ERROR_MAX_SIZE")
    max_retries: int = Field(3, validation_alias="CORTEX_MCP_MAX_RETRIES")

    # --- PAPI Settings ---
    papi_url_env_key: str = Field("", validation_alias="CORTEX_MCP_PAPI_URL")
    papi_auth_header_key: str = Field("", validation_alias="CORTEX_MCP_PAPI_AUTH_HEADER")
    papi_auth_id_key: str = Field("", validation_alias="CORTEX_MCP_PAPI_AUTH_ID")
    papi_key_type: Literal["standard", "advanced"] = Field("standard", validation_alias="CORTEX_MCP_PAPI_KEY_TYPE")
    broker_url: str = Field("", validation_alias="CORTEX_MCP_BROKER_URL")
    broker_factory_password: str = Field("", validation_alias="CORTEX_MCP_BROKER_FACTORY_PASSWORD")

    max_objects_to_retrieve: int = Field(50, validation_alias="MAX_OBJECTS_TO_RETRIEVE")

    # --- Log Settings ---
    log_enable_uvicorn_access_logs: bool = Field(True, validation_alias="LOG_ENABLE_UVICORN_ACCESS_LOGS")
    log_level: str = Field("DEBUG", validation_alias="LOG_LEVEL")

    # This configuration tells Pydantic to:
    # 1. Load variables from a file named '.env' (for local development).
    # 2. Ignore any extra environment variables that aren't defined in this class.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Update Settings ---
    update_folder: str = Field(REMOTE_DIR.as_posix(), validation_alias="CORTEX_MCP_UPDATE_FOLDER")


# Global config instance
config = Settings()


def reload_config():
    """Reload the global config instance"""
    global config
    config = Settings()
    return config


def get_config():
    """Get the current config instance"""
    return config
