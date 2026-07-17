"""Headless client library + CLI for the gusmanb LogicAnalyzer (RP2040/RP2350)."""
from .protocol import (
    DeviceInfo,
    Limits,
    build_capture_request,
    capture_frame,
    capture_mode_for_channels,
    frame,
    id_frame,
    limits_for,
    parse_channel_spec,
    samples_to_rows,
    unpack_samples,
    validate_request,
)

__version__ = "0.1.0"

__all__ = [
    "DeviceInfo",
    "Limits",
    "build_capture_request",
    "capture_frame",
    "capture_mode_for_channels",
    "frame",
    "id_frame",
    "limits_for",
    "parse_channel_spec",
    "samples_to_rows",
    "unpack_samples",
    "validate_request",
    "__version__",
]
