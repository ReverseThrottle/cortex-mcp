import os

# The experimental OpenAPI parser is what the server uses. It must be selected
# before FastMCP is imported.
os.environ.setdefault("FASTMCP_EXPERIMENTAL_ENABLE_NEW_OPENAPI_PARSER", "true")
