"""GPU + accelerator detection without ML-framework imports.

The fleet runs MNEMOS on hosts with vastly different accelerators:

  * NVIDIA CUDA discrete GPUs (TYPHON, CERBERUS) — torch CUDA wheel
  * NVIDIA Tegra (cixmini) — TensorRT, NOT the desktop CUDA wheel
  * Intel iGPU (PYTHIA, PROTEUS, ARGOS) — OpenVINO
  * Apple Silicon (jperlow-mlt, ULTRA, STUDIO) — MPS / Metal
  * VideoCore + ARM CPU (bigpi, clawpi, zeropi) — CPU only

A single "is there a GPU" check is the wrong shape because each
accelerator family wants a DIFFERENT runtime to use it. Picking the
wrong runtime (e.g., torch CUDA on an Intel iGPU host) silently
falls back to CPU AND ships ~1 GB of unused binary weight.

This module classifies what KIND of accelerator is present so the
compression / embedding paths can request the matching runtime
extra (`mnemos-os[gpu]`, `[phi]`, `[ml]`) without making torch a
hard dependency.

The detection is *pure stdlib* — no torch, no nvidia-ml-py, no
fastembed. Method: shell out to ``nvidia-smi`` / ``system_profiler``
/ check ``/proc/cpuinfo`` and ``/sys/class/drm``. Each probe is
cached for the process lifetime; consult is idempotent.

If you need richer detection (memory totals, compute capability,
driver version), use ``nvidia-ml-py`` / ``pyopencl`` directly in
the consumer module. This file is for "is the runtime worth
installing" decisions only.
"""
from __future__ import annotations

import enum
import functools
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import List

__all__ = [
    "GPUKind",
    "HostProfile",
    "detect_host_profile",
    "has_apple_metal",
    "has_intel_igpu",
    "has_nvidia_cuda",
    "has_nvidia_tegra",
]


class GPUKind(enum.Enum):
    """Coarse classification driving runtime-extra selection."""

    NONE = "none"
    NVIDIA_CUDA = "nvidia-cuda"      # desktop / server CUDA
    NVIDIA_TEGRA = "nvidia-tegra"    # Jetson family — TensorRT, not CUDA wheel
    INTEL_IGPU = "intel-igpu"        # Iris, Xe, Arc, Raptor Lake-P
    APPLE_METAL = "apple-metal"      # M-series MPS
    AMD_ROCM = "amd-rocm"            # ROCm-capable AMD discrete
    OTHER = "other"                  # GPU present but doesn't match above


@dataclass(frozen=True)
class HostProfile:
    """Snapshot of the host's accelerator landscape.

    Field shape is intentionally narrow: enough to pick a default
    runtime extra, not enough to manage a fleet inventory (use
    `nvidia-smi` / `lspci` / fleet config for that).
    """

    gpu_kinds: List[GPUKind] = field(default_factory=list)
    os_family: str = ""              # "linux" | "darwin" | "windows"
    arch: str = ""                   # "x86_64" | "aarch64" | "arm64"
    suggested_extra: str = "default"  # "default" | "ml" | "gpu" | "phi"

    @property
    def has_gpu(self) -> bool:
        return any(k != GPUKind.NONE for k in self.gpu_kinds)


@functools.lru_cache(maxsize=1)
def has_nvidia_cuda() -> bool:
    """True iff a CUDA-capable NVIDIA GPU is present.

    Probe: ``nvidia-smi -L`` exit-zero AND output contains ``GPU``
    AND not classified as Tegra. nvidia-smi is provided by the
    NVIDIA driver and is the canonical detection mechanism that
    DOES NOT require torch or any Python GPU binding.

    Returns False on hosts without nvidia-smi installed (the
    binary doesn't ship by default; it lands when the operator
    installs the proprietary driver).
    """
    if shutil.which("nvidia-smi") is None:
        return False
    try:
        out = subprocess.run(
            ["nvidia-smi", "-L"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False
    if out.returncode != 0:
        return False
    text = (out.stdout or "") + (out.stderr or "")
    if "GPU" not in text:
        return False
    # Tegra reports as "GPU 0: Orin (UUID: ...)" or similar; we want
    # the desktop / server cards specifically.
    return not _looks_like_tegra(text)


@functools.lru_cache(maxsize=1)
def has_nvidia_tegra() -> bool:
    """True iff this is a Jetson / Tegra board.

    Tegra needs TensorRT / TRT-LLM, not the desktop CUDA wheel.
    Detection: device-tree model contains "tegra" / "jetson" / "orin"
    / "xavier", OR /proc/device-tree/model is the canonical kernel
    surface on Linux. nvidia-smi sometimes works on Tegra but the
    runtime contract is different.
    """
    if platform.system() != "Linux":
        return False
    try:
        with open("/proc/device-tree/model", "rb") as fh:
            model = fh.read().decode("ascii", errors="ignore").lower().rstrip("\x00")
    except (FileNotFoundError, OSError, PermissionError):
        return False
    return any(k in model for k in ("tegra", "jetson", "orin", "xavier"))


@functools.lru_cache(maxsize=1)
def has_intel_igpu() -> bool:
    """True iff an Intel integrated GPU is present.

    Intel iGPUs are the OpenVINO target — `mnemos-os[phi]` extra.
    Detection: lspci-grep on Linux, ``system_profiler`` on macOS
    (Intel Macs), Windows WMI is out of scope (no fleet host runs
    native Windows mnemos; WSL2 path uses the Linux probe).
    """
    sys_name = platform.system()
    if sys_name == "Linux":
        if shutil.which("lspci") is None:
            return False
        try:
            out = subprocess.run(
                ["lspci"],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return False
        if out.returncode != 0:
            return False
        text = out.stdout.lower()
        return "intel" in text and any(
            kw in text for kw in ("vga", "display", "graphics", "3d controller")
        )
    if sys_name == "Darwin":
        # Intel Macs only — Apple Silicon is detected separately.
        if platform.machine().lower() in ("arm64", "aarch64"):
            return False
        if shutil.which("system_profiler") is None:
            return False
        try:
            out = subprocess.run(
                ["system_profiler", "SPDisplaysDataType"],
                capture_output=True,
                text=True,
                timeout=3.0,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return False
        return "Intel" in (out.stdout or "")
    return False


@functools.lru_cache(maxsize=1)
def has_apple_metal() -> bool:
    """True iff this is Apple Silicon (M-series) — MPS via Metal.

    Detection: macOS + arm64. Intel Macs return False (Metal works
    but the embedding-runtime story we care about — fastembed
    CoreML EP, MLX, etc. — targets Apple Silicon specifically).
    """
    return (
        platform.system() == "Darwin"
        and platform.machine().lower() in ("arm64", "aarch64")
    )


def _looks_like_tegra(nvidia_smi_output: str) -> bool:
    text = nvidia_smi_output.lower()
    return any(k in text for k in ("tegra", "jetson", "orin", "xavier"))


def _detect_kinds() -> List[GPUKind]:
    kinds: List[GPUKind] = []
    if has_nvidia_tegra():
        kinds.append(GPUKind.NVIDIA_TEGRA)
    elif has_nvidia_cuda():
        # Mutually exclusive: a Tegra board reports as cuda-capable
        # but should be classified as TEGRA (different runtime).
        kinds.append(GPUKind.NVIDIA_CUDA)
    if has_apple_metal():
        kinds.append(GPUKind.APPLE_METAL)
    if has_intel_igpu():
        kinds.append(GPUKind.INTEL_IGPU)
    return kinds or [GPUKind.NONE]


def _suggested_extra(kinds: List[GPUKind]) -> str:
    """Map detected accelerators to the recommended pip extra.

    Picks the FIRST matching path in this priority order:

      NVIDIA_CUDA  → ``gpu``    (fastembed-gpu, CUDA EP)
      INTEL_IGPU   → ``phi``    (openvino-genai + fastembed)
      APPLE_METAL  → ``ml``     (CPU fastembed; MLX / CoreML
                                 acceleration is a v4.3+ candidate)
      NVIDIA_TEGRA → ``ml``     (Tegra wants TRT-LLM not in deps)
      NONE / OTHER → ``ml``     (CPU baseline)

    No host gets `default` (zero ML extras) as a recommendation —
    fastembed is small enough that the quality-scoring path is
    worth enabling everywhere. Operators who genuinely don't need
    semantic similarity stay on `default` and get heuristic-only
    quality manifests.
    """
    if GPUKind.NVIDIA_CUDA in kinds:
        return "gpu"
    if GPUKind.INTEL_IGPU in kinds:
        return "phi"
    return "ml"


def detect_host_profile(force_refresh: bool = False) -> HostProfile:
    """Return the host's accelerator + suggested-extra profile.

    Cached for process lifetime; pass ``force_refresh=True`` to
    re-probe (useful in tests after monkey-patching). Side effects:
    runs ``nvidia-smi`` / ``lspci`` / ``system_profiler``
    subprocess calls, each capped at ~2-3s.
    """
    if force_refresh:
        for fn in (has_nvidia_cuda, has_nvidia_tegra, has_intel_igpu, has_apple_metal):
            # Tests may monkey-patch these with plain lambdas that
            # don't have lru_cache machinery — defensively check.
            if hasattr(fn, "cache_clear"):
                fn.cache_clear()
    kinds = _detect_kinds()
    return HostProfile(
        gpu_kinds=kinds,
        os_family=platform.system().lower(),
        arch=platform.machine().lower(),
        suggested_extra=_suggested_extra(kinds),
    )


def cli_doctor() -> int:
    """Print the host profile in a human-readable format.

    Wired into ``mnemos doctor`` (or invoked directly via
    ``python -m mnemos.runtime.hardware``) so operators can confirm
    which extra they should install BEFORE running the actual
    install.
    """
    profile = detect_host_profile(force_refresh=True)
    print(f"Host: {profile.os_family} / {profile.arch}")
    print(f"GPU kinds: {[k.value for k in profile.gpu_kinds]}")
    print(f"Suggested mnemos extra: [{profile.suggested_extra}]")
    if profile.suggested_extra == "ml":
        print("  → pip install 'mnemos-os[ml]'  (fastembed, CPU, ~20 MB)")
    elif profile.suggested_extra == "gpu":
        print("  → pip install 'mnemos-os[gpu]'  (fastembed-gpu, NVIDIA CUDA EP)")
    elif profile.suggested_extra == "phi":
        print("  → pip install 'mnemos-os[phi]'  (OpenVINO + fastembed, Intel iGPU)")
    return 0


if __name__ == "__main__":
    sys.exit(cli_doctor())
