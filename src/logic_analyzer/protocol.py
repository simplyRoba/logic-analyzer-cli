"""Pure, hardware-independent protocol logic for the gusmanb LogicAnalyzer.

Everything here is a pure function or dataclass — no serial I/O — so it can be
unit-tested without hardware. The wire facts are taken from the upstream
open-source project (gusmanb/logicanalyzer): the C# ``SharedDriver`` (framing,
``CaptureRequest`` struct, ID handshake) and the RP2040/RP2350 firmware
(``LogicAnalyzer_V2``: request struct layout, limits, sample readback).

Wire facts
----------
- Framing: ``0x55 0xAA <escaped payload> 0xAA 0x55``. Inside the payload any
  ``0xAA``/``0x55``/``0xF0`` byte is escaped as ``0xF0`` then ``byte ^ 0xF0``.
- ID/handshake: send payload ``[0x00]``; the device replies with five text
  lines: ``LOGIC_ANALYZER_<name>_<ver>``, ``FREQ:<hz>``, ``BLASTFREQ:<hz>``,
  ``BUFFER:<bytes>``, ``CHANNELS:<count>``.
- Capture: send payload ``[0x01] + <48-byte CaptureRequest>``. The device
  replies ``CAPTURE_STARTED`` or ``CAPTURE_ERROR`` (one text line). On trigger
  completion it streams a uint32 little-endian sample count, then
  ``count * bytes_per_sample`` sample bytes, then a 1-byte stamp length (and
  optional timestamps, which this client does not request).
- Edge trigger: ``trigger_type=0``, ``trigger=<absolute 0-based channel>``,
  ``inverted=1`` for a falling edge / ``0`` for rising.
- In each sample word, bit ``i`` is the ``i``-th *requested* capture channel
  (the firmware repacks selected channels into the low bits).
- Capture mode by highest channel index: ``<8`` -> 8-channel (1 byte/sample),
  ``<16`` -> 16-channel (2 bytes), else 24-channel (4 bytes).
- Edge-trigger limits: ``pre in [2, N//10]``, ``post in [2, N-2]``,
  ``pre+post <= N``, ``freq in [max_freq*2//65535, max_freq]``, where
  ``N = buffer_bytes // bytes_per_sample``.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

# CaptureRequest, matching the C# [StructLayout(Sequential)] / firmware struct:
#   byte triggerType, byte trigger, byte invertedOrCount, <pad>, uint16 triggerValue,
#   byte[24] channels, byte channelCount, <pad>, uint32 frequency,
#   uint32 preSamples, uint32 postSamples, byte loopCount, byte measure,
#   byte captureMode, <pad>  == 48 bytes
_REQUEST = struct.Struct("<BBBxH24sBxIIIBBBx")
assert _REQUEST.size == 48

ID_COMMAND = 0x00
CAPTURE_COMMAND = 0x01
ABORT_BYTE = 0xFF


def frame(payload: bytes) -> bytes:
    """Wrap a raw payload in the ``0x55 0xAA … 0xAA 0x55`` escaped frame."""
    out = bytearray([0x55, 0xAA])
    for b in payload:
        if b in (0xAA, 0x55, 0xF0):
            out.append(0xF0)
            out.append(b ^ 0xF0)
        else:
            out.append(b)
    out.append(0xAA)
    out.append(0x55)
    return bytes(out)


def id_frame() -> bytes:
    """The framed ID/handshake request."""
    return frame(bytes([ID_COMMAND]))


def bytes_per_sample(mode: int) -> int:
    return 1 if mode == 0 else (2 if mode == 1 else 4)


def capture_mode_for_channels(channels: list[int]) -> int:
    """0 = 8-channel, 1 = 16-channel, 2 = 24-channel, from the highest index."""
    hi = max(channels)
    return 0 if hi < 8 else (1 if hi < 16 else 2)


def build_capture_request(
    *,
    channels: list[int],
    frequency: int,
    pre: int,
    post: int,
    trigger_channel: int,
    falling: bool,
    mode: int,
    loop: int = 0,
    measure: bool = False,
) -> bytes:
    """Pack a 48-byte edge-trigger CaptureRequest (not framed)."""
    if not 0 <= len(channels) <= 24:
        raise ValueError("channels must have 0..24 entries")
    chan_bytes = bytes(channels) + bytes(24 - len(channels))
    return _REQUEST.pack(
        0,  # triggerType = edge
        trigger_channel & 0xFF,
        1 if falling else 0,  # invertedOrCount
        0,  # triggerValue (unused for edge)
        chan_bytes,
        len(channels) & 0xFF,
        frequency & 0xFFFFFFFF,
        pre & 0xFFFFFFFF,
        post & 0xFFFFFFFF,
        loop & 0xFF,
        1 if measure else 0,
        mode & 0xFF,
    )


def capture_frame(**kwargs) -> bytes:
    """The framed capture request: ``[0x01] + CaptureRequest``."""
    return frame(bytes([CAPTURE_COMMAND]) + build_capture_request(**kwargs))


@dataclass(frozen=True)
class DeviceInfo:
    name: str
    max_freq: int
    blast_freq: int
    buffer_bytes: int
    max_channels: int

    @classmethod
    def from_handshake(cls, lines: list[str]) -> "DeviceInfo":
        """Parse the five handshake lines (ID, FREQ, BLASTFREQ, BUFFER, CHANNELS)."""
        if len(lines) < 5:
            raise ValueError(f"expected 5 handshake lines, got {len(lines)}: {lines!r}")
        name = lines[0].strip()
        if not name.startswith("LOGIC_ANALYZER"):
            raise ValueError(f"unexpected ID line: {name!r}")

        def field(line: str, key: str) -> int:
            k, _, v = line.strip().partition(":")
            if k != key:
                raise ValueError(f"expected {key}:… got {line!r}")
            return int(v)

        return cls(
            name=name,
            max_freq=field(lines[1], "FREQ"),
            blast_freq=field(lines[2], "BLASTFREQ"),
            buffer_bytes=field(lines[3], "BUFFER"),
            max_channels=field(lines[4], "CHANNELS"),
        )


@dataclass(frozen=True)
class Limits:
    total_samples: int
    min_pre: int
    max_pre: int
    min_post: int
    max_post: int
    min_freq: int
    max_freq: int


def limits_for(info: DeviceInfo, mode: int) -> Limits:
    n = info.buffer_bytes // bytes_per_sample(mode)
    return Limits(
        total_samples=n,
        min_pre=2,
        max_pre=n // 10,
        min_post=2,
        max_post=n - 2,
        min_freq=(info.max_freq * 2) // 65535,
        max_freq=info.max_freq,
    )


def validate_request(
    info: DeviceInfo, channels: list[int], frequency: int, pre: int, post: int, mode: int
) -> list[str]:
    """Return a list of human-readable problems; empty means the request is valid.

    Mirrors ``LogicAnalyzerDriver.ValidateSettings`` for the edge-trigger path.
    Catching these here is the whole point: the stock CLI sends an invalid
    request and then hangs forever instead of reporting the rejection.
    """
    lim = limits_for(info, mode)
    errs: list[str] = []
    if not channels:
        errs.append("no capture channels given")
        return errs
    if min(channels) < 0 or max(channels) > info.max_channels - 1:
        errs.append(f"channel out of range 0..{info.max_channels - 1}")
    if not (lim.min_pre <= pre <= lim.max_pre):
        errs.append(
            f"pre-trigger {pre} out of range {lim.min_pre}..{lim.max_pre} "
            f"(hardware caps pre-trigger at buffer/10)"
        )
    if not (lim.min_post <= post <= lim.max_post):
        errs.append(f"post-trigger {post} out of range {lim.min_post}..{lim.max_post}")
    if pre + post > lim.total_samples:
        errs.append(f"pre+post {pre + post} exceeds total buffer {lim.total_samples}")
    if not (lim.min_freq <= frequency <= lim.max_freq):
        errs.append(f"frequency {frequency} out of range {lim.min_freq}..{lim.max_freq}")
    return errs


def unpack_samples(raw: bytes, mode: int) -> list[int]:
    """Decode the sample byte-stream into per-sample integer words."""
    bps = bytes_per_sample(mode)
    if mode == 0:
        return list(raw)
    fmt = "<H" if bps == 2 else "<I"
    s = struct.Struct(fmt)
    return [s.unpack_from(raw, i)[0] for i in range(0, len(raw) - bps + 1, bps)]


def samples_to_rows(words: list[int], channel_count: int) -> list[tuple[int, ...]]:
    """Split each sample word into per-channel 0/1 values (bit i = channel i)."""
    masks = [1 << i for i in range(channel_count)]
    return [tuple(1 if w & m else 0 for m in masks) for w in words]


def parse_channel_spec(spec: str) -> tuple[str, int]:
    """``NAME=CH`` where CH is a 1-based analyzer channel (as labelled on the
    device). Returns ``(name, zero_based_channel)``."""
    name, sep, ch = spec.partition("=")
    if not sep or not name or not ch:
        raise ValueError(f"bad channel spec {spec!r}, expected NAME=CH (e.g. CS=5)")
    return name, int(ch) - 1
