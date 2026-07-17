"""Serial transport for the gusmanb LogicAnalyzer.

This is the thin I/O layer on top of :mod:`logic_analyzer.protocol`. It owns the
pyserial port, the handshake, the capture round-trip, and recovery from a device
left stuck by a previously aborted run.

Why recovery matters: if a capture process is killed while armed, the device can
be left mid-stream and stops answering the ID command. The stock software's only
fix is to close/reopen the port; this client does that automatically (abort +
reopen + retry) so you rarely need to physically replug.
"""
from __future__ import annotations

import glob
import os
import sys
import time
from typing import Callable, Optional

try:
    import serial  # pyserial
except ImportError:  # pragma: no cover - import guard
    serial = None  # type: ignore

from . import protocol
from .protocol import DeviceInfo

BAUD = 115200


def find_port(hint: Optional[str] = None) -> str:
    """Return a concrete serial port.

    If ``hint`` is given and exists, use it. Otherwise auto-pick the first
    usbmodem/usbserial/ACM/USB port. The analyzer can re-enumerate with a
    different name after a recovery, so a fixed name is fragile — auto-detect
    is the default.
    """
    if hint and os.path.exists(hint):
        return hint
    candidates = (
        glob.glob("/dev/cu.usbmodem*")
        + glob.glob("/dev/cu.usbserial*")
        + glob.glob("/dev/tty.usbmodem*")
        + glob.glob("/dev/ttyACM*")
        + glob.glob("/dev/ttyUSB*")
    )
    candidates = [c for c in candidates if "debug" not in c and "Bluetooth" not in c]
    if not candidates:
        raise RuntimeError(
            f"no analyzer serial port found (hint {hint!r} not present)"
        )
    return sorted(candidates)[0]


class Device:
    """Open serial connection to a LogicAnalyzer, with handshake + recovery."""

    def __init__(self, port: Optional[str] = None, log: Callable[[str], None] = None,
                 abort_settle_s: float = 2.0, recovery_settle_s: Optional[float] = None):
        if serial is None:
            raise RuntimeError("pyserial is required: pip install pyserial")
        self._log = log or (lambda m: print(m, file=sys.stderr))
        self._abort_settle_s = abort_settle_s
        # Per-attempt settle during heavy recovery; None = behavior-derived
        # (grows past the device's ~2 s cancel-poll cycle). Tests set 0.
        self._recovery_settle_s = recovery_settle_s
        self.port = find_port(port)
        self.sp = None
        self.info: Optional[DeviceInfo] = None
        self._open()
        self._recover_and_handshake()

    # -- connection ---------------------------------------------------------

    def _open(self) -> None:
        if self.sp is not None:
            try:
                self.sp.close()
            except Exception:
                pass
        self.port = find_port(self.port)
        self.sp = serial.Serial(self.port, BAUD, timeout=10)
        self.sp.dtr = True
        self.sp.rts = True
        time.sleep(0.05)
        self.sp.reset_input_buffer()

    def close(self) -> None:
        try:
            if self.sp is not None:
                self.sp.close()
        except Exception:
            pass

    def __enter__(self) -> "Device":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- low-level I/O ------------------------------------------------------

    def _readline(self, timeout: float = 10.0) -> str:
        self.sp.timeout = timeout
        return self.sp.readline().decode("ascii", "replace").strip()

    def _read_exact(self, n: int, deadline: float) -> Optional[bytes]:
        buf = bytearray()
        while len(buf) < n:
            self.sp.timeout = max(0.1, deadline - time.time())
            chunk = self.sp.read(n - len(buf))
            if chunk:
                buf.extend(chunk)
            elif time.time() >= deadline:
                return None
        return bytes(buf)

    # -- handshake / recovery ----------------------------------------------

    def _try_id(self, read_timeout: float = 2.0) -> list[str]:
        self.sp.reset_input_buffer()
        self.sp.write(protocol.id_frame())
        self.sp.flush()
        first = self._readline(timeout=read_timeout)
        if not first.startswith("LOGIC_ANALYZER"):
            return [first]
        return [first] + [self._readline() for _ in range(4)]

    def _drain(self, seconds: float) -> None:
        """Read and discard everything the device streams for `seconds`."""
        end = time.time() + seconds
        self.sp.timeout = 0.2
        while time.time() < end:
            if not self.sp.read(65536):
                break

    def _toggle_dtr(self) -> None:
        """Pulse DTR/RTS low then high. On the RP2040/RP2350 tinyUSB CDC this
        resets the host-side line state and helps knock the device out of a
        wedged transfer without a physical replug."""
        pause = 0.0 if self._recovery_settle_s == 0 else 0.2
        try:
            self.sp.dtr = False
            self.sp.rts = False
            time.sleep(pause)
            self.sp.dtr = True
            self.sp.rts = True
            time.sleep(pause)
        except Exception:
            pass

    def _recover_and_handshake(self, attempts: int = 5) -> None:
        """Bring the device back to answering the ID command.

        The firmware, while a capture is armed, only checks for a cancel byte
        once per ~2 s poll cycle and may still have a data burst to flush. So a
        naive abort+immediate-retry loses the race. Each recovery attempt here
        escalates: send abort, wait past the ~2 s poll cycle, drain the stream,
        toggle DTR to reset the CDC line state, reopen, then retry ID with a
        generous read timeout.
        """
        last = ""
        # First, a plain fast attempt: a healthy device answers immediately.
        try:
            self.info = DeviceInfo.from_handshake(self._try_id())
            return
        except Exception as e:
            last = str(e)

        for i in range(attempts):
            settle = self._recovery_settle_s if self._recovery_settle_s is not None else (2.5 + i)
            self.abort()
            self._drain(settle)
            self._toggle_dtr()
            try:
                self._open()
            except Exception:
                time.sleep(0.5)
                continue
            try:
                self.info = DeviceInfo.from_handshake(self._try_id(read_timeout=3.0))
                return
            except Exception as e:
                last = str(e)
        raise RuntimeError(
            f"device did not respond to ID after {attempts} recovery attempts "
            f"({last}); replug the analyzer if this persists"
        )

    def handshake(self) -> DeviceInfo:
        assert self.info is not None
        return self.info

    # -- capture ------------------------------------------------------------

    def capture(
        self,
        *,
        channels: list[int],
        frequency: int,
        pre: int,
        post: int,
        trigger_channel: int,
        falling: bool,
        mode: int,
        wait_s: float,
        read_timeout_s: float = 30.0,
    ) -> bytes:
        """Arm a single edge-triggered capture and return the raw sample bytes.

        Raises TimeoutError if the trigger never fires within ``wait_s`` (and
        aborts the device so the next call starts clean).
        """
        req = protocol.capture_frame(
            channels=channels,
            frequency=frequency,
            pre=pre,
            post=post,
            trigger_channel=trigger_channel,
            falling=falling,
            mode=mode,
        )
        self.sp.reset_input_buffer()
        self.sp.write(req)
        self.sp.flush()
        reply = self._readline(timeout=10.0)
        if reply != "CAPTURE_STARTED":
            raise RuntimeError(
                f"device did not start capture (replied {reply!r}); "
                "this is the rejection the stock CLI silently swallows"
            )
        self._log(
            f"armed: waiting up to {wait_s:.0f}s for the trigger "
            f"(channel {trigger_channel}, {'falling' if falling else 'rising'} edge)..."
        )
        armed_at = time.time()
        head = self._read_exact(4, deadline=time.time() + wait_s)
        if head is None:
            self.abort()
            raise TimeoutError(
                f"trigger never fired within {wait_s:.0f}s — no matching edge on the "
                "trigger channel; check wiring/edge/channel (capture aborted)"
            )
        self._log(f"trigger fired {time.time() - armed_at:.1f}s after arming")
        import struct as _struct

        (count,) = _struct.unpack("<I", head)
        nbytes = count * protocol.bytes_per_sample(mode)
        data = self._read_exact(nbytes, deadline=time.time() + read_timeout_s)
        if data is None:
            self.abort()
            raise RuntimeError("incomplete sample stream from device")
        self._read_exact(1, deadline=time.time() + 5)  # stamp-length byte
        return data

    def abort(self) -> None:
        """Stop a running/pending capture and drain leftover bytes so the device
        returns to listening (mirrors the stock driver's StopCapture)."""
        try:
            self.sp.write(bytes([protocol.ABORT_BYTE]))
            self.sp.flush()
            time.sleep(self._abort_settle_s)
            self.sp.timeout = 0.2
            for _ in range(50):
                if not self.sp.read(65536):
                    break
            self.sp.reset_input_buffer()
            self.sp.reset_output_buffer()
        except Exception:
            pass
