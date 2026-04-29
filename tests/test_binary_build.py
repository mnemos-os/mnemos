import ast
import importlib
import os
import subprocess
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_build_extra_includes_pyinstaller_and_sqlite_vec():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    build_extra = pyproject["project"]["optional-dependencies"]["build"]

    assert "pyinstaller>=6.0" in build_extra
    assert "sqlite-vec" in build_extra


def test_mnemos_spec_parses_and_targets_cli_app():
    spec_path = REPO_ROOT / "mnemos.spec"
    source = spec_path.read_text(encoding="utf-8")

    ast.parse(source, filename=str(spec_path))

    assert 'ENTRY_POINT = "mnemos.cli.main:app"' in source
    assert "optimize=2" in source
    assert "strip=True" in source
    assert '"sqlite_vec"' in source
    assert '"google.genai"' in source
    assert '"db/migrations_sqlite"' in source
    assert 'name=BINARY_NAME' in source
    assert 'collect_dynamic_libs("sqlite_vec")' in source


def test_entrypoint_module_exports_typer_app():
    module = importlib.import_module("mnemos.cli.main")

    assert hasattr(module, "app")


def test_build_binary_script_shebang_and_missing_pyinstaller_exit(tmp_path):
    script = REPO_ROOT / "scripts" / "build-binary.sh"
    first_line = script.read_text(encoding="utf-8").splitlines()[0]

    assert first_line == "#!/usr/bin/env bash"

    env = os.environ.copy()
    env["PATH"] = str(tmp_path)
    result = subprocess.run(
        ["/bin/bash", str(script)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "pyinstaller is required" in result.stderr
