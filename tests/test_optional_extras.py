from __future__ import annotations

import asyncio
import importlib
import logging
import sys
from types import ModuleType

import pytest
from fastapi import HTTPException

from mnemos.core import extras


def _drop_modules(prefix: str) -> dict[str, ModuleType]:
    removed: dict[str, ModuleType] = {}
    for name in list(sys.modules):
        if name == prefix or name.startswith(f"{prefix}."):
            module = sys.modules.pop(name)
            if isinstance(module, ModuleType):
                removed[name] = module
    return removed


def _restore_modules(removed: dict[str, ModuleType]) -> None:
    for name, module in removed.items():
        sys.modules.setdefault(name, module)


def test_is_extra_installed_known_no_dep_and_unknown() -> None:
    assert extras.is_extra_installed("pantheon") is True
    assert extras.is_extra_installed("definitely-not-a-mnemos-extra") is False


def test_require_extra_raises_with_install_hint() -> None:
    with pytest.raises(RuntimeError) as exc:
        extras.require_extra("definitely-not-a-mnemos-extra")

    message = str(exc.value)
    assert "definitely-not-a-mnemos-extra subsystem not installed" in message
    assert "pip install mnemos-os[definitely-not-a-mnemos-extra]" in message


def test_optional_subsystem_import_works_when_extra_installed() -> None:
    module = importlib.import_module("mnemos.domain.pantheon")
    assert hasattr(module, "route_model")


def test_optional_subsystem_import_raises_when_dependency_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    removed = _drop_modules("mnemos.domain.persephone")
    monkeypatch.setitem(sys.modules, "zstandard", None)
    try:
        with pytest.raises(RuntimeError) as exc:
            importlib.import_module("mnemos.domain.persephone")
    finally:
        sys.modules.pop("mnemos.domain.persephone", None)
        sys.modules.pop("mnemos.domain.persephone.runner", None)
        _restore_modules(removed)

    assert "persephone subsystem not installed" in str(exc.value)
    assert "mnemos-os[persephone]" in str(exc.value)


def test_optional_route_returns_503_with_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    from mnemos.api.routes import kronos

    monkeypatch.setattr(kronos, "is_extra_installed", lambda name: False if name == "kronos" else True)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(kronos.recall_anomalies(namespace="default", _=None))

    assert exc.value.status_code == 503
    assert exc.value.detail["error"] == "KRONOS not installed"
    assert "mnemos-os[kronos]" in exc.value.detail["install"]


def test_mcp_tool_filter_hides_missing_extra_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    from mnemos.mcp import tools

    monkeypatch.setattr(tools, "is_extra_installed", lambda name: name != "kronos")

    assert tools._filter_unavailable_tools(
        ["search_memories", "kronos_anomalies", "pantheon_list_models"]
    ) == ["search_memories", "pantheon_list_models"]


def test_worker_noops_when_extra_missing(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    from mnemos.workers import persephone_archival_worker as worker

    monkeypatch.setattr(worker, "is_extra_installed", lambda name: False if name == "persephone" else True)
    caplog.set_level(logging.INFO, logger="mnemos.workers.persephone_archival_worker")

    asyncio.run(worker.persephone_archival_worker_loop(object()))

    assert "PERSEPHONE worker disabled (extra not installed)" in caplog.text
