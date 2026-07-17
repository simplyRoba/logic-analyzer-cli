"""Unit tests for the pure protocol logic (no hardware)."""
import struct

import pytest

from logic_analyzer import protocol as p


def test_frame_wraps_with_start_and_end_markers():
    out = p.frame(bytes([0x01, 0x02]))
    assert out[:2] == bytes([0x55, 0xAA])
    assert out[-2:] == bytes([0xAA, 0x55])
    assert out[2:-2] == bytes([0x01, 0x02])


def test_frame_escapes_reserved_bytes():
    # 0xAA, 0x55, 0xF0 must be escaped as 0xF0, byte ^ 0xF0.
    out = p.frame(bytes([0xAA, 0x55, 0xF0, 0x00]))
    body = out[2:-2]
    assert body == bytes([0xF0, 0xAA ^ 0xF0, 0xF0, 0x55 ^ 0xF0, 0xF0, 0xF0 ^ 0xF0, 0x00])


def test_id_frame():
    assert p.id_frame() == p.frame(bytes([0x00]))


def test_capture_request_is_48_bytes():
    req = p.build_capture_request(
        channels=[2, 3, 4], frequency=50_000_000, pre=2, post=100,
        trigger_channel=4, falling=True, mode=0,
    )
    assert len(req) == 48


def test_capture_request_field_layout():
    req = p.build_capture_request(
        channels=[2, 3, 4], frequency=50_000_000, pre=10, post=200,
        trigger_channel=4, falling=True, mode=0,
    )
    (ttype, trig, invcnt, tval, chans, ccount, freq, pre, post,
     loop, measure, mode) = struct.unpack("<BBBxH24sBxIIIBBBx", req)
    assert ttype == 0            # edge
    assert trig == 4
    assert invcnt == 1           # falling
    assert tval == 0
    assert chans[:3] == bytes([2, 3, 4])
    assert chans[3:] == bytes(21)
    assert ccount == 3
    assert freq == 50_000_000
    assert pre == 10
    assert post == 200
    assert loop == 0
    assert measure == 0
    assert mode == 0


def test_rising_edge_sets_inverted_zero():
    req = p.build_capture_request(
        channels=[0], frequency=1000, pre=2, post=2,
        trigger_channel=0, falling=False, mode=0,
    )
    assert req[2] == 0  # invertedOrCount


@pytest.mark.parametrize("channels,expected", [([0, 7], 0), ([0, 8], 1), ([0, 15], 1), ([16], 2), ([0, 23], 2)])
def test_capture_mode_for_channels(channels, expected):
    assert p.capture_mode_for_channels(channels) == expected


@pytest.mark.parametrize("mode,bps", [(0, 1), (1, 2), (2, 4)])
def test_bytes_per_sample(mode, bps):
    assert p.bytes_per_sample(mode) == bps


def test_device_info_from_handshake():
    info = p.DeviceInfo.from_handshake([
        "LOGIC_ANALYZER_PICO_2_V6_0",
        "FREQ:100000000",
        "BLASTFREQ:200000000",
        "BUFFER:393216",
        "CHANNELS:24",
    ])
    assert info.name == "LOGIC_ANALYZER_PICO_2_V6_0"
    assert info.max_freq == 100_000_000
    assert info.blast_freq == 200_000_000
    assert info.buffer_bytes == 393216
    assert info.max_channels == 24


def test_device_info_rejects_bad_id():
    with pytest.raises(ValueError):
        p.DeviceInfo.from_handshake(["GARBAGE", "FREQ:1", "BLASTFREQ:1", "BUFFER:1", "CHANNELS:1"])


def _info():
    return p.DeviceInfo("LOGIC_ANALYZER_PICO_2_V6_0", 100_000_000, 200_000_000, 393216, 24)


def test_limits_8_channel():
    lim = p.limits_for(_info(), mode=0)
    assert lim.total_samples == 393216
    assert lim.max_pre == 39321          # buffer/10
    assert lim.max_post == 393214
    assert lim.min_freq == (100_000_000 * 2) // 65535


def test_validate_accepts_good_request():
    assert p.validate_request(_info(), [2, 3, 4], 50_000_000, pre=39321, post=100000, mode=0) == []


def test_validate_flags_pretrigger_over_buffer_tenth():
    errs = p.validate_request(_info(), [2, 3, 4], 50_000_000, pre=196608, post=100, mode=0)
    assert any("pre-trigger" in e for e in errs)


def test_validate_flags_out_of_range_channel():
    errs = p.validate_request(_info(), [0, 30], 50_000_000, pre=2, post=100, mode=2)
    assert any("channel out of range" in e for e in errs)


def test_validate_flags_bad_frequency():
    errs = p.validate_request(_info(), [0], 999_999_999, pre=2, post=100, mode=0)
    assert any("frequency" in e for e in errs)


def test_unpack_samples_8bit():
    assert p.unpack_samples(bytes([0x00, 0x05, 0xFF]), mode=0) == [0, 5, 255]


def test_unpack_samples_16bit():
    raw = struct.pack("<HH", 0x1234, 0xABCD)
    assert p.unpack_samples(raw, mode=1) == [0x1234, 0xABCD]


def test_samples_to_rows_bit_mapping():
    # word bit i = channel i (the firmware repacks selected channels to low bits)
    rows = p.samples_to_rows([0b000, 0b001, 0b010, 0b101], channel_count=3)
    assert rows == [(0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 0, 1)]


def test_parse_channel_spec():
    assert p.parse_channel_spec("CS=5") == ("CS", 4)  # 1-based -> 0-based


def test_parse_channel_spec_rejects_bad():
    with pytest.raises(ValueError):
        p.parse_channel_spec("noequals")
