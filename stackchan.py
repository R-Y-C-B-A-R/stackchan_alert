#!/usr/bin/env python3
"""Control StackChan (M5Stack CoreS3) via USB serial."""
import serial
import json
import sys
import time
import glob
import grp
import os
import argparse

BAUD = 115200


# ─── Serial connection with hardware reset ──────────────────────────────────

class StackChanConn:
    """Serial connection with RTS-based hardware reset support."""

    def __init__(self, port: str, baud: int = 115200):
        self.ser = serial.Serial(port, baud, timeout=0.1)
        time.sleep(0.1)
        self.ser.reset_input_buffer()

    def hardware_reset(self):
        """Reset ESP32 via RTS (same as esptool). Triggers board_power_init → VM_EN."""
        self.ser.dtr = False
        self.ser.rts = True   # EN pin low → chip in reset
        time.sleep(0.1)
        self.ser.rts = False  # EN pin high → normal boot
        self.ser.dtr = False
        time.sleep(0.05)
        self.ser.reset_input_buffer()

    def read_json_object(self, timeout: float = 1.0):
        """Read bytes until a balanced { } JSON object is assembled.

        Ignores everything before the first '{' so IDF log lines and echo
        bytes are skipped automatically.
        """
        deadline = time.time() + timeout
        buf = ''
        depth = 0
        in_str = False
        esc = False

        while time.time() < deadline:
            b = self.ser.read(1)
            if not b:
                continue
            ch = b.decode(errors='replace')

            if not buf and ch != '{':
                continue  # skip until opening brace

            buf += ch

            if esc:
                esc = False
                continue
            if ch == '\\' and in_str:
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue

            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(buf)
                    except json.JSONDecodeError:
                        buf = ''  # garbled object – reset and try again
        return None

    def send(self, cmd: dict):
        """Send a JSON command."""
        self.ser.write(json.dumps(cmd).encode() + b"\n")
        self.ser.flush()

    def read_response(self, timeout: float = 5.0):
        """Read JSON response (skips non-JSON lines)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = deadline - time.time()
            obj = self.read_json_object(timeout=min(1.0, remaining))
            if obj is not None:
                return obj
        return None

    def close(self):
        self.ser.close()


# ─── Port detection (Linux) ───────────────────────────────────────────────

def check_dialout():
    """Warn if the current session doesn't have dialout access."""
    if not sys.platform.startswith("linux"):
        return
    user = os.getenv("USER") or os.getenv("LOGNAME") or "$(whoami)"
    try:
        gid = grp.getgrnam("dialout").gr_gid
    except KeyError:
        return  # no dialout group on this system

    if gid in os.getgroups():
        return  # already active in this session

    try:
        members = grp.getgrnam("dialout").gr_mem
    except KeyError:
        members = []

    if user in members:
        print("[INFO] dialout group is not active in this session.")
        print("       Quick fix (current shell only):  newgrp dialout")
        print("       Permanent fix:                   log out and back in")
    else:
        print(f"[INFO] User '{user}' is not in the dialout group.")
        print(f"       Run: sudo usermod -aG dialout {user}")
        print("       Then log out and back in (or: newgrp dialout)")


def find_ports():
    """Return candidate serial ports on Linux, ESP32-S3 (VID 303a) first."""
    if not sys.platform.startswith("linux"):
        return []
    preferred, others = [], []
    for pattern in ["/dev/ttyACM*", "/dev/ttyUSB*"]:
        for port in sorted(glob.glob(pattern)):
            name = os.path.basename(port)
            is_esp32s3 = False
            try:
                with open(f"/sys/class/tty/{name}/device/uevent") as f:
                    if "303a" in f.read().lower():  # Espressif VID
                        is_esp32s3 = True
            except OSError:
                pass
            (preferred if is_esp32s3 else others).append(port)
    return preferred + others


def resolve_port(port_arg):
    """Return the port to use; auto-detect if not specified."""
    if port_arg:
        return port_arg

    check_dialout()
    candidates = find_ports()

    if not candidates:
        print("[ERROR] No serial ports found. Is the StackChan connected via USB?",
              file=sys.stderr)
        if sys.platform.startswith("linux"):
            print("        Check: ls /dev/ttyACM*", file=sys.stderr)
        sys.exit(1)

    port = candidates[0]
    if len(candidates) == 1:
        print(f"[INFO] Auto-detected port: {port}")
    else:
        print(f"[INFO] Multiple ports found: {', '.join(candidates)}")
        print(f"[INFO] Using {port}  (use --port to override)")
    return port


# ─── Serial communication ─────────────────────────────────────────────────

def send(cmd: dict, port: str, timeout: float = 5.0) -> dict:
    try:
        conn = StackChanConn(port, BAUD)
        try:
            # Wait for boot banner; if device is already running, reset it so
            # board_power_init() runs again and VM_EN is asserted.
            deadline = time.time() + 3.0
            boot_seen = False
            while time.time() < deadline:
                remaining = deadline - time.time()
                obj = conn.read_json_object(timeout=min(0.5, remaining))
                if obj is not None:
                    if "ready" in obj or "boot" in obj:
                        boot_seen = True
                        break

            if not boot_seen:
                # Device already running – reset via RTS to re-init VM_EN
                conn.hardware_reset()
                deadline = time.time() + 6.0
                while time.time() < deadline:
                    remaining = deadline - time.time()
                    obj = conn.read_json_object(timeout=min(1.0, remaining))
                    if obj is not None:
                        if "ready" in obj or "boot" in obj:
                            boot_seen = True
                            break

            # Send command
            conn.send(cmd)
            result = conn.read_response(timeout=timeout)
            return result if result else {"ok": False, "err": "timeout"}

        finally:
            conn.close()
    except serial.SerialException as e:
        if "Permission denied" in str(e):
            check_dialout()
        return {"ok": False, "err": str(e)}


# ─── Pattern execution ────────────────────────────────────────────────────

def run_pattern(path, port, loop_override=False, deadline=None):
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
                              "tilt": step.get("tilt", 0)}, port=port)
                    if not r.get("ok"):
                        print(json.dumps(r))
                        sys.exit(1)
                if "face" in step:
                    send({"cmd": "face", "expr": step["face"]}, port=port)
                if "text" in step:
                    send({"cmd": "print", "text": step["text"]}, port=port)
                wait = step.get("duration", 200) / 1000.0
                if deadline:
                    wait = min(wait, max(0.0, deadline - time.time()))
                time.sleep(wait)
            if not do_loop:
                break
        return True
    except KeyboardInterrupt:
        return False


# ─── CLI ──────────────────────────────────────────────────────────────────

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
  %(prog)s --port /dev/ttyACM1 face happy
  %(prog)s ports

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

    p.add_argument("--port", metavar="DEV",
                   help="Serial port (e.g. /dev/ttyACM0, COM3). "
                        "Auto-detected if omitted.")

    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    # ── ports ─────────────────────────────────────────────────────────────
    sub.add_parser("ports", help="List detected serial ports (Linux)")

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
                    help="Horizontal degrees from center (positive=right, default: 0)")
    mv.add_argument("--tilt", type=int, default=0, metavar="DEG",
                    help="Vertical degrees from center (positive=up, default: 0)")

    # ── center ────────────────────────────────────────────────────────────
    sub.add_parser("center", help="Return head servos to center position")

    # ── run ───────────────────────────────────────────────────────────────
    ru = sub.add_parser("run", help="Execute a movement pattern from a JSON file")
    ru.add_argument("file", help="Path to pattern JSON (see patterns/ directory)")
    ru.add_argument("--loop", action="store_true",
                    help="Loop until Ctrl-C "
                         "(overrides the loop flag inside the pattern file)")

    # ── scan ──────────────────────────────────────────────────────────────
    sub.add_parser("scan", help="Servo wiggle test (verifies SCS communication)")

    # ── ping ──────────────────────────────────────────────────────────────
    sub.add_parser("ping", help="Read servo positions (diagnostic: -2=no RX, -1=no response)")

    # ── diag ──────────────────────────────────────────────────────────────
    sub.add_parser("diag", help="Run UART diagnostics on servo interface")

    # ─────────────────────────────────────────────────────────────────────
    args = p.parse_args()

    # ── ports: no device connection needed ────────────────────────────────
    if args.cmd == "ports":
        check_dialout()
        candidates = find_ports()
        if candidates:
            print("Detected ports:")
            for c in candidates:
                tag = "  [ESP32-S3]" if _is_esp32s3(c) else ""
                print(f"  {c}{tag}")
        else:
            print("No serial ports found.")
        return

    port = resolve_port(args.port)

    # ── run ───────────────────────────────────────────────────────────────
    if args.cmd == "run":
        ok = run_pattern(args.file, port=port, loop_override=args.loop)
        if ok:
            print(json.dumps({"ok": True}))
        else:
            send({"cmd": "center"}, port=port)
            print(json.dumps({"ok": True, "info": "interrupted, centered"}))
        return

    # scan needs extra time (wiggle test takes ~1.5 s on device)
    # ping needs extra time for two serial reads (2 × 15 ms + margin)
    send_timeout = 5.0
    if args.cmd == "scan":
        send_timeout = 10.0
    elif args.cmd == "ping":
        send_timeout = 3.0

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
            payload["nervous"] = False
    elif args.cmd == "move":
        payload["pan"]  = args.pan
        payload["tilt"] = args.tilt

    result = send(payload, port=port, timeout=send_timeout)
    print(json.dumps(result))

    # After alarm start, optionally drive head via motion pattern
    if args.cmd == "alarm" and getattr(args, "motion", None) and result.get("ok"):
        deadline = time.time() + args.duration
        ok = run_pattern(args.motion, port=port, loop_override=True, deadline=deadline)
        if not ok:
            send({"cmd": "stopalarm"}, port=port)
        send({"cmd": "center"}, port=port)

    sys.exit(0 if result.get("ok") else 1)


def _is_esp32s3(port):
    name = os.path.basename(port)
    try:
        with open(f"/sys/class/tty/{name}/device/uevent") as f:
            return "303a" in f.read().lower()
    except OSError:
        return False


if __name__ == "__main__":
    main()
