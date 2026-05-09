#!/usr/bin/env python3
"""Control StackChan (M5Stack CoreS3) via USB serial."""
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
        return {"ok": False, "err": "timeout"}


def run_pattern(path, loop_override=False, deadline=None):
    """Execute a movement pattern JSON file.
    deadline: time.time() value — stop when reached (for alarm --motion).
    Returns False if interrupted by Ctrl-C.
    """
    try:
        with open(path) as f:
            pattern = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({"ok": False, "err": str(e)}))
        sys.exit(1)

    steps = pattern.get("steps", [])
    do_loop = loop_override or pattern.get("loop", False)

    try:
        while True:
            for step in steps:
                if deadline and time.time() >= deadline:
                    return True
                if "pan" in step or "tilt" in step:
                    r = send({"cmd": "move",
                              "pan":  step.get("pan",  0),
                              "tilt": step.get("tilt", 0)})
                    if not r.get("ok"):
                        print(json.dumps(r))
                        sys.exit(1)
                if "face" in step:
                    send({"cmd": "face", "expr": step["face"]})
                if "text" in step:
                    send({"cmd": "print", "text": step["text"]})
                wait = step.get("duration", 200) / 1000.0
                if deadline:
                    wait = min(wait, max(0.0, deadline - time.time()))
                time.sleep(wait)
            if not do_loop:
                break
        return True
    except KeyboardInterrupt:
        return False


def main():
    p = argparse.ArgumentParser(
        prog="stackchan.py",
        description="Control StackChan (M5Stack CoreS3) via USB serial.",
        epilog="""\
examples:
  %(prog)s face happy
  %(prog)s print "System OK"
  %(prog)s play facilityalarm
  %(prog)s alarm "FIRE!" 10
  %(prog)s alarm "FIRE!" 30 --motion patterns/nervous.json
  %(prog)s move --pan 20 --tilt -10
  %(prog)s center
  %(prog)s run patterns/look_around.json
  %(prog)s run patterns/nervous.json --loop

movement pattern JSON format  (see patterns/ directory):
  {
    "name": "example",
    "loop": false,
    "steps": [
      {"pan": 20,  "tilt":  0, "duration": 400},
      {"pan": -20, "tilt": 10, "duration": 400, "face": "happy"},
      {"pan":   0, "tilt":  0, "duration": 300}
    ]
  }
  pan/tilt in degrees from center  (negative = left / down)
  duration in milliseconds
  optional per-step keys: face (expression name), text (status bar)""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    # ── face ──────────────────────────────────────────────────────────────
    fa = sub.add_parser("face", help="Set avatar expression")
    fa.add_argument("expr",
                    choices=["neutral", "happy", "sad", "angry", "doubt", "sleepy"])

    # ── print ─────────────────────────────────────────────────────────────
    pr = sub.add_parser("print", help="Show text in the status bar")
    pr.add_argument("text")

    # ── clear ─────────────────────────────────────────────────────────────
    sub.add_parser("clear", help="Clear the status bar")

    # ── play ──────────────────────────────────────────────────────────────
    pl = sub.add_parser("play", help="Play a WAV file from flash")
    pl.add_argument("file", help="Filename without .wav extension")

    # ── alarm ─────────────────────────────────────────────────────────────
    al = sub.add_parser(
        "alarm",
        help="Alert mode: red blinking background, scrolling text, alarm sound",
        description=(
            "Starts an alert on the device and returns immediately. "
            "The device runs autonomously until the duration expires. "
            "With --motion, Python drives head movement instead of the "
            "built-in nervous jitter."
        ),
    )
    al.add_argument("text",
                    help="Alarm message (scrolls in speech bubble if too long)")
    al.add_argument("duration", type=int, nargs="?", default=5, metavar="SEC",
                    help="Duration in seconds (default: 5)")
    al.add_argument("--motion", metavar="FILE",
                    help="JSON pattern file to loop during alarm "
                         "(disables built-in nervous movement)")

    # ── stopalarm ─────────────────────────────────────────────────────────
    sub.add_parser("stopalarm", help="Stop a running alarm immediately")

    # ── move ──────────────────────────────────────────────────────────────
    mv = sub.add_parser("move", help="Move head servos to a position")
    mv.add_argument("--pan",  type=int, default=0, metavar="DEG",
                    help="Horizontal offset from center in degrees "
                         "(positive=right, negative=left,  default: 0)")
    mv.add_argument("--tilt", type=int, default=0, metavar="DEG",
                    help="Vertical offset from center in degrees "
                         "(positive=up, negative=down,  default: 0)")

    # ── center ────────────────────────────────────────────────────────────
    sub.add_parser("center", help="Return head servos to center position")

    # ── run ───────────────────────────────────────────────────────────────
    ru = sub.add_parser("run", help="Execute a movement pattern from a JSON file")
    ru.add_argument("file", help="Path to pattern JSON (see patterns/ directory)")
    ru.add_argument("--loop", action="store_true",
                    help="Loop until Ctrl-C "
                         "(overrides the loop flag inside the pattern file)")

    # ── scan ──────────────────────────────────────────────────────────────
    sub.add_parser(
        "scan",
        help="Probe GPIO pins to find servo wiring "
             "(watch which pin makes your servo twitch)",
    )

    # ─────────────────────────────────────────────────────────────────────
    args = p.parse_args()

    if args.cmd == "run":
        ok = run_pattern(args.file, loop_override=args.loop)
        if ok:
            print(json.dumps({"ok": True}))
        else:
            send({"cmd": "center"})
            print(json.dumps({"ok": True, "info": "interrupted, centered"}))
        return

    # scan needs extra time (probes 10 pins sequentially)
    send_timeout = 15.0 if args.cmd == "scan" else 5.0

    payload = {"cmd": args.cmd}

    if args.cmd == "face":
        payload["expr"] = args.expr
    elif args.cmd == "print":
        payload["text"] = args.text
    elif args.cmd == "play":
        payload["file"] = args.file
    elif args.cmd == "alarm":
        payload["text"]     = args.text
        payload["duration"] = args.duration
        if args.motion:
            payload["nervous"] = False  # Python controls movement via --motion
    elif args.cmd == "move":
        payload["pan"]  = args.pan
        payload["tilt"] = args.tilt

    result = send(payload, timeout=send_timeout)
    print(json.dumps(result))

    # After alarm start, drive head via motion pattern file if requested
    if args.cmd == "alarm" and getattr(args, "motion", None) and result.get("ok"):
        deadline = time.time() + args.duration
        ok = run_pattern(args.motion, loop_override=True, deadline=deadline)
        if not ok:
            send({"cmd": "stopalarm"})
        send({"cmd": "center"})

    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
