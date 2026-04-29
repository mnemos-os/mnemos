"""Unified MNEMOS command line interface."""

import asyncio
import inspect
import json
import os
import sys
from contextlib import contextmanager
from enum import Enum
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import httpx
import typer

from mnemos.core.config import get_settings, reload_settings


def _patch_typer_click_compat() -> None:
    """Keep Typer 0.9 usable with newer Click releases in older envs."""
    try:
        from click.core import Parameter
        from typer.core import TyperArgument, TyperOption

        if len(inspect.signature(Parameter.make_metavar).parameters) == 2:
            original_parameter_make_metavar = Parameter.make_metavar

            if not getattr(Parameter.make_metavar, "_mnemos_click_compat", False):

                def parameter_make_metavar(self: Any, ctx: Any = None) -> str:
                    return original_parameter_make_metavar(self, ctx)

                parameter_make_metavar._mnemos_click_compat = True
                Parameter.make_metavar = parameter_make_metavar

        if len(inspect.signature(TyperArgument.make_metavar).parameters) == 1:

            def make_metavar(self: Any, ctx: Any = None) -> str:
                if self.metavar is not None:
                    return self.metavar
                var = (self.name or "").upper()
                if not self.required:
                    var = f"[{var}]"
                try:
                    type_var = self.type.get_metavar(self, ctx)
                except TypeError:
                    type_var = self.type.get_metavar(self)
                if type_var:
                    var += f":{type_var}"
                if self.nargs != 1:
                    var += "..."
                return var

            TyperArgument.make_metavar = make_metavar

        original_option_init = TyperOption.__init__

        if not getattr(TyperOption.__init__, "_mnemos_click_compat", False):
            sig = inspect.signature(original_option_init)
            params = sig.parameters
            accepts_var_kwargs = any(
                param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()
            )
            accepted_names = set(params.keys())

            def option_init(self: Any, **kwargs: Any) -> None:
                if kwargs.get("is_flag") is True and kwargs.get("flag_value") is None:
                    kwargs["type"] = None
                    if accepts_var_kwargs or "flag_value" in accepted_names:
                        kwargs["flag_value"] = True
                if (
                    kwargs.get("is_flag") is None
                    and kwargs.get("flag_value") is None
                    and not kwargs.get("count", False)
                ):
                    kwargs["is_flag"] = False
                if not accepts_var_kwargs:
                    kwargs = {key: value for key, value in kwargs.items() if key in accepted_names}
                original_option_init(self, **kwargs)

            option_init._mnemos_click_compat = True
            TyperOption.__init__ = option_init

        if len(inspect.signature(TyperOption.make_metavar).parameters) == 2:
            original_option_make_metavar = TyperOption.make_metavar

            def option_make_metavar(self: Any, ctx: Any = None) -> str:
                return original_option_make_metavar(self, ctx)

            TyperOption.make_metavar = option_make_metavar
    except Exception:
        return


_patch_typer_click_compat()


class ExportFormat(str, Enum):
    mpf = "mpf"
    jsonl = "jsonl"
    markdown = "markdown"
    html = "html"
    text = "text"


class ImportSource(str, Enum):
    mpf = "mpf"
    mem0 = "mem0"
    letta = "letta"
    graphiti = "graphiti"
    cognee = "cognee"
    mempalace = "mempalace"
    docling = "docling"


class ConsultMode(str, Enum):
    auto = "auto"
    consensus = "consensus"
    debate = "debate"
    single = "single"


class DeploymentProfile(str, Enum):
    server = "server"
    edge = "edge"
    dev = "dev"


EXPORT_DISPATCH: dict[str, str] = {
    "mpf": "mnemos.tools.memory_export",
    "jsonl": "mnemos.tools.memory_export",
    "markdown": "mnemos.tools.export_memories_for_docling",
    "html": "mnemos.tools.export_memories_for_docling",
    "text": "mnemos.tools.export_memories_for_docling",
}

IMPORT_DISPATCH: dict[str, str] = {
    "mpf": "mnemos.tools.memory_import",
    "docling": "mnemos.tools.docling_import",
    "mem0": "mnemos.tools.adapters.mem0",
    "letta": "mnemos.tools.adapters.letta",
    "graphiti": "mnemos.tools.adapters.graphiti",
    "cognee": "mnemos.tools.adapters.cognee",
    "mempalace": "mnemos.tools.adapters.mempalace",
}

_EXPORT_TOOL_SUBCOMMANDS: dict[ExportFormat, str] = {
    ExportFormat.mpf: "json",
    ExportFormat.jsonl: "jsonl",
    ExportFormat.markdown: "markdown",
    ExportFormat.html: "html",
    ExportFormat.text: "text",
}

app = typer.Typer(help="Unified MNEMOS command line interface.", no_args_is_help=True)
serve_app = typer.Typer(
    help="Run MNEMOS API and MCP servers.",
    invoke_without_command=True,
    no_args_is_help=False,
)
worker_app = typer.Typer(help="Run MNEMOS background workers.", no_args_is_help=True)


def _version_option_callback(value: bool) -> None:
    if not value:
        return
    from mnemos._version import __version__

    typer.echo(__version__)
    raise typer.Exit()


@app.callback()
def cli(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_option_callback,
        is_eager=True,
        help="Print the installed MNEMOS version.",
    ),
) -> None:
    """Unified MNEMOS command line interface."""


@contextmanager
def _patched_argv(prog: str, argv: Sequence[str]):
    original = sys.argv[:]
    sys.argv = [prog, *argv]
    try:
        yield
    finally:
        sys.argv = original


def _main_accepts_argv(main: Callable[..., Any]) -> bool:
    try:
        signature = inspect.signature(main)
    except (TypeError, ValueError):
        return False
    return bool(signature.parameters)


def _raise_for_int_result(result: Any) -> None:
    if isinstance(result, int):
        raise typer.Exit(result)


def _run_module_main(module_name: str, argv: Sequence[str] = (), *, prog: Optional[str] = None) -> None:
    module = import_module(module_name)
    main = getattr(module, "main")

    if inspect.iscoroutinefunction(main):
        if argv:
            raise typer.BadParameter(f"{module_name}.main() does not accept argv")
        _raise_for_int_result(asyncio.run(main()))
        return

    if _main_accepts_argv(main):
        _raise_for_int_result(main(list(argv)))
        return

    with _patched_argv(prog or module_name, argv):
        _raise_for_int_result(main())


def _run_async_module_main(module_name: str) -> None:
    module = import_module(module_name)
    _raise_for_int_result(asyncio.run(module.main()))


def _api_env(require_key: bool) -> tuple[str, dict[str, str]]:
    settings = get_settings().server
    base = settings.base if settings.base_configured else ""
    api_key = settings.api_key

    if not base:
        typer.echo(
            "ERROR: MNEMOS_BASE is not set. Example: export MNEMOS_BASE=http://localhost:5002",
            err=True,
        )
        raise typer.Exit(2)
    if require_key and not api_key:
        typer.echo(
            "ERROR: MNEMOS_API_KEY is not set. Export a bearer token for this command.",
            err=True,
        )
        raise typer.Exit(2)

    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return base.rstrip("/"), headers


def _append_env_endpoint(argv: list[str]) -> None:
    settings = get_settings().server
    base = settings.base if settings.base_configured else ""
    api_key = settings.api_key
    if base:
        argv.extend(["--endpoint", base])
    if api_key:
        argv.extend(["--api-key", api_key])


def _has_adapter_target(args: Sequence[str]) -> bool:
    for arg in args:
        if arg in {"--out", "--post"} or arg.startswith("--out=") or arg.startswith("--post="):
            return True
    return False


def _append_adapter_target(argv: list[str], extra_args: Sequence[str]) -> None:
    if _has_adapter_target(extra_args):
        return
    base, headers = _api_env(require_key=True)
    token = headers["Authorization"].removeprefix("Bearer ")
    argv.extend(["--post", base, "--api-key", token])


def _echo_json_response(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        typer.echo(f"ERROR: HTTP {exc.response.status_code}: {exc.response.text[:500]}", err=True)
        raise typer.Exit(1) from exc

    try:
        payload = response.json()
    except ValueError:
        typer.echo(response.text)
        return
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


def _infer_import_source(source: Path) -> ImportSource:
    if source.suffix.lower() in {".mpf", ".json", ".jsonl"}:
        return ImportSource.mpf
    typer.echo(
        "ERROR: Could not infer import source. Pass --from mpf, mem0, letta, graphiti, cognee, "
        "mempalace, or docling.",
        err=True,
    )
    raise typer.Exit(2)


def _parse_export_format(format_name: str) -> ExportFormat:
    try:
        return ExportFormat(format_name.lower())
    except ValueError as exc:
        valid = ", ".join(item.value for item in ExportFormat)
        typer.echo(f"ERROR: --format must be one of: {valid}", err=True)
        raise typer.Exit(2) from exc


def _parse_import_source(source_name: Optional[str], source: Path) -> ImportSource:
    if source_name is None:
        return _infer_import_source(source)
    try:
        return ImportSource(source_name.lower())
    except ValueError as exc:
        valid = ", ".join(item.value for item in ImportSource)
        typer.echo(f"ERROR: --from must be one of: {valid}", err=True)
        raise typer.Exit(2) from exc


def _parse_consult_mode(mode: str) -> ConsultMode:
    try:
        return ConsultMode(mode.lower())
    except ValueError as exc:
        valid = ", ".join(item.value for item in ConsultMode)
        typer.echo(f"ERROR: --mode must be one of: {valid}", err=True)
        raise typer.Exit(2) from exc


def _apply_profile_flag(profile: Optional[DeploymentProfile]) -> None:
    if profile is None:
        return
    os.environ["MNEMOS_PROFILE_OVERRIDE"] = profile.value
    os.environ["MNEMOS_PROFILE"] = profile.value
    reload_settings()


def _adapter_source_args(import_from: ImportSource, source: Path) -> list[str]:
    source_text = str(source)

    if import_from == ImportSource.mem0:
        if source_text.startswith(("http://", "https://")):
            return ["--qdrant-url", source_text]
        return ["--qdrant-path", source_text]

    if import_from == ImportSource.letta:
        if source_text.startswith(("http://", "https://")):
            return ["--mode", "server", "--base", source_text]
        return ["--mode", "sqlite", "--db", source_text]

    if import_from == ImportSource.graphiti:
        if source.exists() and source.is_dir():
            return ["--backend", "kuzu", "--kuzu-db", source_text]
        return ["--neo4j", source_text]

    if import_from == ImportSource.cognee:
        return ["--dataset", source_text]

    if import_from == ImportSource.mempalace:
        return ["--palace", source_text]

    raise typer.BadParameter(f"Unsupported adapter source: {import_from.value}")


@serve_app.callback()
def serve(
    ctx: typer.Context,
    host: str = typer.Option("0.0.0.0", "--host", help="API bind address.", is_flag=False),
    port: int = typer.Option(5002, "--port", help="API listen port.", is_flag=False),
    profile: Optional[DeploymentProfile] = typer.Option(
        None,
        "--profile",
        help="Deployment profile: server, edge, or dev.",
        is_flag=False,
    ),
    workers: Optional[int] = typer.Option(
        None,
        "--workers",
        is_flag=False,
        help="Uvicorn worker count. Defaults to MNEMOS_WORKERS (1).",
    ),
) -> None:
    """Run the MNEMOS FastAPI server."""
    if ctx.invoked_subcommand is not None:
        return

    _apply_profile_flag(profile)

    import uvicorn

    worker_count = workers if workers is not None else get_settings().server.workers
    uvicorn.run("mnemos.api.main:app", host=host, port=port, workers=worker_count)


@serve_app.command("mcp-stdio")
def serve_mcp_stdio() -> None:
    """Run the stdio MCP transport for Claude Code, OpenClaw, and ZeroClaw."""
    _run_async_module_main("mnemos.mcp.stdio")


@serve_app.command("mcp-http")
def serve_mcp_http(
    host: str = typer.Option("0.0.0.0", "--host", help="MCP HTTP bind address.", is_flag=False),
    port: int = typer.Option(5003, "--port", help="MCP HTTP listen port.", is_flag=False),
) -> None:
    """Run the HTTP/SSE MCP transport."""
    _run_module_main(
        "mnemos.mcp.http",
        ["--host", host, "--port", str(port)],
        prog="mnemos serve mcp-http",
    )


@worker_app.command("distillation")
def worker_distillation() -> None:
    """Run the compression contest distillation worker."""
    _run_async_module_main("mnemos.workers.distillation")


@app.command()
def install(
    agent: bool = typer.Option(False, "--agent", help="LLM-guided installation."),
    wizard: bool = typer.Option(False, "--wizard", help="Traditional interactive wizard."),
    unattended: bool = typer.Option(False, "--unattended", help="Non-interactive environment-driven install."),
    upgrade: bool = typer.Option(False, "--upgrade", help="Re-run migrations only."),
    check: bool = typer.Option(False, "--check", help="Run environment checks only."),
    profile: Optional[DeploymentProfile] = typer.Option(
        None,
        "--profile",
        help="Deployment profile: server, edge, or dev.",
        is_flag=False,
    ),
) -> None:
    """Run the MNEMOS install wizard."""
    selected_modes = sum(bool(value) for value in (agent, wizard, unattended))
    if selected_modes > 1:
        typer.echo("ERROR: choose only one of --agent, --wizard, or --unattended.", err=True)
        raise typer.Exit(2)

    argv: list[str] = []
    if agent:
        argv.append("--agent")
    if wizard:
        argv.append("--wizard")
    if unattended:
        argv.append("--unattended")
    if upgrade:
        argv.append("--upgrade")
    if check:
        argv.append("--check")
    if profile is not None:
        argv.extend(["--profile", profile.value])

    _run_module_main("mnemos.installer.__main__", argv, prog="mnemos install")


@app.command("export")
def export_memories(
    format_: str = typer.Option(
        "mpf",
        "--format",
        help="Export format: mpf, jsonl, markdown, html, or text.",
        is_flag=False,
    ),
    out: Path = typer.Option(..., "--out", help="Output file path.", is_flag=False),
    owner_id: Optional[str] = typer.Option(None, "--owner-id", help="Filter by owner id.", is_flag=False),
    namespace: Optional[str] = typer.Option(None, "--namespace", help="Filter by namespace.", is_flag=False),
) -> None:
    """Export memories through the existing portability tools."""
    export_format = _parse_export_format(format_)
    argv = [_EXPORT_TOOL_SUBCOMMANDS[export_format], "--out", str(out)]
    if owner_id:
        argv.extend(["--owner-id", owner_id])
    if namespace:
        argv.extend(["--namespace", namespace])
    _append_env_endpoint(argv)
    _run_module_main("mnemos.tools.memory_export", argv)


@app.command("import")
def import_memories(
    source: Path = typer.Argument(..., help="Source file, directory, endpoint, or foreign-system locator."),
    import_from: Optional[str] = typer.Option(None, "--from", help="Source system.", is_flag=False),
    owner_id: Optional[str] = typer.Option(
        None,
        "--owner-id",
        help="Override imported owner id when supported.",
        is_flag=False,
    ),
    namespace: Optional[str] = typer.Option(
        None,
        "--namespace",
        help="Override imported namespace when supported.",
        is_flag=False,
    ),
    preserve_owner: bool = typer.Option(False, "--preserve-owner", help="Preserve source MPF ownership metadata."),
) -> None:
    """Import memories through the existing portability tools and adapters."""
    selected_source = _parse_import_source(import_from, source)
    extra_args: list[str] = []

    if selected_source == ImportSource.mpf:
        argv = ["json", "--file", str(source)]
        if source.suffix.lower() == ".jsonl":
            argv.append("--jsonl")
        if preserve_owner:
            argv.append("--preserve-metadata")
        if owner_id:
            argv.extend(["--owner-id", owner_id])
        if namespace:
            argv.extend(["--namespace", namespace])
        _append_env_endpoint(argv)
        argv.extend(extra_args)
        _run_module_main(IMPORT_DISPATCH[selected_source.value], argv)
        return

    if selected_source == ImportSource.docling:
        argv = ["--source" if source.exists() and source.is_dir() else "--file", str(source)]
        if owner_id:
            argv.extend(["--owner-id", owner_id])
        if namespace:
            argv.extend(["--namespace", namespace])
        _append_env_endpoint(argv)
        argv.extend(extra_args)
        _run_module_main(IMPORT_DISPATCH[selected_source.value], argv)
        return

    argv = _adapter_source_args(selected_source, source)
    _append_adapter_target(argv, extra_args)
    argv.extend(extra_args)
    _run_module_main(IMPORT_DISPATCH[selected_source.value], argv)


@app.command("validate-mpf")
def validate_mpf(envelope_path: Path = typer.Argument(..., help="MPF envelope path.")) -> None:
    """Validate an MPF envelope."""
    _run_module_main("mnemos.tools.mpf_validate", ["--file", str(envelope_path)])


@app.command()
def consult(
    prompt: str = typer.Argument(..., help="Prompt to send to GRAEAE."),
    mode: str = typer.Option(
        "auto",
        "--mode",
        help="Consultation mode: auto, consensus, debate, or single.",
        is_flag=False,
    ),
    task_type: Optional[str] = typer.Option(None, "--task-type", help="Task type for routing.", is_flag=False),
) -> None:
    """Create a consultation against the configured MNEMOS API."""
    base, headers = _api_env(require_key=True)
    consult_mode = _parse_consult_mode(mode)
    payload: dict[str, Any] = {"prompt": prompt, "mode": consult_mode.value}
    if task_type:
        payload["task_type"] = task_type

    try:
        response = httpx.post(f"{base}/v1/consultations", json=payload, headers=headers, timeout=120)
    except httpx.RequestError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(1) from exc
    _echo_json_response(response)


@app.command()
def health() -> None:
    """Check the configured MNEMOS API health endpoint."""
    base, headers = _api_env(require_key=False)
    try:
        response = httpx.get(f"{base}/health", headers=headers, timeout=30)
    except httpx.RequestError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(1) from exc
    _echo_json_response(response)


@app.command()
def version() -> None:
    """Print the installed MNEMOS version."""
    from mnemos._version import __version__

    typer.echo(__version__)


app.add_typer(serve_app, name="serve")
app.add_typer(worker_app, name="worker")


if __name__ == "__main__":
    app()
