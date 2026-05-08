"""Unified MNEMOS command line interface."""

import asyncio
import inspect
import json
import sys
from contextlib import contextmanager
from enum import Enum
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import httpx
import typer

from mnemos.core.config import get_settings, set_profile_override


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


class DeletionRequestWorkerPhase(str, Enum):
    soft_delete = "soft_delete"
    hard_delete = "hard_delete"


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
artemis_app = typer.Typer(help="ARTEMIS corpus maintenance commands.", no_args_is_help=True)
morpheus_app = typer.Typer(help="MORPHEUS run maintenance commands.", no_args_is_help=True)


DOC_IMPORT_EXTENSIONS = {
    ".md",
    ".markdown",
    ".rst",
    ".txt",
    ".pdf",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    ".html",
    ".htm",
}


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


def _print_dedup_markdown(payload: dict[str, Any]) -> None:
    typer.echo("# ARTEMIS duplicate-content sweep")
    typer.echo("")
    typer.echo(f"- Namespace: {payload.get('namespace') or '*'}")
    typer.echo(f"- Groups: {payload['group_count']}")
    typer.echo(f"- Duplicate rows: {payload['duplicate_count']}")
    typer.echo(f"- Auto-merge: {'yes' if payload['auto_merge'] else 'no'}")
    if payload["auto_merge"]:
        typer.echo(f"- Rows consolidated: {payload['merged_count']}")
    if not payload["groups"]:
        return
    typer.echo("")
    typer.echo("| owner_id | namespace | canonical_id | duplicates | content_hash |")
    typer.echo("| --- | --- | --- | ---: | --- |")
    for group in payload["groups"]:
        typer.echo(
            "| {owner_id} | {namespace} | {canonical_id} | {count} | `{hash}` |".format(
                owner_id=group["owner_id"],
                namespace=group["namespace"],
                canonical_id=group["canonical_id"],
                count=len(group["duplicate_ids"]),
                hash=group["content_hash"][:16],
            )
        )


async def _open_cli_persistence_backend():
    """Open a backend for one-shot CLI maintenance commands."""
    import asyncpg

    import mnemos.core.lifecycle as lifecycle

    try:
        return lifecycle.get_persistence_backend(), False
    except Exception:
        pass

    settings = get_settings()
    backend_type = lifecycle._select_persistence_backend(settings)
    if backend_type == "sqlite":
        backend = await lifecycle._build_sqlite_backend(
            lifecycle._sqlite_path_from_settings(settings),
            settings,
        )
        return backend, True

    database_dsn = lifecycle._database_dsn_from_settings(settings)
    if database_dsn:
        pool = await asyncpg.create_pool(database_dsn)
    else:
        from mnemos.core.config import PG_CONFIG

        pool = await asyncpg.create_pool(
            user=PG_CONFIG["user"],
            password=PG_CONFIG["password"],
            database=PG_CONFIG["database"],
            host=PG_CONFIG["host"],
            port=PG_CONFIG["port"],
            min_size=PG_CONFIG["pool_min_size"],
            max_size=PG_CONFIG["pool_max_size"],
        )
    return lifecycle._build_postgres_backend(pool, settings), True


async def _open_cli_morpheus_pool():
    """Open a raw Postgres pool for one-shot MORPHEUS maintenance commands."""
    import asyncpg

    import mnemos.core.lifecycle as lifecycle

    try:
        return lifecycle.get_pool_manager().pool, False
    except Exception:
        pass

    settings = get_settings()
    if lifecycle._select_persistence_backend(settings) != "postgres":
        raise RuntimeError("mnemos morpheus sweep-orphans requires a Postgres backend")

    database_dsn = lifecycle._database_dsn_from_settings(settings)
    if database_dsn:
        pool = await asyncpg.create_pool(database_dsn)
    else:
        from mnemos.core.config import PG_CONFIG

        pool = await asyncpg.create_pool(
            user=PG_CONFIG["user"],
            password=PG_CONFIG["password"],
            database=PG_CONFIG["database"],
            host=PG_CONFIG["host"],
            port=PG_CONFIG["port"],
            min_size=PG_CONFIG["pool_min_size"],
            max_size=PG_CONFIG["pool_max_size"],
        )
    return pool, True


async def _dedup_sweep_async(
    *,
    namespace: str | None,
    auto_merge: bool,
) -> dict[str, Any]:
    from mnemos.domain.artemis_dedup import sweep_duplicate_content

    backend, close_backend = await _open_cli_persistence_backend()
    try:
        return await sweep_duplicate_content(
            backend,
            namespace=namespace,
            auto_merge=auto_merge,
        )
    finally:
        if close_backend:
            await backend.close()


async def _sweep_morpheus_orphans_async(*, max_age_hours: int) -> int:
    from mnemos.domain.morpheus.runner import sweep_orphan_runs

    pool, close_pool = await _open_cli_morpheus_pool()
    try:
        return await sweep_orphan_runs(pool, max_age_hours=max_age_hours)
    finally:
        if close_pool:
            await pool.close()


def _post_document_import(
    path: Path,
    *,
    tag: str,
    category: str,
    subcategory: str | None,
    permission_mode: int | None,
    allow_archive_snapshot: bool,
) -> dict[str, Any]:
    base, headers = _api_env(require_key=True)
    data: dict[str, str] = {
        "category": category,
        "project_tag": tag,
    }
    if subcategory:
        data["subcategory"] = subcategory
    if permission_mode is not None:
        data["permission_mode"] = str(permission_mode)
    if allow_archive_snapshot:
        data["allow_archive_snapshot"] = "true"
    with path.open("rb") as handle:
        files = {"file": (path.name, handle)}
        response = httpx.post(
            f"{base}/v1/documents/import",
            data=data,
            files=files,
            headers=headers,
            timeout=120,
        )
    try:
        body = response.json()
    except ValueError:
        body = {"text": response.text}
    body["status_code"] = response.status_code
    body["path"] = str(path)
    return body


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
    set_profile_override(profile.value)


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


@worker_app.command("deletion-requests")
def worker_deletion_requests(
    phase: DeletionRequestWorkerPhase = typer.Option(
        DeletionRequestWorkerPhase.soft_delete,
        "--phase",
        help="Deletion-request worker phase: soft_delete or hard_delete.",
        is_flag=False,
    ),
) -> None:
    """Run the GDPR deletion-request worker."""
    from mnemos.workers import deletion_request_worker

    _raise_for_int_result(asyncio.run(deletion_request_worker.main(phase=phase.value)))


@worker_app.command("persephone")
def worker_persephone() -> None:
    """Run the PERSEPHONE archival worker."""
    _run_async_module_main("mnemos.workers.persephone_archival_worker")


@artemis_app.command("dedup-sweep")
def artemis_dedup_sweep(
    namespace: Optional[str] = typer.Option(None, "--namespace", help="Limit sweep to one namespace.", is_flag=False),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report duplicates without modifying rows."),
    auto_merge: bool = typer.Option(False, "--auto-merge", help="Consolidate duplicate rows into the oldest row."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON instead of markdown."),
) -> None:
    """Find duplicate active memories by normalized content hash."""
    if dry_run and auto_merge:
        typer.echo("ERROR: choose either --dry-run or --auto-merge.", err=True)
        raise typer.Exit(2)
    payload = asyncio.run(
        _dedup_sweep_async(namespace=namespace, auto_merge=auto_merge)
    )
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_dedup_markdown(payload)


@morpheus_app.command("sweep-orphans")
def morpheus_sweep_orphans(
    max_age_hours: int = typer.Option(
        2,
        "--max-age-hours",
        help="Fail running MORPHEUS runs older than this many hours.",
        is_flag=False,
    ),
) -> None:
    """Fail MORPHEUS runs stuck in running past the orphan timeout."""
    if max_age_hours <= 0:
        typer.echo("ERROR: --max-age-hours must be positive.", err=True)
        raise typer.Exit(2)
    try:
        swept = asyncio.run(_sweep_morpheus_orphans_async(max_age_hours=max_age_hours))
    except RuntimeError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"Swept {swept} MORPHEUS orphan run(s).")


@app.command("import-doc")
def import_doc(
    file: Path = typer.Argument(..., help="Document file to import."),
    tag: str = typer.Option(..., "--tag", help="Active project tag.", is_flag=False),
    category: str = typer.Option("documents", "--category", help="Memory category.", is_flag=False),
    subcategory: Optional[str] = typer.Option(None, "--subcategory", help="Memory subcategory.", is_flag=False),
    permission_mode: Optional[int] = typer.Option(None, "--permission-mode", help="Unix-style permission mode.", is_flag=False),
    allow_archive_snapshot: bool = typer.Option(
        False,
        "--allow-archive-snapshot",
        help="Allow imports that match historical archive path heuristics.",
    ),
) -> None:
    """Import one document and tag it with an active project."""
    if not file.exists() or not file.is_file():
        raise typer.BadParameter(f"not a file: {file}", param_hint="file")
    result = _post_document_import(
        file,
        tag=tag,
        category=category,
        subcategory=subcategory,
        permission_mode=permission_mode,
        allow_archive_snapshot=allow_archive_snapshot,
    )
    typer.echo(json.dumps(result, indent=2, sort_keys=True))
    if int(result.get("status_code") or 0) >= 400:
        raise typer.Exit(1)


@app.command("import-project")
def import_project(
    repo_path: Path = typer.Argument(..., help="Repository path to scan for docs."),
    tag: str = typer.Option(..., "--tag", help="Active project tag.", is_flag=False),
    category: str = typer.Option("documents", "--category", help="Memory category.", is_flag=False),
    permission_mode: Optional[int] = typer.Option(None, "--permission-mode", help="Unix-style permission mode.", is_flag=False),
    allow_archive_snapshot: bool = typer.Option(
        False,
        "--allow-archive-snapshot",
        help="Allow imports that match historical archive path heuristics.",
    ),
) -> None:
    """Recursively import docs from a repository with a project tag."""
    if not repo_path.exists() or not repo_path.is_dir():
        raise typer.BadParameter(f"not a directory: {repo_path}", param_hint="repo-path")
    results: list[dict[str, Any]] = []
    for path in sorted(repo_path.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in DOC_IMPORT_EXTENSIONS:
            continue
        if any(part in {".git", ".hg", ".svn", "__pycache__"} for part in path.parts):
            continue
        results.append(
            _post_document_import(
                path,
                tag=tag,
                category=category,
                subcategory=None,
                permission_mode=permission_mode,
                allow_archive_snapshot=allow_archive_snapshot,
            )
        )
    payload = {
        "project_tag": tag,
        "repo_path": str(repo_path),
        "documents_seen": len(results),
        "imported": sum(1 for item in results if int(item.get("status_code") or 0) < 400),
        "failed": sum(1 for item in results if int(item.get("status_code") or 0) >= 400),
        "results": results,
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    if payload["failed"]:
        raise typer.Exit(1)


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


@app.command()
def doctor() -> None:
    """Probe host accelerators and print the recommended pip extra.

    Pure-stdlib detection of NVIDIA CUDA / Tegra, Intel iGPU, and
    Apple Silicon, plus optional subsystem extra and bundle status.
    Use this before pip install to pick runtime extras and after
    install to verify feature bundles.
    """
    from mnemos.runtime.hardware import cli_doctor

    raise typer.Exit(code=cli_doctor())


@app.command("dump-openapi")
def dump_openapi(
    output: Optional[str] = typer.Option(
        None,
        "--output", "-o",
        help=(
            "Write the OpenAPI spec to this path instead of stdout. "
            "Use ``-`` or omit to print to stdout."
        ),
    ),
    indent: int = typer.Option(
        2,
        "--indent",
        min=0,
        max=8,
        help="JSON indentation (0 = single-line). Default 2.",
    ),
    title: Optional[str] = typer.Option(
        None,
        "--title",
        help="Override the spec title (default: pulled from FastAPI app).",
    ),
    target: str = typer.Option(
        "full",
        "--target",
        case_sensitive=False,
        help=(
            "Spec target. ``full`` (default) emits the raw FastAPI spec "
            "with no transformations. ``gpt-actions`` truncates endpoint "
            "summary/description fields to 300 chars and parameter "
            "description fields to 700 chars per OpenAI's Custom GPT "
            "Actions limits "
            "(https://developers.openai.com/api/docs/actions/production); "
            "use this when the artifact is going into a Custom GPT, "
            "ChatGPT Pro Developer Mode connector, or OpenAI Actions "
            "bridge that rejects over-long fields."
        ),
    ),
    server_url: Optional[str] = typer.Option(
        None,
        "--server-url",
        help=(
            "Inject this URL as the spec's ``servers[0].url``. "
            "FastAPI does NOT auto-populate the servers field, so "
            "downstream consumers (notably OpenAI Custom GPT "
            "Actions) get a spec whose default server is ``/`` — "
            "useless when the artifact is uploaded into a Builder "
            "running outside your network. Pass the public HTTPS "
            "URL of your MNEMOS deployment "
            "(e.g., ``--server-url https://mnemos.example.com``) "
            "so the consumer's REST calls land at the right host."
        ),
    ),
) -> None:
    """Dump the FastAPI OpenAPI spec to JSON.

    Produces the ``mnemos-openapi.json`` artifact described in
    ROADMAP.md (v4.1 connector deliverable). Useful for OpenAPI-
    aware clients (Custom GPTs, OpenAI Actions bridges, Cursor's
    HTTP MCP, ChatGPT Pro Developer Mode connectors) that need the
    spec without booting the server. Operators with a running
    server can also ``curl http://<host>:5002/openapi.json``; this
    CLI is the build-time / CI path that produces the static
    artifact.

    For Custom GPT / OpenAI Actions consumers, pass
    ``--target gpt-actions`` so endpoint descriptions and parameter
    descriptions are truncated to OpenAI's documented field-length
    limits. The full target keeps the original prose so other
    OpenAPI consumers see the complete documentation.

    Examples:

      mnemos dump-openapi
      mnemos dump-openapi --output mnemos-openapi.json
      mnemos dump-openapi --target gpt-actions -o gpt-spec.json
      mnemos dump-openapi --indent 0 -o /tmp/spec.min.json
    """
    import json
    import sys

    target_norm = (target or "full").strip().lower()
    if target_norm not in {"full", "gpt-actions"}:
        raise typer.BadParameter(
            f"--target must be 'full' or 'gpt-actions'; got {target!r}",
            param_hint="--target",
        )

    # Lazy import — building the FastAPI app pulls in lifecycle
    # plumbing we don't want at module-load time.
    import copy as _copy

    from mnemos.api.main import app as fastapi_app

    # FastAPI caches ``app.openapi()``'s return value and reuses
    # the same dict reference across calls. Mutating it (e.g.
    # ``spec["servers"] = [...]``) bleeds state across CLI
    # invocations — and across tests. Deep-copy first.
    spec = _copy.deepcopy(fastapi_app.openapi())
    if title:
        spec.setdefault("info", {})["title"] = title

    if server_url:
        normalized = server_url.strip()
        if not normalized:
            raise typer.BadParameter(
                "--server-url must not be empty",
                param_hint="--server-url",
            )
        spec["servers"] = [{"url": normalized}]

    if target_norm == "gpt-actions":
        from mnemos.api.openapi_compat import truncate_for_gpt_actions

        spec = truncate_for_gpt_actions(spec)

    rendered = json.dumps(spec, indent=indent if indent > 0 else None, sort_keys=False)

    if output is None or output == "-":
        sys.stdout.write(rendered)
        if not rendered.endswith("\n"):
            sys.stdout.write("\n")
        return

    with open(output, "w", encoding="utf-8") as handle:
        handle.write(rendered)
        if not rendered.endswith("\n"):
            handle.write("\n")
    typer.echo(f"OpenAPI spec written to {output}")


app.add_typer(serve_app, name="serve")
app.add_typer(worker_app, name="worker")
app.add_typer(artemis_app, name="artemis")
app.add_typer(morpheus_app, name="morpheus")


if __name__ == "__main__":
    app()
