"""mnemos.runtime — host hardware + runtime probing.

Pure-stdlib helpers that introspect the host without pulling in any
ML framework. Used by the dependency-aware paths in
mnemos.domain.compression and the install-time `mnemos doctor` command
to make sensible defaults (CPU vs GPU, OpenVINO vs ONNX-CUDA, etc.)
without requiring torch or fastembed-gpu to be installed.
"""

from mnemos.runtime.hardware import (
    GPUKind,
    HostProfile,
    detect_host_profile,
    has_apple_metal,
    has_intel_igpu,
    has_nvidia_cuda,
)

__all__ = [
    "GPUKind",
    "HostProfile",
    "detect_host_profile",
    "has_apple_metal",
    "has_intel_igpu",
    "has_nvidia_cuda",
]
