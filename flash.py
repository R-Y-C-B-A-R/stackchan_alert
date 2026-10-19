#!/usr/bin/env python3
"""Flash StackChan firmware and/or LittleFS to /dev/stackchan (or auto-detected port).

Usage:
  ./flash.py              # build + flash firmware + filesystem
  ./flash.py --fw         # build + flash firmware only
  ./flash.py --fs         # build + flash filesystem only
  ./flash.py --no-build   # skip pio build, use existing .bin
  ./flash.py --port /dev/ttyACM1
  ./flash.py --monitor    # open serial monitor after flashing
"""

import argparse
import glob
import os
import subprocess
import sys
import time

FIRMWARE_DIR = os.path.join(os.path.dirname(__file__), "firmware")
PREFERRED_PORT = "/dev/stackchan"
ESPRESSIF_VID = "303a"
BAUD_MONITOR = 115200


# ─── Port detection ────────────────────────────────────────────────────────────

def find_port() -> str:
    if os.path.exists(PREFERRED_PORT):
        return PREFERRED_PORT
    # Fall back: scan /sys for Espressif VID
    for path in glob.glob("/sys/bus/usb/devices/*/idVendor"):
        try:
            if open(path).read().strip() == ESPRESSIF_VID:
                dev_dir = os.path.dirname(path)
                tty_dirs = glob.glob(f"{dev_dir}/**/tty/tty*", recursive=True)
                if tty_dirs:
                    tty = os.path.basename(tty_dirs[0])
                    return f"/dev/{tty}"
        except OSError:
            pass
    # Last resort
    candidates = sorted(glob.glob("/dev/ttyACM*"))
    if candidates:
        return candidates[0]
    return ""


# ─── Runner ───────────────────────────────────────────────────────────────────

def run(cmd: list[str], label: str) -> bool:
    print(f"\n{'─'*60}")
    print(f"  {label}")
    print(f"{'─'*60}")
    result = subprocess.run(cmd, cwd=FIRMWARE_DIR)
    if result.returncode != 0:
        print(f"\n✗ {label} FAILED (exit {result.returncode})", file=sys.stderr)
        return False
    print(f"\n✓ {label} OK")
    return True


def pio(target: str, port: str) -> list[str]:
    cmd = ["pio", "run", "--target", target, "--upload-port", port]
    return cmd


# ─── Monitor ──────────────────────────────────────────────────────────────────

def monitor(port: str):
    print(f"\n{'─'*60}")
    print(f"  Serial monitor  {port}  {BAUD_MONITOR} baud  (Ctrl-C to exit)")
    print(f"{'─'*60}\n")
    try:
        import serial
    except ImportError:
        print("pyserial not installed — run:  pip install pyserial", file=sys.stderr)
        return
    try:
        ser = serial.Serial(port, BAUD_MONITOR, timeout=0.2)
        while True:
            line = ser.readline()
            if line:
                print(line.decode(errors="replace"), end="")
    except KeyboardInterrupt:
        print("\n(monitor stopped)")
    finally:
        ser.close()


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Flash StackChan firmware / filesystem")
    parser.add_argument("--fw",       action="store_true", help="Flash firmware only")
    parser.add_argument("--fs",       action="store_true", help="Flash filesystem only")
    parser.add_argument("--no-build", action="store_true", help="Skip pio build step")
    parser.add_argument("--monitor",  action="store_true", help="Open serial monitor after flash")
    parser.add_argument("--port",     default="",          help="Serial port (default: auto)")
    args = parser.parse_args()

    # If neither --fw nor --fs, do both
    do_fw = args.fw or (not args.fw and not args.fs)
    do_fs = args.fs or (not args.fw and not args.fs)

    port = args.port or find_port()
    if not port:
        print("✗ No StackChan port found. Connect the device or pass --port.", file=sys.stderr)
        sys.exit(1)
    print(f"Port: {port}")

    ok = True

    # Build
    if not args.no_build:
        ok = run(["pio", "run"], "Build firmware") and ok

    # Flash firmware
    if ok and do_fw:
        ok = run(pio("upload", port), "Flash firmware") and ok

    # Flash filesystem
    if ok and do_fs:
        ok = run(pio("uploadfs", port), "Flash filesystem (LittleFS)") and ok

    if not ok:
        sys.exit(1)

    # Boot check
    if do_fw or do_fs:
        print(f"\nWaiting for boot banner on {port} …", end="", flush=True)
        try:
            import serial
            ser = serial.Serial(port, BAUD_MONITOR, timeout=0.3)
            deadline = time.time() + 10
            while time.time() < deadline:
                line = ser.readline().decode(errors="replace").strip()
                if '"ready"' in line:
                    print(f"\n✓ Device ready: {line}")
                    break
                if line:
                    print(f"\n  {line}", end="", flush=True)
            else:
                print("\n(no ready banner within 10 s)")
            ser.close()
        except ImportError:
            print("\n(pyserial not installed, skipping boot check)")
        except Exception as e:
            print(f"\n(boot check error: {e})")

    if args.monitor:
        monitor(port)


if __name__ == "__main__":
    main()
