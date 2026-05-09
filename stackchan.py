#!/usr/bin/env python3
"""Send JSON commands to StackChan over USB serial."""
import serial
import json
import sys
import time
import argparse

PORT = "/dev/ttyACM0"
BAUD = 115200


def send(cmd: dict, timeout: float = 5.0) -> dict:
    with serial.Serial(PORT, BAUD, timeout=1.0) as s:
        s.write(json.dumps(cmd).encode() + b"\n")
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = s.readline().decode(errors="replace").strip()
            if not line:
                continue
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    return {"ok": False, "err": f"bad json: {line}"}
            # Skip ESP-IDF log lines and other non-JSON output
        return {"ok": False, "err": "timeout"}


def main():
    p = argparse.ArgumentParser(description="Control StackChan via USB")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("print", help="Show text on status line")
    pr.add_argument("text")

    sub.add_parser("clear", help="Clear status line")

    pl = sub.add_parser("play", help="Play a WAV file from flash")
    pl.add_argument("file", help="Filename without .wav extension")

    fa = sub.add_parser("face", help="Set avatar expression")
    fa.add_argument("expr", choices=["neutral", "happy", "sad", "angry", "doubt", "sleepy"])

    al = sub.add_parser("alarm", help="Red blinking alarm with scrolling text and sound")
    al.add_argument("text", help="Alarm message (scrolls if too long)")
    al.add_argument("--duration", type=int, default=5, metavar="SEC",
                    help="Duration in seconds (default: 5)")

    sub.add_parser("stopalarm", help="Stop a running alarm immediately")

    args = p.parse_args()

    payload = {"cmd": args.cmd}
    if args.cmd == "print":
        payload["text"] = args.text
    elif args.cmd == "play":
        payload["file"] = args.file
    elif args.cmd == "face":
        payload["expr"] = args.expr
    elif args.cmd == "alarm":
        payload["text"] = args.text
        payload["duration"] = args.duration

    result = send(payload)
    print(json.dumps(result))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
