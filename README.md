# logic-analyzer-cli

A small, headless capture client for the [gusmanb
LogicAnalyzer](https://github.com/gusmanb/logicanalyzer) (the RP2040/RP2350
firmware + its `TerminalCapture`/desktop software). It talks the same USB-serial
protocol but is meant to be scripted: arm a capture, wait for a trigger, and
write a CSV — from the command line or from Python.

## Why

The stock `TerminalCapture` CLI has a habit of **arming and then hanging
forever**. Its capture command ignores the return value of its own
`StartCapture`, and it has no trigger timeout — so if the device rejects the
request (bad parameters, or a settings file in the wrong shape) or the trigger
simply never fires, it still prints “Capture started” and blocks indefinitely.
From the outside this is indistinguishable from “armed but the trigger never
fired.”

This client fixes that:

- **Validates the request** against the device’s reported limits *before*
  sending it (the same checks the driver does internally), so a bad request is
  reported, not swallowed.
- **Checks the device reply** (`CAPTURE_STARTED` vs `CAPTURE_ERROR`).
- **Always times out** waiting for the trigger, and aborts the device cleanly so
  the next run starts fresh — and prints *when* the trigger actually fired.
- **Auto-detects the serial port** and **self-recovers a stuck device**
  (abort + reopen + retry) instead of making you replug it.
- **Loops**: re-arm immediately after each trigger to grab a sequence of
  consecutive transactions.

The protocol details were read from the upstream project’s source (the C#
`SharedDriver` and the `LogicAnalyzer_V2` firmware). This tool is independent and
unaffiliated.

## Install

Install directly from GitHub:

```bash
pip install git+ssh://git@github.com/simplyRoba/logic-analyzer-cli.git
# or over HTTPS:
pip install git+https://github.com/simplyRoba/logic-analyzer-cli.git
```

This puts the `la-capture` command on your PATH. To develop against a local
checkout, use an editable install instead: `pip install -e .` from the repo root.

Requires Python ≥ 3.9 and `pyserial`. Use a 3.3 V logic level to the analyzer.

## Usage

Print device info (handshake only):

```bash
la-capture --info
```

Capture three channels, triggering on a falling edge of CS, into a CSV:

```bash
la-capture out.csv -c MOSI=3 -c SCLK=4 -c CS=5 \
  --samplerate 50000000 --trigger CS --edge falling --wait 30
```

- `--channel NAME=CH` maps a friendly name to a **1-based** analyzer channel (as
  labelled on the board). Repeat it; the CSV columns use these names.
- `--trigger NAME` picks the trigger channel (defaults to the last `--channel`);
  `--edge rising|falling`.
- `--pre` / `--post` set the sample split; by default pre-trigger is the maximum
  the device allows and post-trigger fills the rest of the buffer. **Hardware
  limit:** pre-trigger is capped at `buffer / 10`.
- `--loop N` captures N times, re-arming after each trigger; the output filename
  gets `-000`, `-001`, … inserted before the extension.
- `--port` is optional; the port is auto-detected if omitted.

The CSV has one column per channel (header = your names) and one row per sample,
each cell `0` or `1`.

### As a library

```python
from logic_analyzer.device import Device

with Device() as dev:
    info = dev.handshake()
    data = dev.capture(
        channels=[2, 3, 4], frequency=50_000_000,
        pre=2, post=100_000, trigger_channel=4,
        falling=True, mode=0, wait_s=30,
    )
```

The pure protocol logic (framing, request packing, limit validation, sample
decoding) lives in `logic_analyzer.protocol` and has no I/O dependency.

## Tested devices

Developed against an RP2350 board reporting `LOGIC_ANALYZER_PICO_2_V6_0`
(100 MHz max, 393216-byte buffer, 24 channels). Other gusmanb builds that speak
the same protocol should work; the client reads the device’s own limits at
handshake. Only the single-device **edge** trigger is implemented so far
(no pattern/complex/blast triggers, no multi-device).

## Development

```bash
pip install -e ".[dev]"
pytest
```

The unit tests cover the protocol and the device layer against an in-memory fake
serial — no hardware required. A quick **manual hardware smoke test**:

```bash
la-capture --info                       # should print the device banner + limits
la-capture smoke.csv -c A=1 --wait 5    # toggle channel 1; expect a CSV, or a
                                        # clean timeout message (never a hang)
```

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
