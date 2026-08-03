# cortex-mcp

A community-built [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server for the [Palo Alto Networks Cortex](https://docs-cortex.paloaltonetworks.com/) platform. It exposes Cortex XSIAM / XDR data — cases, issues, endpoints, assets, vulnerabilities, and XQL queries — as MCP tools that any MCP-compatible AI assistant (Claude, Cursor, etc.) can call directly.

> **License:** Palo Alto Networks Cortex Communication Python Files License 1.0 — see [LICENSE](LICENSE).

---

## Table of Contents

- [Features](#features)
- [Architecture overview](#architecture-overview)
- [Prerequisites](#prerequisites)
- [Quickstart](#quickstart)
  - [Docker (recommended)](#option-1-docker-recommended)
  - [Local / Poetry](#option-2-local--poetry)
- [Configuration](#configuration)
- [Connecting to an AI client](#connecting-to-an-ai-client)
  - [Claude Desktop](#claude-desktop)
  - [Cursor / other MCP clients](#cursor--other-mcp-clients)
  - [HTTP transport (remote server)](#http-transport-remote-server)
- [Available tools](#available-tools)
- [CLI reference](#cli-reference)
- [Extending with custom tools](#extending-with-custom-tools)
  - [Python module](#python-module)
  - [OpenAPI spec](#openapi-spec)
- [Development](#development)
- [Project structure](#project-structure)
- [Troubleshooting](#troubleshooting)

---

## Features

| Category | Tools |
|---|---|
| **Cases / Incidents** | Search & filter cases, update status / severity / assignee, add comments |
| **Issues / Alerts** | Search & filter issues with full pagination |
| **Endpoints** | List & filter endpoints, isolate / unisolate from the network |
| **Assets** | Get asset inventory by ID or filtered list |
| **Vulnerabilities** | Paginated vulnerability search with CVSS, EPSS, CISA KEV filters |
| **XQL** | Execute any XQL query with automatic async polling and result return |
| **Assessment** | Pull assessment profile results |
| **Tenant** | Retrieve tenant information |
| **Remote components** | Pull Cortex-managed tools with the `update` CLI command |

---

## Architecture overview

```
AI Client (Claude / Cursor / …)
        │  MCP protocol (stdio or HTTP)
        ▼
  cortex-mcp server (FastMCP)
        │  HTTPS / REST
        ▼
  Cortex PAPI (your tenant)
```

The server acts as a thin bridge: it translates MCP tool calls into authenticated Cortex PAPI requests and returns the results as structured JSON. All authentication stays on the server — the AI client never sees your credentials.

**Tool sources (three layers):**

| Layer | Location | Who manages it |
|---|---|---|
| Built-in | `src/usecase/builtin_components/` | This repo |
| Custom | `src/usecase/custom_components/` | You |
| Remote | `src/usecase/remote_components/` | Cortex (via `update` command) |

---

## Prerequisites

- **Cortex API credentials** — a Standard API key and its numeric key ID from your Cortex tenant
- One of:
  - Docker (recommended for production / Claude Desktop)
  - Python ≥ 3.12 + [Poetry](https://python-poetry.org/) (recommended for development)

---

## Quickstart

### Option 1: Docker (recommended)

```bash
# 1. Clone the repo
git clone https://github.com/ReverseThrottle/cortex-mcp.git
cd cortex-mcp

# 2. Create your env file
cp .env.example .env
# Edit .env with your Cortex credentials

# 3. Build the image
docker build -t cortex-mcp .

# 4. Run (stdio mode — used by Claude Desktop)
docker run --env-file .env -i --rm cortex-mcp
```

### Option 2: Local / Poetry

```bash
# 1. Clone the repo
git clone https://github.com/ReverseThrottle/cortex-mcp.git
cd cortex-mcp

# 2. Install Poetry (if not already installed)
curl -sSL https://install.python-poetry.org | python3 -

# 3. Install dependencies
poetry install

# 4. Create your env file
cp .env.example .env
# Edit .env with your Cortex credentials

# 5. Start the server
poetry run python src/cli.py start
```

---

## Configuration

All configuration is via environment variables (or a `.env` file in the project root). Copy `.env.example` to `.env` as your starting point.

### Required

| Variable | Description |
|---|---|
| `CORTEX_MCP_PAPI_URL` | Base URL of your Cortex tenant, e.g. `https://api-acme.xdr.us.paloaltonetworks.com` |
| `CORTEX_MCP_PAPI_AUTH_HEADER` | Your API key secret |
| `CORTEX_MCP_PAPI_AUTH_ID` | Numeric ID of the API key |

### Optional — transport

| Variable | Default | Description |
|---|---|---|
| `MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http` |
| `MCP_HOST` | `0.0.0.0` | Bind host (HTTP mode only) |
| `MCP_PORT` | `8080` | Listen port (HTTP mode only) |
| `MCP_PATH` | `/api/v1/stream/mcp` | URL path (HTTP mode only) |
| `MCP_AUTH_TOKEN` | unset | Bearer token required from clients in HTTP mode (`Authorization: Bearer <token>`). Unauthenticated when unset — only safe for local/stdio use. |

### Optional — feature flags

| Variable | Default | Description |
|---|---|---|
| `MCP_WRITE_TOOLS_ENABLED` | `false` | Enable write tools (`update_case`) |
| `MCP_ISOLATE_ENDPOINT_TOOL_ENABLED` | `false` | Enable `isolate_endpoint` / `unisolate_endpoint` |
| `MCP_ELICITATION_ENABLED` | `false` | Enable MCP elicitation support |

### Optional — limits & logging

| Variable | Default | Description |
|---|---|---|
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |
| `LOG_ENABLE_UVICORN_ACCESS_LOGS` | `true` | Toggle uvicorn HTTP access logs |
| `MAX_OBJECTS_TO_RETRIEVE` | `50` | Default page size for list operations |
| `CORTEX_MCP_RESPONSE_ERROR_MAX_SIZE` | `1000` | Max characters of error detail returned to the LLM |

---

## Connecting to an AI client

### Claude Desktop

Open the Claude Desktop config file (accessible from **Settings → Developer**) and add:

**Docker container:**

```json
{
  "mcpServers": {
    "Cortex MCP": {
      "command": "docker",
      "args": [
        "run",
        "--env-file", "/absolute/path/to/.env",
        "-i", "--rm",
        "cortex-mcp"
      ]
    }
  }
}
```

**Local install:**

```json
{
  "mcpServers": {
    "Cortex MCP": {
      "command": "/path/to/cortex-mcp/.venv/bin/python",
      "args": ["/path/to/cortex-mcp/src/main.py"],
      "env": {
        "CORTEX_MCP_PAPI_URL": "https://api-acme.xdr.us.paloaltonetworks.com",
        "CORTEX_MCP_PAPI_AUTH_HEADER": "your_api_key",
        "CORTEX_MCP_PAPI_AUTH_ID": "12345"
      }
    }
  }
}
```

### Cursor / other MCP clients

Most MCP clients accept the same stdio command pattern. Point the command at the Docker run invocation or the local Python binary — exactly as shown for Claude Desktop above.

### HTTP transport (remote server)

Set `MCP_TRANSPORT=streamable-http` in your `.env`, then run the server. The MCP endpoint will be available at:

```
http://<host>:<port>/api/v1/stream/mcp
```

Configure your AI client to connect to that URL using the HTTP MCP transport.

If `MCP_AUTH_TOKEN` is set, clients must send `Authorization: Bearer <token>` on every
request, or they get `401`. Set this for any deployment reachable over a network.

A health-check endpoint is also available at `GET /ping/` (unauthenticated).

---

## Available tools

Tools are grouped by domain. Write tools and the endpoint isolation tools are **disabled by default** and must be explicitly enabled via environment variables.

### Cases

| Tool | Description |
|---|---|
| `get_cases` | Search and filter cases / incidents with pagination and sorting |
| `update_case` | Add a comment, change status, reassign, or update severity (**write, opt-in**) |

### Issues / Alerts

| Tool | Description |
|---|---|
| `get_issues` | Search and filter security issues / alerts with pagination |

### Endpoints

| Tool | Description |
|---|---|
| `get_filtered_endpoints` | List endpoints matching host, OS, or agent status filters |
| `isolate_endpoint` | Block all network traffic on one or more endpoints (**opt-in**) |
| `unisolate_endpoint` | Restore network access to isolated endpoints (**opt-in**) |

### Assets

| Tool | Description |
|---|---|
| `get_assets` | Retrieve the asset inventory with optional filters |
| `get_asset_by_id` | Fetch a single asset by its ID |

### Vulnerabilities

| Tool | Description |
|---|---|
| `get_vulnerabilities` | Paginated vulnerability search (CVSS, EPSS, CISA KEV, package, vendor filters) |

### XQL

| Tool | Description |
|---|---|
| `run_xql_query` | Execute an XQL query; automatically polls until complete and returns results |

### Assessment & Tenant

| Tool | Description |
|---|---|
| `get_assessment_profile_results` | Retrieve assessment profile results |
| `get_tenant_info` | Retrieve tenant metadata |

---

## CLI reference

The CLI is the recommended way to start the server and keep remote components up to date.

```
python src/cli.py <command> [OPTIONS]
```

### `start`

Start the MCP server.

```bash
python src/cli.py start \
  --api_key_id 12345 \
  --api_key_secret "your-secret" \
  --server-url "https://api-acme.xdr.us.paloaltonetworks.com" \
  --log-level INFO
```

All flags fall back to the corresponding environment variables if not provided.

### `update`

Download the latest Cortex-managed remote components from the Cortex API and replace the `remote_components` folder.

```bash
python src/cli.py update
# or with a custom folder:
python src/cli.py update --folder /path/to/remote_components
```

> **Warning:** The `remote_components` folder is fully replaced on every update. Do not store custom tools there.

### `version`

Print the installed version.

```bash
python src/cli.py version
```

---

## Extending with custom tools

Add your own MCP tools in `src/usecase/custom_components/` — the server discovers and registers them automatically at startup.

### Python module

1. Create a Python file in `src/usecase/custom_components/`, e.g. `my_tool.py`.
2. Define a class that inherits from `BaseModule`:

```python
from fastmcp import Context, FastMCP
from usecase.base_module import BaseModule
from pkg.util import create_response
from usecase.fetcher import get_fetcher

async def my_custom_tool(ctx: Context, asset_id: str) -> str:
    """Short description shown to the LLM."""
    fetcher = await get_fetcher(ctx)
    data = await fetcher.send_request("my/endpoint/", data={"request_data": {"id": asset_id}})
    return create_response(data=data)

class MyToolModule(BaseModule):
    def register_tools(self):
        self._add_tool(my_custom_tool)

    def register_resources(self):
        pass

    def __init__(self, mcp: FastMCP):
        super().__init__(mcp)
```

3. Restart the server — the tool appears automatically.

### OpenAPI spec

For simple CRUD-style endpoints, drop a YAML file in `src/usecase/custom_components/openapi/`. Use the files in `src/usecase/builtin_components/openapi/` as a template and consult the [Cortex API docs](https://docs-cortex.paloaltonetworks.com/r/Cortex-Cloud-Platform-APIs/Cortex-Cloud-Platform-APIs).

---

## Development

```bash
# Install all dev dependencies
poetry install

# Run tests
poetry run pytest

# Format
poetry run black .
poetry run isort .

# Lint
poetry run ruff check .

# Type-check
poetry run mypy src/
```

### Debugging with MCP Inspector

The [MCP Inspector](https://github.com/modelcontextprotocol/inspector) is the best tool for interactively testing MCP servers:

```bash
npx @modelcontextprotocol/inspector python src/main.py
```

---

## Project structure

```
cortex-mcp/
├── Dockerfile
├── pyproject.toml
├── .env.example                        # Config template — copy to .env
└── src/
    ├── main.py                         # Async entry point
    ├── cli.py                          # CLI (start / update / version)
    ├── version.py
    ├── config/
    │   └── config.py                   # Pydantic settings (env vars)
    ├── entities/                       # Data models, exceptions, LLM hints
    ├── pkg/                            # HTTP client, logging, utilities
    ├── service/cortex_mcp/
    │   └── server.py                   # FastMCP server factory + lifespan
    └── usecase/
        ├── base_module.py              # BaseModule ABC
        ├── module_util.py              # Auto-discovery of modules
        ├── fetcher.py                  # Authenticated PAPI request helper
        ├── builtin_components/         # Bundled read tools (cases, issues, …)
        │   └── openapi/                # OpenAPI specs for built-in tools
        ├── custom_components/          # YOUR custom tools go here
        │   └── openapi/                # OpenAPI specs for custom tools
        └── remote_components/          # Cortex-managed tools (updated via CLI)
```

---

## Troubleshooting

**`Missing authentication headers`** — Ensure `CORTEX_MCP_PAPI_AUTH_HEADER` and `CORTEX_MCP_PAPI_AUTH_ID` are set correctly.

**`Connection refused` / network errors** — Verify `CORTEX_MCP_PAPI_URL` is reachable from your machine and does not include a trailing `/`.

**Tool not appearing in the AI client** — Check the server logs for registration errors. Confirm the module class inherits from `BaseModule` and calls `super().__init__(mcp)`.

**`update` command fails** — Confirm the API credentials have permission to call the MCP download endpoint and that the target folder exists and is writable.

**XQL query times out** — The server polls up to 30 seconds (15 × 2 s). For long-running queries, reduce scope with tighter filters or a shorter timeframe.

**Debugging** — Set `LOG_LEVEL=DEBUG` for verbose output, or connect with the MCP Inspector for interactive tool testing.
