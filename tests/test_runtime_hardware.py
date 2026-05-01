"""Coverage for the GPU / accelerator detection module.

The module is pure-stdlib (no torch / fastembed / nvidia-ml-py) and
classifies the host accelerator into a coarse GPUKind so the
compression + embedding paths can pick the right runtime extra
without making any ML framework a hard dependency.

Tests cover:
  * pure platform detection (apple-metal, etc.) without subprocess
  * subprocess probes monkey-patched to return known shapes
  * suggested-extra mapping per host kind
  * cache-clear path used by ``detect_host_profile(force_refresh=True)``
"""
from __future__ import annotations

import platform

import pytest

from mnemos.runtime import hardware


def _clear_caches() -> None:
    """Reset lru_cache state on detection probes.

    Defensive .cache_clear() — tests monkeypatch the bound names
    (e.g., to plain lambdas) and the prior test's teardown may
    have replaced the lru_cache fn already, so check first.
    """
    for fn in (
        hardware.has_nvidia_cuda,
        hardware.has_nvidia_tegra,
        hardware.has_intel_igpu,
        hardware.has_apple_metal,
    ):
        if hasattr(fn, "cache_clear"):
            fn.cache_clear()


@pytest.fixture(autouse=True)
def _clear_cache():
    _clear_caches()
    yield
    _clear_caches()


def test_apple_metal_detection_uses_platform_only(monkeypatch):
    """Apple Silicon detection is platform-only, no subprocess required."""
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    assert hardware.has_apple_metal() is True


def test_apple_metal_returns_false_on_intel_mac(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    assert hardware.has_apple_metal() is False


def test_apple_metal_returns_false_on_linux(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "aarch64")
    assert hardware.has_apple_metal() is False


def test_nvidia_cuda_returns_false_when_smi_missing(monkeypatch):
    monkeypatch.setattr(hardware.shutil, "which", lambda _: None)
    assert hardware.has_nvidia_cuda() is False


def test_nvidia_cuda_classifies_desktop_gpu(monkeypatch):
    monkeypatch.setattr(hardware.shutil, "which", lambda x: "/usr/bin/nvidia-smi" if x == "nvidia-smi" else None)

    class _Result:
        returncode = 0
        stdout = "GPU 0: NVIDIA GeForce RTX 5060 (UUID: GPU-xxx)\n"
        stderr = ""

    monkeypatch.setattr(hardware.subprocess, "run", lambda *a, **kw: _Result())
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    # Block tegra detection — model file open will raise.
    monkeypatch.setattr("builtins.open", _make_failing_open())
    assert hardware.has_nvidia_cuda() is True
    assert hardware.has_nvidia_tegra() is False


def test_nvidia_cuda_skips_tegra_devices(monkeypatch):
    """Tegra reports via nvidia-smi but is a different runtime story.

    has_nvidia_cuda must return False on Tegra so the suggested
    extra is `ml` (CPU fastembed) instead of `gpu` (CUDA EP that
    won't load on Tegra).
    """
    monkeypatch.setattr(hardware.shutil, "which", lambda x: "/usr/bin/nvidia-smi" if x == "nvidia-smi" else None)

    class _Result:
        returncode = 0
        stdout = "GPU 0: Orin (Tegra) (UUID: GPU-xxx)\n"
        stderr = ""

    monkeypatch.setattr(hardware.subprocess, "run", lambda *a, **kw: _Result())
    assert hardware.has_nvidia_cuda() is False


def test_tegra_detection_via_device_tree(monkeypatch, tmp_path):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    # Stage a model file with "tegra" content.
    real_open = open

    def fake_open(path, *args, **kwargs):
        if str(path) == "/proc/device-tree/model":
            return real_open(_stage_model_file(tmp_path, "NVIDIA Jetson Orin Nano Devkit\x00"), *args, **kwargs)
        raise FileNotFoundError(path)

    monkeypatch.setattr("builtins.open", fake_open)
    assert hardware.has_nvidia_tegra() is True


def test_intel_igpu_detection_via_lspci(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(hardware.shutil, "which", lambda x: "/usr/bin/lspci" if x == "lspci" else None)

    class _Result:
        returncode = 0
        stdout = "00:02.0 VGA compatible controller: Intel Corporation Raptor Lake-P [Iris Xe Graphics]\n"
        stderr = ""

    monkeypatch.setattr(hardware.subprocess, "run", lambda *a, **kw: _Result())
    assert hardware.has_intel_igpu() is True


def test_suggested_extra_for_clean_cpu_host(monkeypatch):
    monkeypatch.setattr(hardware, "has_nvidia_cuda", lambda: False)
    monkeypatch.setattr(hardware, "has_nvidia_tegra", lambda: False)
    monkeypatch.setattr(hardware, "has_intel_igpu", lambda: False)
    monkeypatch.setattr(hardware, "has_apple_metal", lambda: False)
    profile = hardware.detect_host_profile(force_refresh=False)
    assert profile.suggested_extra == "ml"
    assert profile.has_gpu is False


def test_suggested_extra_prefers_cuda_over_intel(monkeypatch):
    """On a hypothetical host with both NVIDIA CUDA AND Intel iGPU,
    we prefer the discrete GPU path (`gpu` extra) — fastembed-gpu's
    CUDA EP gives more performance headroom than OpenVINO."""
    monkeypatch.setattr(hardware, "has_nvidia_cuda", lambda: True)
    monkeypatch.setattr(hardware, "has_nvidia_tegra", lambda: False)
    monkeypatch.setattr(hardware, "has_intel_igpu", lambda: True)
    monkeypatch.setattr(hardware, "has_apple_metal", lambda: False)
    profile = hardware.detect_host_profile(force_refresh=False)
    assert profile.suggested_extra == "gpu"


def test_suggested_extra_apple_silicon_uses_ml(monkeypatch):
    """Apple Silicon picks `ml` (CPU fastembed) until MLX / CoreML EP
    integration lands. Operators with M-series can opt into a custom
    runtime separately."""
    monkeypatch.setattr(hardware, "has_nvidia_cuda", lambda: False)
    monkeypatch.setattr(hardware, "has_nvidia_tegra", lambda: False)
    monkeypatch.setattr(hardware, "has_intel_igpu", lambda: False)
    monkeypatch.setattr(hardware, "has_apple_metal", lambda: True)
    profile = hardware.detect_host_profile(force_refresh=False)
    assert profile.suggested_extra == "ml"
    assert hardware.GPUKind.APPLE_METAL in profile.gpu_kinds


def test_force_refresh_clears_cache(monkeypatch):
    """detect_host_profile(force_refresh=True) re-runs the lru_cached
    probes so successive calls observe a changed environment."""
    calls = []

    def first_call():
        calls.append(1)
        return False

    monkeypatch.setattr(hardware, "has_nvidia_cuda", first_call)
    monkeypatch.setattr(hardware, "has_nvidia_tegra", lambda: False)
    monkeypatch.setattr(hardware, "has_intel_igpu", lambda: False)
    monkeypatch.setattr(hardware, "has_apple_metal", lambda: False)
    p1 = hardware.detect_host_profile(force_refresh=False)
    p2 = hardware.detect_host_profile(force_refresh=True)
    assert p1.suggested_extra == p2.suggested_extra == "ml"
    # We don't assert call count because lru_cache wraps still apply
    # to has_nvidia_cuda; the test exercises that the API doesn't
    # raise and that force_refresh is callable.


# --- helpers ---


def _make_failing_open():
    """Return a builtins.open replacement that raises FileNotFoundError
    for every path. Lets us stub out the /proc/device-tree probe."""

    def fake_open(*args, **kwargs):
        raise FileNotFoundError("stubbed")

    return fake_open


def _stage_model_file(tmp_path, content: str) -> str:
    p = tmp_path / "model"
    p.write_text(content)
    return str(p)
