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

import shlex
import sys
from pathlib import Path

import pytest

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


def _logical_lines(text: str) -> list[str]:
    """Dockerfile lines with backslash continuations joined (#90 AC2).

    Comment and blank lines are dropped BEFORE joining, per Docker: a
    comment ending in '\\' does not swallow the next instruction.
    """
    lines: list[str] = []
    buf = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        buf = f"{buf} {stripped}" if buf else stripped
        if buf.endswith("\\"):
            buf = buf[:-1].rstrip()
        else:
            lines.append(buf)
            buf = ""
    if buf:
        lines.append(buf)
    return lines


def _unquote(value: str) -> str:
    """Strip one layer of surrounding single/double quotes (#90 AC3)."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _dockerfile_env() -> dict[str, str]:
    """The image's baked-in ENV pairs, in both Docker grammars (#90).

    Continuations are joined first, then each ENV instruction branches on
    its first token: containing '=' means the key=value grammar (multiple
    pairs per instruction, shlex handles quoting); otherwise the legacy
    `ENV key value` grammar — exactly one pair whose value is the whole
    remainder of the line, spaces and '=' included.
    """
    env: dict[str, str] = {}
    for line in _logical_lines(DOCKERFILE.read_text(encoding="utf-8")):
        parts = line.split(None, 1)
        # Dockerfile instruction keywords are case-insensitive per the
        # Dockerfile reference, so match `ENV` against any case.
        if len(parts) != 2 or parts[0].upper() != "ENV":
            continue
        tokens = parts[1].split(None, 1)
        if "=" in tokens[0]:
            for pair in shlex.split(parts[1]):
                key, sep, value = pair.partition("=")
                if sep:
                    env[key] = value
        elif len(tokens) == 2:
            env[tokens[0]] = _unquote(tokens[1].strip())
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


# ---------------------------------------------------------------------------
# #90 regression lock: the pre-#90 parser only understood single-line
# `ENV key=value` pairs, so legacy `ENV key value`, continuation lines, and
# quoted values silently escaped BOTH drift guards above (the #88 failure
# mode: image defaults to stdio / wrong port with a green suite).
# ---------------------------------------------------------------------------


def _patch_dockerfile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str) -> None:
    """Point the guards' DOCKERFILE at a fixture instead of the shipped file."""
    fixture = tmp_path / "Dockerfile"
    fixture.write_text(text, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "DOCKERFILE", fixture)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # key=value grammar (existing behavior must not regress)
        ("ENV MCP_PORT=8080\n", {"MCP_PORT": "8080"}),
        ("ENV A=1 B=2\n", {"A": "1", "B": "2"}),
        ("ENV MCP_PORT=8080", {"MCP_PORT": "8080"}),  # no trailing newline
        # AC3: surrounding quotes stripped, both quote styles, both grammars
        ('ENV MCP_PORT="8080"\n', {"MCP_PORT": "8080"}),
        ("ENV MCP_PORT='8080'\n", {"MCP_PORT": "8080"}),
        ('ENV MCP_PORT "8080"\n', {"MCP_PORT": "8080"}),
        ('ENV A="hello world"\n', {"A": "hello world"}),
        # AC1: legacy grammar — one pair, value = rest of the line, trimmed
        ("ENV MCP_PORT 8080\n", {"MCP_PORT": "8080"}),
        ("ENV MOTD hello world\n", {"MOTD": "hello world"}),
        ("ENV OPTS a=b c=d\n", {"OPTS": "a=b c=d"}),  # '=' in value stays legacy
        # AC2: backslash continuations joined for BOTH grammars
        ("ENV MCP_PORT \\\n    8080\n", {"MCP_PORT": "8080"}),
        ("ENV A=1 \\\n    B=2\n", {"A": "1", "B": "2"}),
        ('ENV MCP_PORT \\\n    "8080"\n', {"MCP_PORT": "8080"}),
        # comments must not hide instructions (even a comment ending in '\')
        ("# ENV GHOST=1\nENV MCP_PORT=8080\n", {"MCP_PORT": "8080"}),
        ("# comment ends in backslash \\\nENV MCP_PORT=8080\n", {"MCP_PORT": "8080"}),
        # instruction keyword is case-insensitive per the Dockerfile reference —
        # lowercase/mixed `env` must be parsed identically to uppercase, in BOTH
        # grammars (the #88/#90 drift guards must never silently skip these).
        ("env MCP_PORT=8080\n", {"MCP_PORT": "8080"}),
        ("Env A=1 B=2\n", {"A": "1", "B": "2"}),
        ('eNV MCP_PORT="8080"\n', {"MCP_PORT": "8080"}),  # key=value + quotes
        ("enV MCP_PORT=8080\n", {"MCP_PORT": "8080"}),
        ("enV MCP_PORT 8080\n", {"MCP_PORT": "8080"}),  # legacy grammar
        ("eNv MOTD hello world\n", {"MOTD": "hello world"}),
        ("Env MCP_PORT \\\n    8080\n", {"MCP_PORT": "8080"}),  # legacy + continuation
    ],
)
def test_dockerfile_env_parses_both_grammars(
    text: str, expected: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_dockerfile(monkeypatch, tmp_path, text)
    assert _dockerfile_env() == expected


# Fixture Dockerfiles setting MCP_TRANSPORT in the syntaxes the old parser
# missed. {port} snippets below are formatted against fly.toml's actual
# internal_port so the fixtures never hard-code a second 8080 (same doctrine
# as the guards themselves).
_TRANSPORT_DRIFT_SNIPPETS = [
    "ENV MCP_TRANSPORT streamable-http\n",  # legacy
    "ENV MCP_TRANSPORT \\\n    streamable-http\n",  # legacy + continuation
    "ENV MCP_HOST=0.0.0.0 \\\n    MCP_TRANSPORT=streamable-http\n",  # continuation pairs
]

_PORT_SNIPPETS = [
    "ENV MCP_PORT {port}\n",  # legacy
    "ENV MCP_PORT \\\n    {port}\n",  # legacy + continuation
    'ENV MCP_PORT "{port}"\n',  # legacy + quotes
    'ENV MCP_PORT="{port}"\n',  # key=value + quotes
    "ENV MCP_HOST=0.0.0.0 \\\n    MCP_PORT={port}\n",  # continuation pairs
]


@pytest.mark.parametrize("snippet", _TRANSPORT_DRIFT_SNIPPETS)
def test_transport_guard_rejects_drift_in_missed_syntaxes(
    snippet: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4: the transport guard must FAIL on legacy/continuation MCP_TRANSPORT.

    Pre-#90 the parser returned {} for these fixtures and the guard passed.
    """
    _patch_dockerfile(monkeypatch, tmp_path, snippet)
    with pytest.raises(AssertionError, match="MCP_TRANSPORT"):
        test_dockerfile_does_not_set_mcp_transport()


@pytest.mark.parametrize("snippet", _PORT_SNIPPETS)
def test_port_guard_rejects_drift_in_missed_syntaxes(
    snippet: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4: the port guard must FAIL when the image port disagrees with fly.toml.

    Pre-#90 legacy/continuation fixtures false-passed via the image_port is
    None early return, and quoted values raised ValueError instead of the
    guard's AssertionError.
    """
    drifted_port = _fly_config()["http_service"]["internal_port"] + 1
    _patch_dockerfile(monkeypatch, tmp_path, snippet.format(port=drifted_port))
    with pytest.raises(AssertionError, match="MCP_PORT"):
        test_dockerfile_port_matches_fly_internal_port()


@pytest.mark.parametrize("snippet", _PORT_SNIPPETS)
def test_guards_pass_on_matching_port_in_all_syntaxes(
    snippet: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4: a correctly configured image passes in every supported syntax.

    The shipped-file PASS case is covered by the four guards above running
    against the real Dockerfile/fly.toml in the normal suite.
    """
    matching_port = _fly_config()["http_service"]["internal_port"]
    _patch_dockerfile(monkeypatch, tmp_path, snippet.format(port=matching_port))
    test_dockerfile_port_matches_fly_internal_port()
    test_dockerfile_does_not_set_mcp_transport()
