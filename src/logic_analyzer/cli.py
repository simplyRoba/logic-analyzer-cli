"""Command-line interface: ``la-capture``.

A headless capture client for the gusmanb LogicAnalyzer that, unlike the stock
``TerminalCapture``, validates the request, checks the device's reply, and always
times out instead of hanging.
"""
from __future__ import annotations

import argparse
import sys

from . import protocol
from .device import Device


def _csv(path: str, names: list[str], channel_count: int, data: bytes, mode: int) -> None:
    words = protocol.unpack_samples(data, mode)
    rows = protocol.samples_to_rows(words, channel_count)
    with open(path, "w") as f:
        f.write(",".join(names) + "\n")
        for row in rows:
            f.write(",".join(str(v) for v in row) + "\n")


def _parse_channels(specs: list[str]) -> tuple[list[str], list[int]]:
    names: list[str] = []
    channels: list[int] = []
    for spec in specs:
        name, ch = protocol.parse_channel_spec(spec)
        names.append(name)
        channels.append(ch)
    return names, channels


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="la-capture",
        description=(
            "Headless capture client for the gusmanb LogicAnalyzer "
            "(RP2040/RP2350). Validates the request, checks the device reply, "
            "and always times out instead of hanging like the stock CLI."
        ),
    )
    p.add_argument("output", nargs="?", help="output CSV path (omit with --info)")
    p.add_argument("--port", "-p", default=None,
                   help="serial port (e.g. /dev/ttyACM0); omit to auto-detect")
    p.add_argument("--channel", "-c", action="append", default=[], metavar="NAME=CH",
                   help="capture channel NAME=<1-based analyzer channel>; repeat. "
                        "e.g. -c MOSI=3 -c SCLK=4 -c CS=5")
    p.add_argument("--samplerate", "-r", type=int, default=50_000_000,
                   help="sample rate in Hz (default 50000000)")
    p.add_argument("--pre", type=int, default=None,
                   help="pre-trigger samples (default: max the device allows)")
    p.add_argument("--post", type=int, default=None,
                   help="post-trigger samples (default: fill the rest of the buffer)")
    p.add_argument("--trigger", "-t", metavar="NAME", default=None,
                   help="trigger channel NAME (default: last --channel)")
    p.add_argument("--edge", choices=["rising", "falling"], default="falling",
                   help="trigger edge (default falling)")
    p.add_argument("--wait", "-w", type=float, default=30.0,
                   help="seconds to wait for the trigger before giving up (default 30)")
    p.add_argument("--loop", "-l", type=int, default=1, metavar="N",
                   help="capture N times, re-arming after each trigger; output "
                        "gets -000, -001, … inserted before the extension")
    p.add_argument("--info", "-i", action="store_true",
                   help="handshake and print device info, then exit")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    dev = Device(args.port)
    try:
        info = dev.handshake()
        print(f"device: {info.name}", file=sys.stderr)
        print(
            f"  max {info.max_freq} Hz, buffer {info.buffer_bytes} bytes, "
            f"{info.max_channels} channels",
            file=sys.stderr,
        )
        if args.info:
            return 0
        if not args.channel or not args.output:
            print("error: need --channel specs and an output path (or --info)",
                  file=sys.stderr)
            return 2

        names, channels = _parse_channels(args.channel)
        mode = protocol.capture_mode_for_channels(channels)
        lim = protocol.limits_for(info, mode)

        pre = args.pre if args.pre is not None else lim.max_pre
        post = args.post if args.post is not None else (lim.total_samples - pre)

        errs = protocol.validate_request(info, channels, args.samplerate, pre, post, mode)
        if errs:
            print("request rejected (this is what hangs the stock CLI):", file=sys.stderr)
            for e in errs:
                print("  - " + e, file=sys.stderr)
            print(
                f"  device limits: total {lim.total_samples}, "
                f"pre {lim.min_pre}..{lim.max_pre}, post {lim.min_post}..{lim.max_post}",
                file=sys.stderr,
            )
            return 2

        if args.trigger is None:
            trig_ch = channels[-1]
        elif args.trigger in names:
            trig_ch = channels[names.index(args.trigger)]
        else:
            print(f"error: --trigger {args.trigger!r} is not one of {names}",
                  file=sys.stderr)
            return 2

        print(
            f"capturing {pre}+{post}={pre + post} samples @ {args.samplerate} Hz, "
            f"channels {names} (analyzer {[c + 1 for c in channels]}), loop={args.loop}",
            file=sys.stderr,
        )

        base, dot, ext = args.output.rpartition(".")
        captured: list[bytes] = []
        for i in range(args.loop):
            try:
                data = dev.capture(
                    channels=channels,
                    frequency=args.samplerate,
                    pre=pre,
                    post=post,
                    trigger_channel=trig_ch,
                    falling=(args.edge == "falling"),
                    mode=mode,
                    wait_s=args.wait,
                )
            except TimeoutError as e:
                print(f"[{i}] {e}", file=sys.stderr)
                break
            captured.append(data)
            print(f"[{i}] triggered, {len(data)} samples", file=sys.stderr)

        for i, data in enumerate(captured):
            if args.loop > 1:
                out = f"{base}-{i:03d}.{ext}" if dot else f"{args.output}-{i:03d}"
            else:
                out = args.output
            _csv(out, names, len(channels), data, mode)
            print(f"wrote {out}", file=sys.stderr)

        return 0 if captured else 1
    finally:
        dev.close()


if __name__ == "__main__":
    raise SystemExit(main())
