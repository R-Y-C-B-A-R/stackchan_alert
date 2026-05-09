#include <M5Unified.h>
#include <Avatar.h>
#include <ArduinoJson.h>
#include <LittleFS.h>
#include <HardwareSerial.h>

using namespace m5avatar;

Avatar avatar;

static const int STATUS_Y    = 200;
static const int STATUS_H    = 40;
static const int DISPLAY_W   = 320;
static const int SPEECH_CHARS = 18;

// ─── Feetech SCSCL serial servo ──────────────────────────────────────────
// Half-duplex UART1 at 1 Mbps on GPIO6 (TX) / GPIO7 (RX)
// Servo IDs and center positions from official M5Stack StackChan firmware.
static HardwareSerial scsBus(1);
static const int     SCS_TX    = 6;
static const int     SCS_RX    = 7;
static const int     SCS_BAUD  = 1000000;

static const uint8_t ID_YAW       = 1;
static const uint8_t ID_PITCH     = 2;
static const int     CTR_YAW      = 460;   // neutral position in SCS units
static const int     CTR_PITCH    = 620;
static const int     RANGE_YAW    = 400;   // center 460 → 60..860 (within 0-1000)
static const int     RANGE_PITCH  = 380;   // center 620 → 240..1000 (full upward range)

// SCS register addresses (big-endian byte order for SCSCL series)
static const uint8_t REG_TORQUE   = 0x28;  // Torque Enable
static const uint8_t REG_GOAL_POS = 0x2A;  // Goal Position L (H is at 0x2B)

// Send an SCS protocol packet:
//   FF FF [id] [nparams+2] [ins] [params...] [chksum]
static void scsSend(uint8_t id, uint8_t ins, const uint8_t* p, int n) {
    uint8_t len = (uint8_t)(n + 2);
    uint8_t chk = id + len + ins;
    for (int i = 0; i < n; i++) chk += p[i];
    chk = ~chk & 0xFF;

    scsBus.write(0xFF); scsBus.write(0xFF);
    scsBus.write(id);   scsBus.write(len);
    scsBus.write(ins);
    for (int i = 0; i < n; i++) scsBus.write(p[i]);
    scsBus.write(chk);
    scsBus.flush(); // wait until packet is fully transmitted
}

// Write 16-bit goal position + time=0 + speed=0 to a servo.
// SCSCL uses big-endian (high byte first).
static void scsWritePos(uint8_t id, int pos) {
    pos = constrain(pos, 0, 1000);
    uint8_t p[] = {
        REG_GOAL_POS,
        (uint8_t)(pos >> 8),    // position high byte
        (uint8_t)(pos & 0xFF),  // position low byte
        0x00, 0x00,             // time = 0  → full speed
        0x00, 0x00              // speed = 0 → full speed
    };
    scsSend(id, 0x03, p, 7);
}

static void scsTorque(uint8_t id, bool on) {
    uint8_t p[] = {REG_TORQUE, on ? (uint8_t)1 : (uint8_t)0};
    scsSend(id, 0x03, p, 2);
}

// degrees from center → SCS units  (1° = 3.2 units = 16/5)
static int degToUnits(int deg) { return deg * 38 / 9; }

static void moveServos(int pan, int tilt) {
    int yaw   = constrain(CTR_YAW   + degToUnits(pan),
                          CTR_YAW  - RANGE_YAW,  CTR_YAW  + RANGE_YAW);
    int pitch = constrain(CTR_PITCH + degToUnits(tilt),
                          CTR_PITCH - RANGE_PITCH, CTR_PITCH + RANGE_PITCH);
    // SYNC_WRITE (0x83) to broadcast ID (0xFE): moves both servos in one packet.
    // No individual responses → no half-duplex bus collision between commands.
    uint8_t p[] = {
        REG_GOAL_POS, 6,                                          // addr, data bytes per servo
        ID_YAW,   (uint8_t)(yaw   >> 8), (uint8_t)(yaw   & 0xFF), 0,0, 0,0,
        ID_PITCH, (uint8_t)(pitch >> 8), (uint8_t)(pitch & 0xFF), 0,0, 0,0
    };
    scsSend(0xFE, 0x83, p, sizeof(p));
}

static void centerServos() { moveServos(0, 0); }

static void initServos() {
    scsBus.begin(SCS_BAUD, SERIAL_8N1, SCS_RX, SCS_TX);
    delay(100);
    scsTorque(ID_YAW,   true);
    scsTorque(ID_PITCH, true);
    delay(50);
    centerServos();
}

// ─── Alarm state ──────────────────────────────────────────────────────────
static struct {
    bool          active     = false;
    bool          nervous    = true;
    unsigned long startMs    = 0;
    unsigned long durMs      = 0;
    String        text;
    uint8_t*      audioBuf   = nullptr;
    size_t        audioSz    = 0;
    int           scrollIdx  = 0;
    bool          redBg      = false;
    unsigned long lastBlink  = 0;
    unsigned long lastScroll = 0;
    unsigned long lastMove   = 0;
} alm;

// ─── Display helpers ──────────────────────────────────────────────────────

void clearStatus() {
    M5.Display.fillRect(0, STATUS_Y, DISPLAY_W, STATUS_H, TFT_BLACK);
}

void printStatus(const char* text) {
    clearStatus();
    M5.Display.setTextSize(2);
    M5.Display.setTextColor(TFT_WHITE, TFT_BLACK);
    M5.Display.setCursor(4, STATUS_Y + 8);
    M5.Display.print(text);
}

// ─── Audio helper ─────────────────────────────────────────────────────────

void playFile(const char* name) {
    String path = String("/") + name + ".wav";
    File f = LittleFS.open(path, "r");
    if (!f) {
        Serial.printf("{\"ok\":false,\"err\":\"file not found: %s\"}\n", path.c_str());
        return;
    }
    size_t sz = f.size();
    uint8_t* buf = (uint8_t*)malloc(sz);
    if (!buf) {
        f.close();
        Serial.println("{\"ok\":false,\"err\":\"out of memory\"}");
        return;
    }
    f.read(buf, sz);
    f.close();
    M5.Speaker.playWav(buf, sz);
    while (M5.Speaker.isPlaying()) { M5.update(); delay(10); }
    free(buf);
}

// ─── Face helper ──────────────────────────────────────────────────────────

void setFace(const char* expr) {
    if      (strcmp(expr, "happy")  == 0) avatar.setExpression(Expression::Happy);
    else if (strcmp(expr, "sad")    == 0) avatar.setExpression(Expression::Sad);
    else if (strcmp(expr, "angry")  == 0) avatar.setExpression(Expression::Angry);
    else if (strcmp(expr, "doubt")  == 0) avatar.setExpression(Expression::Doubt);
    else if (strcmp(expr, "sleepy") == 0) avatar.setExpression(Expression::Sleepy);
    else                                   avatar.setExpression(Expression::Neutral);
}

// ─── Alarm implementation ─────────────────────────────────────────────────

static void setAvatarBg(bool red) {
    ColorPalette cp;
    cp.set(COLOR_PRIMARY,    TFT_WHITE);
    cp.set(COLOR_BACKGROUND, red ? TFT_RED : TFT_BLACK);
    avatar.setColorPalette(cp);
    alm.redBg = red;
}

static void updateSpeechText() {
    int len = (int)alm.text.length();
    if (len <= SPEECH_CHARS) {
        avatar.setSpeechText(alm.text.c_str());
        return;
    }
    char buf[SPEECH_CHARS + 1];
    for (int i = 0; i < SPEECH_CHARS; i++) {
        buf[i] = alm.text[(alm.scrollIdx + i) % len];
    }
    buf[SPEECH_CHARS] = '\0';
    avatar.setSpeechText(buf);
    alm.scrollIdx = (alm.scrollIdx + 1) % len;
}

static void updateAlarmMovement() {
    if (!alm.nervous) return;
    unsigned long now = millis();
    if (now - alm.lastMove < 200) return;
    moveServos(random(-15, 16), random(-8, 9));
    alm.lastMove = now;
}

void stopAlarm() {
    if (!alm.active) return;
    alm.active = false;
    M5.Speaker.stop();
    if (alm.audioBuf) { free(alm.audioBuf); alm.audioBuf = nullptr; }
    avatar.setSpeechText("");
    setAvatarBg(false);
    centerServos();
}

void startAlarm(const char* text, int durationSec, bool nervous) {
    stopAlarm();
    alm.active    = true;
    alm.nervous   = nervous;
    alm.startMs   = millis();
    alm.durMs     = (unsigned long)durationSec * 1000UL;
    alm.text      = text;
    alm.scrollIdx = 0;
    unsigned long now = millis();
    alm.lastBlink  = now;
    alm.lastScroll = now;
    alm.lastMove   = now;

    File f = LittleFS.open("/facilityalarm.wav", "r");
    if (f) {
        alm.audioSz  = f.size();
        alm.audioBuf = (uint8_t*)ps_malloc(alm.audioSz);
        if (!alm.audioBuf) alm.audioBuf = (uint8_t*)malloc(alm.audioSz);
        if (alm.audioBuf) {
            f.read(alm.audioBuf, alm.audioSz);
            M5.Speaker.playWav(alm.audioBuf, alm.audioSz);
        }
        f.close();
    }
    updateSpeechText();
    setAvatarBg(true);
}

void updateAlarm() {
    if (!alm.active) return;
    unsigned long now = millis();
    if (now - alm.startMs >= alm.durMs) { stopAlarm(); return; }
    if (alm.audioBuf && !M5.Speaker.isPlaying()) {
        M5.Speaker.playWav(alm.audioBuf, alm.audioSz);
    }
    if (now - alm.lastBlink >= 400) {
        setAvatarBg(!alm.redBg);
        alm.lastBlink = now;
    }
    if (now - alm.lastScroll >= 300) {
        updateSpeechText();
        alm.lastScroll = now;
    }
    updateAlarmMovement();
}

// ─── Command handler ──────────────────────────────────────────────────────

void handleCommand(JsonDocument& doc) {
    const char* cmd = doc["cmd"] | "";

    if (strcmp(cmd, "print") == 0) {
        printStatus(doc["text"] | "");
        Serial.println("{\"ok\":true}");

    } else if (strcmp(cmd, "clear") == 0) {
        clearStatus();
        Serial.println("{\"ok\":true}");

    } else if (strcmp(cmd, "play") == 0) {
        const char* file = doc["file"] | "";
        if (file[0] == '\0') { Serial.println("{\"ok\":false,\"err\":\"missing file\"}"); return; }
        playFile(file);
        Serial.println("{\"ok\":true}");

    } else if (strcmp(cmd, "face") == 0) {
        setFace(doc["expr"] | "neutral");
        Serial.println("{\"ok\":true}");

    } else if (strcmp(cmd, "alarm") == 0) {
        bool nervous = doc["nervous"] | true;
        startAlarm(doc["text"] | "ALARM", doc["duration"] | 5, nervous);
        Serial.println("{\"ok\":true}");

    } else if (strcmp(cmd, "stopalarm") == 0) {
        stopAlarm();
        Serial.println("{\"ok\":true}");

    } else if (strcmp(cmd, "move") == 0) {
        moveServos(doc["pan"] | 0, doc["tilt"] | 0);
        Serial.println("{\"ok\":true}");

    } else if (strcmp(cmd, "center") == 0) {
        centerServos();
        Serial.println("{\"ok\":true}");

    } else if (strcmp(cmd, "scan") == 0) {
        // Wiggle both servos to confirm SCS communication works
        scsWritePos(ID_YAW,   CTR_YAW   + 150); // yaw  right ~47°
        scsWritePos(ID_PITCH, CTR_PITCH +  75); // tilt up    ~23°
        delay(700);
        scsWritePos(ID_YAW,   CTR_YAW   - 150); // yaw  left
        delay(700);
        centerServos();
        Serial.println("{\"ok\":true,\"info\":\"SCS wiggle done\"}");

    } else {
        Serial.printf("{\"ok\":false,\"err\":\"unknown cmd: %s\"}\n", cmd);
    }
}

// ─── Arduino entry points ─────────────────────────────────────────────────

void setup() {
    auto cfg = M5.config();
    M5.begin(cfg);

    M5.Display.setRotation(1);
    M5.Display.fillScreen(TFT_BLACK);
    M5.Speaker.setVolume(200);

    if (!LittleFS.begin(true, "/littlefs", 10, "littlefs")) {
        M5.Display.setTextColor(TFT_RED);
        M5.Display.println("LittleFS error");
    }

    initServos();
    avatar.init();
    M5.Display.drawFastHLine(0, STATUS_Y, DISPLAY_W, TFT_DARKGREY);

    Serial.begin(115200);
    Serial.println("{\"ready\":true}");
}

String inputBuf;

void loop() {
    M5.update();
    updateAlarm();

    while (Serial.available()) {
        char c = Serial.read();
        if (c == '\n') {
            inputBuf.trim();
            if (inputBuf.length() > 0) {
                JsonDocument doc;
                DeserializationError err = deserializeJson(doc, inputBuf);
                if (!err) {
                    handleCommand(doc);
                } else {
                    Serial.printf("{\"ok\":false,\"err\":\"%s\"}\n", err.c_str());
                }
                inputBuf = "";
            }
        } else if (c != '\r') {
            inputBuf += c;
        }
    }
}
