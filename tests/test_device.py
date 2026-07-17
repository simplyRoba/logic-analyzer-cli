"""Device-layer tests using a fake serial port (no hardware).

These drive logic_analyzer.device.Device against an in-memory stand-in that
speaks just enough of the protocol to exercise the handshake, a good capture,
a rejected capture, a trigger timeout, and stuck-device recovery.
"""
import struct

import pytest

import logic_analyzer.device as device_mod
from logic_analyzer.device import Device


class FakeSerial:
    """Minimal pyserial stand-in scripted per test scenario."""

    def __init__(self, scenario):
        self.scenario = scenario
        self.timeout = 1.0
        self._dtr = False
        self.rts = False
        self.toggle_count = 0            # DTR low->high pulses
        self._out = bytearray()          # bytes the client wrote
        self._in = bytearray()           # bytes queued for the client to read
        self.closed = False
        self.reopen_count = 0
        scenario.attach(self)

    @property
    def dtr(self):
        return self._dtr

    @dtr.setter
    def dtr(self, v):
        if v and not self._dtr:
            self.toggle_count += 1
            # persist across reopens (real DTR toggles the physical device)
            self.scenario.toggle_count = getattr(self.scenario, "toggle_count", 0) + 1
        self._dtr = v

    # -- pyserial API surface used by Device --
    def write(self, data):
        self._out += bytes(data)
        self.scenario.on_write(self, bytes(data))
        return len(data)

    def flush(self):
        pass

    def read(self, n=1):
        chunk = bytes(self._in[:n])
        del self._in[:n]
        return chunk

    def readline(self):
        nl = self._in.find(b"\n")
        if nl == -1:
            out = bytes(self._in)
            self._in.clear()
            return out
        out = bytes(self._in[: nl + 1])
        del self._in[: nl + 1]
        return out

    def reset_input_buffer(self):
        self._in.clear()

    def reset_output_buffer(self):
        self._out.clear()

    def close(self):
        self.closed = True

    # -- helpers for scenarios --
    def queue(self, data: bytes):
        self._in += data


HANDSHAKE = (
    b"LOGIC_ANALYZER_PICO_2_V6_0\n"
    b"FREQ:100000000\n"
    b"BLASTFREQ:200000000\n"
    b"BUFFER:393216\n"
    b"CHANNELS:24\n"
)


class Scenario:
    """Base scenario: respond to the ID command with a valid handshake."""

    def __init__(self):
        self.serials = []

    def attach(self, s):
        self.serials.append(s)

    def on_write(self, s, data):
        # ID command is frame([0x00]) == 55 AA 00 AA 55
        if data == bytes([0x55, 0xAA, 0x00, 0xAA, 0x55]):
            s.queue(HANDSHAKE)


@pytest.fixture
def patch_serial(monkeypatch):
    """Patch device_mod.serial.Serial to build a FakeSerial from the active scenario,
    and make port discovery deterministic."""
    holder = {}

    class FakeSerialModule:
        @staticmethod
        def Serial(port, baud, timeout=10):
            s = FakeSerial(holder["scenario"])
            s.timeout = timeout
            return s

    monkeypatch.setattr(device_mod, "serial", FakeSerialModule)
    monkeypatch.setattr(device_mod, "find_port", lambda hint=None: "/dev/fake")

    def use(scenario):
        holder["scenario"] = scenario
    return use


def test_handshake(patch_serial):
    patch_serial(Scenario())
    dev = Device(port="/dev/fake", log=lambda m: None, abort_settle_s=0)
    info = dev.handshake()
    assert info.name == "LOGIC_ANALYZER_PICO_2_V6_0"
    assert info.buffer_bytes == 393216
    assert info.max_channels == 24


class GoodCapture(Scenario):
    """After handshake, a capture command yields CAPTURE_STARTED then samples."""

    def __init__(self, samples: bytes):
        super().__init__()
        self.samples = samples

    def on_write(self, s, data):
        super().on_write(s, data)
        # capture command starts with frame prefix 55 AA 01 ...
        if len(data) > 3 and data[0] == 0x55 and data[1] == 0xAA and data[2] == 0x01:
            s.queue(b"CAPTURE_STARTED\n")
            s.queue(struct.pack("<I", len(self.samples)))
            s.queue(self.samples)
            s.queue(bytes([0]))  # stamp length


def test_capture_roundtrip(patch_serial):
    payload = bytes([0b001, 0b010, 0b100, 0b111])
    patch_serial(GoodCapture(payload))
    dev = Device(port="/dev/fake", log=lambda m: None, abort_settle_s=0)
    data = dev.capture(
        channels=[2, 3, 4], frequency=50_000_000, pre=2, post=2,
        trigger_channel=4, falling=True, mode=0, wait_s=5,
    )
    assert data == payload


class RejectedCapture(Scenario):
    def on_write(self, s, data):
        super().on_write(s, data)
        if len(data) > 3 and data[2] == 0x01:
            s.queue(b"CAPTURE_ERROR\n")


def test_capture_rejected_raises(patch_serial):
    patch_serial(RejectedCapture())
    dev = Device(port="/dev/fake", log=lambda m: None, abort_settle_s=0)
    with pytest.raises(RuntimeError, match="did not start capture"):
        dev.capture(
            channels=[2], frequency=50_000_000, pre=2, post=2,
            trigger_channel=2, falling=True, mode=0, wait_s=5,
        )


class NoTrigger(Scenario):
    """CAPTURE_STARTED but the trigger never fires (no sample count follows)."""

    def on_write(self, s, data):
        super().on_write(s, data)
        if len(data) > 3 and data[2] == 0x01:
            s.queue(b"CAPTURE_STARTED\n")
            # deliberately queue nothing else -> read_exact(4) times out


def test_capture_timeout_raises(patch_serial):
    patch_serial(NoTrigger())
    dev = Device(port="/dev/fake", log=lambda m: None, abort_settle_s=0)
    with pytest.raises(TimeoutError):
        dev.capture(
            channels=[2], frequency=50_000_000, pre=2, post=2,
            trigger_channel=2, falling=True, mode=0, wait_s=0.3,
        )


class StuckThenRecovers(Scenario):
    """First ID attempt gets no reply (stuck); after an abort it answers."""

    def __init__(self):
        super().__init__()
        self.seen_abort = False

    def on_write(self, s, data):
        if data == bytes([0xFF]):
            self.seen_abort = True
            return
        if data == bytes([0x55, 0xAA, 0x00, 0xAA, 0x55]) and self.seen_abort:
            s.queue(HANDSHAKE)
        # before the abort, stay silent on ID -> triggers recovery path


def test_recovery_from_stuck_device(patch_serial):
    sc = StuckThenRecovers()
    patch_serial(sc)
    dev = Device(port="/dev/fake", log=lambda m: None, abort_settle_s=0,
                 recovery_settle_s=0)
    assert dev.handshake().name.startswith("LOGIC_ANALYZER")
    assert sc.seen_abort


class DeepWedge(Scenario):
    """Answers ID only after enough DTR toggles (simulates a hard wedge that a
    plain abort+retry can't clear but a CDC line-state reset can)."""

    def __init__(self, needed_toggles=2):
        super().__init__()
        self.needed_toggles = needed_toggles

    def on_write(self, s, data):
        if data == bytes([0x55, 0xAA, 0x00, 0xAA, 0x55]):
            if getattr(self, "toggle_count", 0) >= self.needed_toggles:
                s.queue(HANDSHAKE)
        # otherwise silent


def test_recovery_needs_dtr_toggle(patch_serial):
    sc = DeepWedge(needed_toggles=2)
    patch_serial(sc)
    dev = Device(port="/dev/fake", log=lambda m: None, abort_settle_s=0,
                 recovery_settle_s=0)
    assert dev.handshake().name.startswith("LOGIC_ANALYZER")
