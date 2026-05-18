# StackChan Alert — Entwicklungskontext für Claude

## Projektüberblick

Dieses Repo enthält eine eigene Firmware für den **M5Stack CoreS3 im StackChan-Kickstarter-Kit**
sowie ein Python-CLI zur Steuerung über USB. Ziel: Text auf das Display ausgeben,
WAV-Sounds abspielen, einen Alarm mit blinkendem Hintergrund auslösen und den Kopf
mit Servo-Bewegungen steuern — alles über USB vom Linux-Host aus.

## Hardware

- **Modell**: M5Stack CoreS3 im StackChan-Kickstarter-Kit
- **SoC**: ESP32-S3 (dual-core LX7 240 MHz), Chip-Rev v0.2
- **Flash**: 16 MB, **PSRAM**: 8 MB (OPI PSRAM)
- **Display**: ILI9342, 2", 320×240, Landscape (Rotation 1)
- **Audio**: 1 W Lautsprecher, M5Speaker (DMA-basiert, asynchron)
- **USB**: Nativer ESP32-S3 USB CDC — kein UART-Bridge, Devicenode `/dev/ttyACM0`
- **USB-ID**: `303a:1001` (Espressif USB JTAG/serial debug unit)
- **Servos**: 2× Feetech SCSCL Smart-Serial-Servo (kein Standard-PWM!)

## Warum eigene Firmware?

Die Werksfirmware basiert auf xiaozhi-esp32 und hat **keinen USB-Kommando-Eingang**:
USB-CDC ist dort reiner Log-Output. Steuerung läuft ausschließlich über
WebSocket/MCP zur xiaozhi.me-Cloud. Eigene Firmware ist die einzige Lösung für
lokale Textanzeige + Ton + Servo.

## Projektstruktur

```
stackchan_alert/
├── firmware/                   PlatformIO-Projekt (ESP32-Arduino)
│   ├── src/main.cpp            Haupt-Firmware
│   ├── data/facilityalarm.wav  Alarmsound (ins LittleFS geflasht)
│   ├── platformio.ini          Board: m5stack-cores3, Upload: /dev/ttyACM0
│   └── partitions.csv          Custom-Partitionstabelle
├── patterns/                   Bewegungsmuster-Dateien (JSON)
│   ├── nervous.json            Nervöses Zittern (Alarm)
│   ├── nod.json                Nicken
│   ├── look_around.json        Umschauen
│   └── greet.json              Begrüßung mit happy-Gesicht
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
| `alarm` | `text`, `duration` (sec), `nervous` (bool) | Alarm starten |
| `stopalarm` | — | Alarm sofort stoppen |
| `move` | `pan` (°), `tilt` (°) | Kopf positionieren |
| `center` | — | Kopf zur Mittelposition |
| `scan` | — | Servo-Wiggle-Test (links+rechts+hoch) |

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

### Servo-Implementierung (Feetech SCSCL)

Die StackChan-Servos sind **keine Standard-PWM-Servos**. Sie verwenden das
proprietäre Feetech SCS Binär-Protokoll über UART.

**Anschluss:**
- UART1: TX=GPIO6, RX=GPIO7, 1 Mbps (half-duplex, selbe physikalische Leitung)
- Gefunden durch Analyse von `m5stack/StackChan` → `hal/hal_servo.cpp`

**Servo-Konfiguration:**

| Achse | Servo-ID | Center (SCS) | Modus |
|-------|----------|-------------|-------|
| Yaw (Pan, links/rechts) | 1 | 460 | PWM / Continuous Rotation |
| Pitch (Tilt, hoch/runter) | 2 | 620 | Position |

**SCS-Protokoll-Paket:**
```
FF FF [ID] [LEN] [INS] [PARAMS...] [CHKSUM]
LEN    = nparams + 2
CHKSUM = ~(ID + LEN + INS + sum(params)) & 0xFF
```

**WRITE-Befehl (INS=0x03) für Goal-Position:**
- Register 0x28 = Torque Enable (1 Byte)
- Register 0x2A = Goal Position L (2 Byte, Big-Endian für SCSCL)
- Zusätzlich Time (2 Byte) + Speed (2 Byte) = gesamt 6 Datenbytes pro Servo

**SYNC_WRITE (INS=0x83, ID=0xFE broadcast):**
Bewegt beide Servos in einem einzigen Paket — unverzichtbar, da individuelle
WRITE-Befehle an Servo 1 → Servo 2 eine Halbduplex-Bus-Kollision auslösen
(Servo 1 antwortet noch, wenn Servo 2 adressiert wird → Tilt ohne Funktion).

```cpp
uint8_t p[] = {
    REG_GOAL_POS, 6,                          // Adresse, Datenbytes/Servo
    ID_YAW,   hi(yaw),   lo(yaw),   0,0, 0,0,
    ID_PITCH, hi(pitch), lo(pitch), 0,0, 0,0
};
scsSend(0xFE, 0x83, p, sizeof(p));
```

**Winkel → SCS-Einheiten:**
```cpp
static int degToUnits(int deg) { return deg * 38 / 9; }
// Kalibrierung: 90° = 380 SCS-Einheiten = voller Aufwärts-Hub (Decke)
// Gefunden durch Testen: Phys. Maximum Pitch = Center(620) + 380 = 1000
```

**Physikalische Limits (empirisch ermittelt):**
- Pitch: Center 620, max up = 1000 (+380 = +90°), max down ≈ 240 (-380)
- Yaw: Center 460, Bereich 60–860 (PWM-Modus, reagiert auf Offset vom Center)

**Initialisierung:**
1. `HardwareSerial scsBus(1)` → `begin(1000000, SERIAL_8N1, RX=7, TX=6)`
2. Torque Enable senden (Register 0x28 = 1) für beide IDs
3. `centerServos()` → SYNC_WRITE mit Center-Positionen

**Warum ESP32Servo nicht funktioniert:**
Die SCSCL-Servos antworten nicht auf PWM-Signale. Sie erwarten nur das SCS-Binärprotokoll.
`ESP32Servo` generiert PWM und hat keinen Effekt → Bibliothek entfernt.

### Alarm-Implementierung

```
startAlarm(text, sec, nervous=true)
  → LittleFS.open("/facilityalarm.wav") → ps_malloc(3.1 MB) → Speaker.playWav()
  → avatar.setSpeechText() für scrollenden Text
  → avatar.setColorPalette() für rot/schwarz-Blinken (400 ms)
  → wenn nervous=true: Servo-Zittern alle 200 ms (±12° pan, ±6° tilt, zufällig)
  → wenn nervous=false: Python steuert Bewegung via --motion Pattern-Datei

updateAlarm() im loop():
  → alle 400 ms: Hintergrundfarbe umschalten
  → alle 300 ms: Sprechtext um 1 Zeichen vorschieben (Kreisscroll, 18 Zeichen)
  → alle 200 ms: Servo-Jitter (wenn nervous)
  → wenn Speaker.isPlaying() == false: Audio neu starten (Loop)
  → nach durMs: stopAlarm() → center servos
```

### Audio

- WAV-Datei (3,1 MB) komplett in PSRAM laden: `ps_malloc()`, Fallback auf `malloc()`
- `M5.Speaker.playWav(buf, sz)` ist asynchron (DMA)
- `M5.Speaker.isPlaying()` prüfen für Re-Loop
- `M5.Speaker.stop()` in `stopAlarm()` — danach Buffer freigeben

### Bewegungsmuster (patterns/)

Pattern-Dateien werden vom Python-CLI gelesen und als `move`-Befehle gesendet.
Format: JSON mit `steps`-Array, jeder Step kann `pan`, `tilt`, `face`, `text`, `duration` haben.

```json
{
  "name": "beispiel",
  "loop": false,
  "steps": [
    {"pan": 40, "tilt": 0,  "duration": 600},
    {"pan":  0, "tilt": 30, "duration": 500, "face": "happy"}
  ]
}
```

Aktuelle Patterns und ihre Winkel (neu kalibriert mit 38/9 Faktor):
- `nervous.json`: ±12° pan, ±6° tilt, 100-150 ms/Schritt — für Alarm
- `nod.json`: +35° tilt 2×, je 350 ms
- `look_around.json`: ±40° pan, ±12-30° tilt
- `greet.json`: 2× +30° tilt + happy-Gesicht

## Bekannte Stolperstellen

### Servo-Offline nach Reconnect (GELÖST)
Symptom: Servos funktionieren in der Entwicklung, dann gehen sie offline und reagieren nicht mehr.
Root Cause: Wenn der Host/PC neu gestartet wird ohne das Gerät zu resetten, bleibt die Firmware
in einem alten Zustand. VM_EN (Servo-Stromversorgung im PY32 IO-Expander) wird nicht neu
asserted, daher keine Servo-Kommunikation möglich.
→ **Fix**: `stackchan.py` führt jetzt `hardware_reset()` via RTS durch, wenn beim Connect
kein Boot-Banner empfangen wird. Dies triggert `setup()` neu, was `initServos()` und damit
die Servo-Initialisierung erneut ausführt. Siehe `StackChanConn` Klasse in `stackchan.py`.

### LittleFS-Partition-Label (GELÖST)
`LittleFS.begin()` sucht standardmäßig nach Partition mit Name `"spiffs"`.
Unsere Partition heißt `"littlefs"` (laut partitions.csv).
→ **Fix**: `LittleFS.begin(true, "/littlefs", 10, "littlefs")`

### Python: ESP-IDF-Logs mischen sich in Serial-Output
Das ESP-IDF schreibt Logs auf denselben USB-CDC-Port wie JSON-Antworten.
`send()` überspringt Zeilen die nicht mit `{` beginnen:
```python
while time.time() < deadline:
    line = s.readline().decode(errors="replace").strip()
    if line.startswith("{"):
        return json.loads(line)
```

### Avatar-FreeRTOS vs. direktes Display-Drawing (GELÖST)
Direktes `M5.Display.fillRect()` während der Avatar-Task läuft → sichtbare Korruption.
→ **Fix**: Ausschließlich Avatar-API verwenden (`setColorPalette`, `setSpeechText`).

### Halbduplex-Bus-Kollision bei Servo-WRITE (GELÖST)
Individuelle WRITE-Befehle an Servo 1 dann Servo 2 → Kollision auf dem shared Bus
→ Tilt (Pitch, ID=2) reagiert nicht, nur Pan (Yaw, ID=1) funktioniert.
→ **Fix**: SYNC_WRITE (INS=0x83, Broadcast-ID=0xFE) — ein Paket für beide Servos.

### Servo-Skalierung (GELÖST)
Initiale Formel `degrees × 16/5 = degrees × 3.2` stammte aus der offiziellen
Firmware (Winkel-Parameter dort in 0.1°-Einheiten). Empirisch ermittelt:
physikalisches Maximum = 380 SCS-Einheiten vom Center → 90° = 380 Einheiten.
→ **Fix**: `degrees × 38/9 ≈ degrees × 4.22` — 90° = voller Hub.

## Entwicklungs-Workflow

```bash
# Firmware bauen und flashen
cd firmware
pio run --target upload

# Filesystem flashen (WAV-Datei, nur wenn data/ geändert)
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
# Kein ESP32Servo — Feetech SCSCL verwendet SCS-Protokoll, kein PWM
# HardwareSerial ist Teil des Arduino-Frameworks (kein extra lib nötig)
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
