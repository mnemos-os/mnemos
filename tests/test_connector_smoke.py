"""End-to-end MCP smoke tests for documented connector surfaces.

These tests exercise the real MCP transports while keeping the backend
database-free. Tool calls are pointed at a tiny HTTP mock that behaves like
the MNEMOS REST API for the benign ``search_memories`` call.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import anyio
import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
CONNECTOR_DIR = REPO_ROOT / "docs" / "connectors"
BACKEND_TOKEN = "connector-smoke-backend-token"
MCP_EDGE_TOKEN = "connector-smoke-mcp-token"
SEARCH_QUERY = "connector smoke"
TRANSPORT_TIMEOUT_SECONDS = 10.0

STDIO_SURFACES = [
    ("claude-code", CONNECTOR_DIR / "claude-code.md"),
    ("claude-desktop", CONNECTOR_DIR / "claude-desktop.md"),
    ("cursor", CONNECTOR_DIR / "cursor.md"),
    ("codex-cli", CONNECTOR_DIR / "codex-cli.md"),
    ("continue-dev", CONNECTOR_DIR / "continue-dev.md"),
    ("cline", CONNECTOR_DIR / "cline.md"),
]
HTTP_SURFACES = [
    ("chatgpt-pro-developer-mode", CONNECTOR_DIR / "chatgpt-pro-developer-mode.md"),
]


class _MockMnemosServer(ThreadingHTTPServer):
    requests: list[dict[str, Any]]


class _MockMnemosHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - stdlib hook name
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw_body = self.rfile.read(length)
        try:
            body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except json.JSONDecodeError:
            body = {"_invalid_json": raw_body.decode("utf-8", errors="replace")}

        self.server.requests.append(  # type: ignore[attr-defined]
            {
                "method": "POST",
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": body,
            }
        )

        if self.path == "/v1/memories/search":
            self._send_json(
                {
                    "success": True,
                    "result": {
                        "count": 0,
                        "memories": [],
                    },
                }
            )
            return

        self._send_json({"success": False, "error": f"unexpected path: {self.path}"}, status=404)

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


@contextlib.contextmanager
def _mock_mnemos_backend() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    try:
        server = _MockMnemosServer(("127.0.0.1", 0), _MockMnemosHandler)
    except PermissionError as exc:
        pytest.skip(f"loopback bind unavailable for connector smoke mock backend: {exc}")
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}", server.requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _extract_fenced_blocks(text: str, language: str) -> list[str]:
    pattern = re.compile(rf"```{re.escape(language)}\n(.*?)```", re.DOTALL)
    return [match.group(1) for match in pattern.finditer(text)]


def _first_stdio_config(doc_path: Path) -> dict[str, Any]:
    text = doc_path.read_text(encoding="utf-8")

    for block in _extract_fenced_blocks(text, "json"):
        parsed = json.loads(block)
        if not isinstance(parsed, dict):
            continue
        servers = parsed.get("mcpServers")
        if not isinstance(servers, dict):
            continue
        for cfg in servers.values():
            if (
                isinstance(cfg, dict)
                and cfg.get("command") == "mnemos"
                and cfg.get("args", [])[:2] == ["serve", "mcp-stdio"]
            ):
                return cfg

    for block in _extract_fenced_blocks(text, "toml"):
        try:
            parsed = tomllib.loads(block)
        except tomllib.TOMLDecodeError:
            parsed = {}
        servers = parsed.get("mcp", {}).get("servers", {}) if isinstance(parsed, dict) else {}
        cfg = servers.get("mnemos") if isinstance(servers, dict) else None
        if isinstance(cfg, dict):
            if cfg.get("command") == "mnemos" and cfg.get("args", [])[:2] == ["serve", "mcp-stdio"]:
                return cfg
        command_match = re.search(r'command\s*=\s*"mnemos"', block)
        args_match = re.search(r'args\s*=\s*(\[[^\]]+\])', block)
        if command_match and args_match and json.loads(args_match.group(1))[:2] == ["serve", "mcp-stdio"]:
            return {"command": "mnemos", "args": ["serve", "mcp-stdio"], "env": {}}

    raise AssertionError(f"{doc_path.name} has no mnemos serve mcp-stdio config")


def _http_command_args_from_doc(doc_path: Path) -> list[str]:
    text = doc_path.read_text(encoding="utf-8")
    match = re.search(r"command:\s*(\[[^\n]+\])", text)
    if not match:
        raise AssertionError(f"{doc_path.name} has no docker command list")
    command = json.loads(match.group(1))
    if command[:3] != ["mnemos", "serve", "mcp-http"]:
        raise AssertionError(f"{doc_path.name} command does not launch mnemos serve mcp-http: {command!r}")
    return command[1:]


def _write_empty_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "mnemos-smoke.toml"
    config_path.write_text("", encoding="utf-8")
    return config_path


def _base_env(base_url: str, config_path: Path) -> dict[str, str]:
    pythonpath = str(REPO_ROOT)
    if os.environ.get("PYTHONPATH"):
        pythonpath = f"{pythonpath}{os.pathsep}{os.environ['PYTHONPATH']}"
    return {
        "MNEMOS_BASE": base_url,
        "MNEMOS_API_KEY": BACKEND_TOKEN,
        "MNEMOS_CONFIG_PATH": str(config_path),
        "PYTHONPATH": pythonpath,
        "RATE_LIMIT_ENABLED": "false",
        "RATE_LIMIT_STORAGE_URI": "memory://",
    }


def _safe_process_env() -> dict[str, str]:
    return {
        key: os.environ[key]
        for key in ("HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER")
        if key in os.environ
    }


def _stdio_server_params(cfg: dict[str, Any], base_url: str, config_path: Path):
    from mcp.client.stdio import StdioServerParameters

    args = list(cfg["args"])
    env = dict(cfg.get("env") or {})
    env.update(_base_env(base_url, config_path))
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "mnemos.cli.main", *args],
        env=env,
        cwd=REPO_ROOT,
    )


def _assert_canonical_tool_registry(tools: list[Any]) -> None:
    from mnemos.mcp.tools import TOOL_REGISTRY

    expected = set(TOOL_REGISTRY)
    observed = {tool.name for tool in tools}
    assert observed == expected
    assert all(tool.inputSchema["type"] == "object" for tool in tools)


def _assert_search_payload(call_result: Any) -> dict[str, Any]:
    assert call_result.isError is False
    assert len(call_result.content) == 1
    content = call_result.content[0]
    assert content.type == "text"
    payload = json.loads(content.text)
    assert isinstance(payload.get("success"), bool)
    assert payload["success"] is True
    assert "result" in payload
    assert "error" not in payload
    assert isinstance(payload["result"], dict)
    return payload


def _assert_backend_search_request(requests: list[dict[str, Any]]) -> None:
    assert requests == [
        {
            "method": "POST",
            "path": "/v1/memories/search",
            "authorization": f"Bearer {BACKEND_TOKEN}",
            "body": {"query": SEARCH_QUERY, "limit": 1},
        }
    ]


async def _run_stdio_smoke(server_params: Any) -> dict[str, Any]:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import stdio_client

    with anyio.fail_after(TRANSPORT_TIMEOUT_SECONDS):
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(seconds=TRANSPORT_TIMEOUT_SECONDS),
            ) as session:
                await session.initialize()
                tools_result = await session.list_tools()
                _assert_canonical_tool_registry(tools_result.tools)
                call_result = await session.call_tool(
                    "search_memories",
                    {"query": SEARCH_QUERY, "limit": 1},
                    read_timeout_seconds=timedelta(seconds=TRANSPORT_TIMEOUT_SECONDS),
                )
                return _assert_search_payload(call_result)


@pytest.mark.parametrize("surface,doc_path", STDIO_SURFACES, ids=[name for name, _path in STDIO_SURFACES])
def test_documented_stdio_connector_smoke(surface: str, doc_path: Path, tmp_path: Path) -> None:
    cfg = _first_stdio_config(doc_path)
    assert surface
    with _mock_mnemos_backend() as (base_url, requests):
        config_path = _write_empty_config(tmp_path)
        payload = anyio.run(_run_stdio_smoke, _stdio_server_params(cfg, base_url, config_path))

    assert payload["result"]["count"] == 0
    _assert_backend_search_request(requests)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", 0))
        except PermissionError as exc:
            pytest.skip(f"loopback bind unavailable for connector HTTP/SSE smoke: {exc}")
        return int(sock.getsockname()[1])


def _read_http(path: str) -> bytes:
    with urllib.request.urlopen(path, timeout=0.5) as response:
        return response.read()


def _process_output(proc: subprocess.Popen[str]) -> str:
    try:
        stdout, stderr = proc.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        return "<process still running>"
    return f"stdout:\n{stdout}\nstderr:\n{stderr}"


def _wait_for_http_ready(proc: subprocess.Popen[str], base_url: str) -> None:
    deadline = time.monotonic() + TRANSPORT_TIMEOUT_SECONDS
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"mcp-http exited before readiness\n{_process_output(proc)}")
        try:
            if _read_http(f"{base_url}/healthz") == b"ok":
                return
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
        time.sleep(0.1)
    raise AssertionError(f"mcp-http did not become ready: {last_error!r}")


def _stop_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)


async def _run_sse_smoke(base_url: str) -> dict[str, Any]:
    from mcp.client.session import ClientSession
    from mcp.client.sse import sse_client

    with anyio.fail_after(TRANSPORT_TIMEOUT_SECONDS):
        async with sse_client(
            f"{base_url}/sse",
            headers={"Authorization": f"Bearer {MCP_EDGE_TOKEN}"},
            timeout=2,
            sse_read_timeout=TRANSPORT_TIMEOUT_SECONDS,
        ) as (read_stream, write_stream):
            async with ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(seconds=TRANSPORT_TIMEOUT_SECONDS),
            ) as session:
                await session.initialize()
                tools_result = await session.list_tools()
                _assert_canonical_tool_registry(tools_result.tools)
                call_result = await session.call_tool(
                    "search_memories",
                    {"query": SEARCH_QUERY, "limit": 1},
                    read_timeout_seconds=timedelta(seconds=TRANSPORT_TIMEOUT_SECONDS),
                )
                return _assert_search_payload(call_result)


@pytest.mark.parametrize("surface,doc_path", HTTP_SURFACES, ids=[name for name, _path in HTTP_SURFACES])
def test_documented_http_sse_connector_smoke(surface: str, doc_path: Path, tmp_path: Path) -> None:
    args = _http_command_args_from_doc(doc_path)
    assert surface
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    with _mock_mnemos_backend() as (backend_url, requests):
        config_path = _write_empty_config(tmp_path)
        env = _safe_process_env()
        env.update(_base_env(backend_url, config_path))
        env["MNEMOS_MCP_TOKENS"] = f"smoke:{MCP_EDGE_TOKEN}:{BACKEND_TOKEN}"
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "mnemos.cli.main",
                *args[:2],
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _wait_for_http_ready(proc, base_url)
            payload = anyio.run(_run_sse_smoke, base_url)
        finally:
            _stop_process(proc)

    assert payload["result"]["count"] == 0
    _assert_backend_search_request(requests)
