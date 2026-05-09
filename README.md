# StackChan Alert Firmware

Custom firmware for the **M5Stack CoreS3 StackChan** that replaces the stock firmware
with a USB-serial command interface. Supports displaying text, playing WAV audio,
setting avatar expressions, and triggering a full alert mode with a blinking red
background, scrolling text, and looping alarm sound.

## Hardware

| Component | Details |
|-----------|---------|
| Board | M5Stack CoreS3 (StackChan Kickstarter Kit) |
| SoC | ESP32-S3 dual-core LX7 240 MHz |
| Flash | 16 MB |
| PSRAM | 8 MB |
| Display | ILI9342 2" 320×240 |
| Audio | 1 W speaker |
| USB | Native ESP32-S3 USB CDC — no UART bridge |
| Device node (Linux) | `/dev/ttyACM0` |

## Why custom firmware?

The stock firmware (based on [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32))
exposes no USB command interface. Control is cloud-only via WebSocket/MCP. This
firmware replaces it with a simple JSON-over-serial protocol.

## Features

- JSON command protocol over USB serial (115200 baud)
- Avatar face with expressions (happy, sad, angry, doubt, sleepy, neutral)
- Status text in the bottom bar
- WAV playback from onboard LittleFS
- **Alert mode**: blinking red/black background, scrolling text in speech bubble,
  looping alarm sound — auto-stops after a configurable duration

## Quick start

### 1. Prerequisites

```bash
pip install platformio pyserial
# Make sure your user is in the dialout group:
sudo usermod -aG dialout $USER   # then re-login
```

### 2. Flash the firmware

```bash
cd firmware
pio run --target upload
```

### 3. Upload the filesystem (WAV file)

```bash
pio run --target uploadfs
```

### 4. Control via Python

```bash
# Show avatar expression
python3 stackchan.py face happy

# Print status text
python3 stackchan.py print "System OK"

# Play a WAV file (filename without extension)
python3 stackchan.py play facilityalarm

# Trigger alert for 10 seconds
python3 stackchan.py alarm "FACILITY ALARM – Please evacuate immediately!" --duration 10

# Stop alert early
python3 stackchan.py stopalarm
```

## Command reference

| Command | Parameters | Description |
|---------|-----------|-------------|
| `face` | `neutral\|happy\|sad\|angry\|doubt\|sleepy` | Set avatar expression |
| `print` | `<text>` | Show text in the status bar |
| `clear` | — | Clear the status bar |
| `play` | `<filename>` (without `.wav`) | Play a WAV file from flash |
| `alarm` | `<text>` `--duration <sec>` | Start alert mode |
| `stopalarm` | — | Stop alert immediately |

## JSON protocol (raw)

Commands are newline-terminated JSON objects sent to `/dev/ttyACM0` at 115200 baud.
The device responds with `{"ok":true}` or `{"ok":false,"err":"..."}`.

```json
{"cmd":"alarm","text":"FIRE!","duration":30}
{"cmd":"face","expr":"happy"}
{"cmd":"print","text":"Hello"}
{"cmd":"play","file":"facilityalarm"}
{"cmd":"clear"}
{"cmd":"stopalarm"}
```

On boot the device sends: `{"ready":true}`

## Partition layout

| Name | Type | Offset | Size | Notes |
|------|------|--------|------|-------|
| nvs | data/nvs | 0x9000 | 20 KB | |
| phy_init | data/phy | 0xE000 | 4 KB | |
| app0 | app/ota_0 | 0x10000 | 2 MB | Firmware |
| littlefs | data/spiffs | 0x210000 | ~6 MB | WAV files |

## Restoring the original firmware

See [`backup/README.md`](backup/README.md) for backup and restore instructions.

## Repository structure

```
stackchan_alert/
├── firmware/          PlatformIO project (ESP32 firmware)
│   ├── src/main.cpp   Firmware source
│   ├── data/          Files uploaded to LittleFS (WAV audio)
│   ├── platformio.ini Board config and dependencies
│   └── partitions.csv Flash partition table
├── backup/            Checksums of full-flash backups (.bin excluded from git)
├── stackchan.py       Python CLI to control the device over USB
├── CLAUDE.md          AI context for development sessions
└── README.md          This file
```
