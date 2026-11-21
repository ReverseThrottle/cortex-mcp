"""Guard the Fly deployment config against transport/port drift (issue #88).

fly.toml shipped an [http_service] on internal_port 8080 while the image
defaulted to MCP_TRANSPORT=stdio: on a Fly machine (no attached stdin) the
stdio server saw EOF and exited before ever binding the port, so the proxy
had no backend. The fix sets MCP_TRANSPORT=streamable-http in fly.toml's
[env] — deliberately NOT in the Dockerfile, which would flip bare
`docker run -i` (the documented Claude Desktop stdio pattern) to HTTP.

Structural drift guards, not source-text pins (same doctrine as
tests/test_config_docs.py, issues #54/#56): the port expectation is read
from Settings itself, so changing the MCP_PORT default without updating
fly.toml fails here instead of at deploy time.
"""

from pathlib import Path

from config.config import Settings

ROOT = Path(__file__).resolve().parents[1]
FLY_TOML = ROOT / "fly.toml"
DOCKERFILE = ROOT / "Dockerfile"


def _fly_config() -> dict:
    # Imported here, not at module scope: ruff (default target-version, no
    # [project] table in pyproject.toml) classes tomllib as third-party while
    # isort knows it is stdlib, and the two gates demand conflicting orders.
    import tomllib

    with FLY_TOML.open("rb") as fh:
        return tomllib.load(fh)


def _dockerfile_env() -> dict[str, str]:
    """The image's baked-in ENV key=value pairs, parsed from the Dockerfile."""
    env: dict[str, str] = {}
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if parts and parts[0] == "ENV":
            for pair in parts[1:]:
                key, sep, value = pair.partition("=")
                if sep:
                    env[key] = value
    return env


def test_fly_env_selects_streamable_http() -> None:
    """AC1/AC3: the shipped Fly config must select the HTTP transport itself.

    Without [env] MCP_TRANSPORT the container takes the stdio branch and
    exits on stdin EOF before binding internal_port (#88).
    """
    config = _fly_config()
    assert config.get("env", {}).get("MCP_TRANSPORT") == "streamable-http", (
        "fly.toml must set [env] MCP_TRANSPORT = 'streamable-http'. The image "
        "defaults to stdio, which exits on stdin EOF on a Fly machine and never "
        "binds http_service.internal_port (#88)."
    )


def test_fly_internal_port_matches_settings_default() -> None:
    """AC3: the Fly proxy port and the server's MCP_PORT default cannot drift.

    The expectation comes from Settings, not a second hard-coded 8080, so a
    change to the model default forces fly.toml to follow.
    """
    default_port = Settings.model_fields["mcp_port"].default
    internal_port = _fly_config()["http_service"]["internal_port"]
    assert internal_port == default_port, (
        f"fly.toml http_service.internal_port is {internal_port} but "
        f"Settings.mcp_port defaults to {default_port}. The Fly proxy would "
        f"forward to a port the server does not bind (#88)."
    )


def test_dockerfile_does_not_set_mcp_transport() -> None:
    """AC2: the image stays stdio-by-default for `docker run -i` clients.

    The HTTP default belongs to fly.toml only. An ENV MCP_TRANSPORT in the
    image would silently flip the documented Claude Desktop stdio pattern
    (bare `docker run -i`, or an env file that omits the variable) to HTTP.
    """
    assert "MCP_TRANSPORT" not in _dockerfile_env(), (
        "Do not set ENV MCP_TRANSPORT in the Dockerfile: it would break the "
        "stdio-over-Docker client pattern. Set the HTTP transport in fly.toml "
        "[env] instead (#88). If the image default ever changes, the README "
        "stdio examples must set MCP_TRANSPORT=stdio explicitly."
    )


def test_dockerfile_port_matches_fly_internal_port() -> None:
    """The image's baked-in MCP_PORT, when present, must match the Fly proxy.

    Same drift class as the port guard above: the Dockerfile overrides the
    Settings default, so fly.toml has to agree with the image, not just the
    model. If the Dockerfile stops setting MCP_PORT, the Settings-default
    guard already covers the fallback.
    """
    image_port = _dockerfile_env().get("MCP_PORT")
    if image_port is None:
        return
    internal_port = _fly_config()["http_service"]["internal_port"]
    assert int(image_port) == internal_port, (
        f"Dockerfile ENV MCP_PORT={image_port} but fly.toml "
        f"http_service.internal_port is {internal_port}. The container would "
        f"bind a different port than the Fly proxy forwards to (#88)."
    )
