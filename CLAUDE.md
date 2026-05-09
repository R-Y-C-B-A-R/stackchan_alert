# StackChan Alert — Entwicklungskontext für Claude

## Projektüberblick

Dieses Repo enthält eine eigene Firmware für den **M5Stack CoreS3 im StackChan-Kickstarter-Kit**
sowie ein Python-CLI zur Steuerung über USB. Ziel: Text auf das Display ausgeben,
WAV-Sounds abspielen und einen Alarm mit blinkendem Hintergrund auslösen — alles
über USB vom Linux-Host aus.

## Hardware

- **Modell**: M5Stack CoreS3 im StackChan-Kickstarter-Kit
- **SoC**: ESP32-S3 (dual-core LX7 240 MHz), Chip-Rev v0.2
- **Flash**: 16 MB, **PSRAM**: 8 MB (OPI PSRAM)
- **Display**: ILI9342, 2", 320×240, Landscape (Rotation 1)
- **Audio**: 1 W Lautsprecher, M5Speaker (DMA-basiert, asynchron)
- **USB**: Nativer ESP32-S3 USB CDC — kein UART-Bridge, Devicenode `/dev/ttyACM0`
- **USB-ID**: `303a:1001` (Espressif USB JTAG/serial debug unit)

## Warum eigene Firmware?

Die Werksfirmware basiert auf xiaozhi-esp32 und hat **keinen USB-Kommando-Eingang**:
USB-CDC ist dort reiner Log-Output. Steuerung läuft ausschließlich über
WebSocket/MCP zur xiaozhi.me-Cloud. Eigene Firmware ist die einzige Lösung für
lokale Textanzeige + Ton.

## Projektstruktur

```
stackchan_alert/
├── firmware/                   PlatformIO-Projekt (ESP32-Arduino)
│   ├── src/main.cpp            Haupt-Firmware
│   ├── data/facilityalarm.wav  Alarmsound (ins LittleFS geflasht)
│   ├── platformio.ini          Board: m5stack-cores3, Upload: /dev/ttyACM0
│   └── partitions.csv          Custom-Partitionstabelle
├── backup/
│   ├── SHA256SUMS              Prüfsummen der Flash-Backups
│   └── README.md               Backup/Restore-Anleitung
├── stackchan.py                Python-CLI (pyserial)
├── CLAUDE.md                   Diese Datei
└── README.md                   Nutzer-Dokumentation (Englisch)
```

## Firmware-Architektur

### Protokoll
JSON-Zeilenpakete über Serial (115200 baud):
```
Host → Device:  {"cmd":"alarm","text":"FEUER!","duration":10}\n
Device → Host:  {"ok":true}\n
```
Boot-Signal: `{"ready":true}`

### Befehle
| cmd | Parameter | Beschreibung |
|-----|-----------|--------------|
| `face` | `expr`: neutral/happy/sad/angry/doubt/sleepy | Avatar-Ausdruck |
| `print` | `text` | Text in die Statuszeile (y=200, h=40) |
| `clear` | — | Statuszeile leeren |
| `play` | `file` (ohne .wav) | WAV aus LittleFS abspielen (blockierend) |
| `alarm` | `text`, `duration` (int, Sekunden) | Alarm starten |
| `stopalarm` | — | Alarm sofort stoppen |

### Avatar (M5Stack-Avatar Library)

Der Avatar läuft in einem **eigenen FreeRTOS-Task** und zeichnet ~30fps via Sprite
auf das gesamte Display. **Wichtig: Nie direkt ins Display zeichnen während der
Avatar läuft — das führt zu sichtbarer Korruption.**

Korrekte Wege den Avatar-Render-Task zu nutzen:
- `avatar.setColorPalette(cp)` — Hintergrundfarbe ändern
- `avatar.setSpeechText("text")` — Text in der Sprechblase
- `avatar.setExpression(Expression::Happy)` — Gesichtsausdruck

Die Statusleiste (y=200..240) liegt technisch im Avatar-Sprite-Bereich.
`printStatus()` funktioniert für statische Texte, da die kurze Anzeigezeit
ausreicht. Für animierte Inhalte (Alarm) **nur Avatar-API verwenden**.

### Alarm-Implementierung

```
startAlarm(text, sec)
  → LittleFS.open("/facilityalarm.wav") → ps_malloc(3.1 MB) → Speaker.playWav()
  → avatar.setSpeechText() für Text
  → avatar.setColorPalette() für rot/schwarz-Blinken

updateAlarm() im loop():
  → alle 400 ms: Hintergrundfarbe umschalten
  → alle 300 ms: Sprechtext um 1 Zeichen vorschieben (Kreisscroll, 18 Zeichen sichtbar)
  → wenn Speaker.isPlaying() == false: Audio neu starten (Loop)
  → nach durMs: stopAlarm()
```

### Audio

- WAV-Datei (3,1 MB) komplett in PSRAM laden: `ps_malloc()`, Fallback auf `malloc()`
- `M5.Speaker.playWav(buf, sz)` ist asynchron (DMA)
- `M5.Speaker.isPlaying()` prüfen für Re-Loop
- `M5.Speaker.stop()` in `stopAlarm()` — danach Buffer freigeben

## Bekannte Stolperstellen

### LittleFS-Partition-Label (GELÖST)
`LittleFS.begin()` sucht standardmäßig nach Partition mit Name `"spiffs"`.
Unsere Partition heißt `"littlefs"` (laut partitions.csv).
→ **Fix**: `LittleFS.begin(true, "/littlefs", 10, "littlefs")`

Ohne diesen Parameter-Fix schlägt das Mounten still fehl und alle
`LittleFS.open()`-Aufrufe liefern `vfs_api.cpp: File system is not mounted`.

### Python: ESP-IDF-Logs mischen sich in Serial-Output
Das ESP-IDF schreibt Logs wie `[E][vfs_api.cpp:24] open(): ...` auf denselben
USB-CDC-Port wie unsere JSON-Antworten. `send()` in stackchan.py muss
Zeilen überspringen, die nicht mit `{` beginnen:

```python
while time.time() < deadline:
    line = s.readline().decode(errors="replace").strip()
    if line.startswith("{"):
        return json.loads(line)
    # ESP log → überspringen
```

### Avatar-FreeRTOS vs. direktes Display-Drawing (GELÖST)
Direktes `M5.Display.fillRect()` / `pushSprite()` während der Avatar-Task
läuft → sichtbare Korruption (beide Tasks schreiben gleichzeitig ins Display).
→ **Fix**: Ausschließlich Avatar-API verwenden (`setColorPalette`, `setSpeechText`).

## Entwicklungs-Workflow

```bash
# Firmware bauen und flashen
cd firmware
pio run --target upload

# Filesystem flashen (WAV-Datei)
pio run --target uploadfs

# Serieller Monitor
pio device monitor

# Flash-Backup erstellen
esptool --chip esp32s3 --port /dev/ttyACM0 --baud 460800 \
  read-flash 0x0 0x1000000 ../backup/flash_$(date +%F).bin
sha256sum ../backup/flash_$(date +%F).bin >> ../backup/SHA256SUMS
```

## Partitionstabelle

| Name | Typ | Offset | Größe |
|------|-----|--------|-------|
| nvs | data/nvs | 0x9000 | 20 KB |
| phy_init | data/phy | 0xE000 | 4 KB |
| app0 | app/ota_0 | 0x10000 | 2 MB |
| littlefs | data/spiffs | 0x210000 | ~6 MB |

## Abhängigkeiten (PlatformIO)

```ini
lib_deps =
    m5stack/M5Unified@^0.2
    meganetaaan/M5Stack-Avatar@^0.9
    bblanchon/ArduinoJson@^7
```

## Original-Firmware

- Basis: xiaozhi-esp32 mit M5Stack-StackChan HAL
- App-Version: 1.3.0, ESP-IDF v5.5.4, Compile: 2026-05-07
- Backup: siehe `backup/SHA256SUMS` (factory_2026-05-08.bin)

Restore:
```bash
esptool --chip esp32s3 --port /dev/ttyACM0 --baud 460800 \
  write-flash 0x0 backup/factory_2026-05-08.bin
```
