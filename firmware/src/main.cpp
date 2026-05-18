#include <M5Unified.h>
#include <Avatar.h>
#include <ArduinoJson.h>
#include <LittleFS.h>
#include "SCSCL.h"

using namespace m5avatar;

Avatar avatar;
static SCSCL scs;  // Official StackChan servo library

static const int STATUS_Y    = 200;
static const int STATUS_H    = 40;
static const int DISPLAY_W   = 320;
static const int SPEECH_CHARS = 18;

static const uint8_t ID_YAW       = 1;
static const uint8_t ID_PITCH     = 2;
static const int     CTR_YAW      = 460;   // neutral position in SCS units
static const int     CTR_PITCH    = 620;
static const int     RANGE_YAW    = 400;   // center 460 → 60..860
static const int     RANGE_PITCH  = 380;   // center 620 → 240..1000

// degrees from center → SCS units
static int degToUnits(int deg) { return deg * 38 / 9; }

static void moveServos(int pan, int tilt, int time_ms = 20) {
    int yaw   = constrain(CTR_YAW   + degToUnits(pan),   CTR_YAW   - RANGE_YAW,   CTR_YAW   + RANGE_YAW);
    int pitch = constrain(CTR_PITCH + degToUnits(tilt),  CTR_PITCH - RANGE_PITCH, CTR_PITCH + RANGE_PITCH);
    scs.WritePos(ID_YAW,   yaw,   time_ms, 0);
    scs.WritePos(ID_PITCH, pitch, time_ms, 0);
}

static void centerServos() { moveServos(0, 0); }

static void initServos() {
    scs.begin(UART_NUM_1, 1000000, 6, 7);  // Same params as original StackChan
    delay(500);  // Longer wait for servo boot
    scs.EnableTorque(ID_YAW,   0);
    scs.EnableTorque(ID_PITCH, 0);
    delay(200);
    scs.EnableTorque(ID_YAW,   1);
    scs.EnableTorque(ID_PITCH, 1);
    delay(200);
    centerServos();
    delay(100);
}

static bool isServoHealthy() {
    return scs.ReadPos(ID_YAW) >= 0 && scs.ReadPos(ID_PITCH) >= 0;
}

static void diagServos() {
    bool init_ok = scs.begin(UART_NUM_1, 1000000, 6, 7);
    delay(100);
    int yaw_pos   = scs.ReadPos(ID_YAW);
    int pitch_pos = scs.ReadPos(ID_PITCH);
    Serial.printf("{\"ok\":true,\"diag\":{\"init\":%d,\"yaw\":%d,\"pitch\":%d}}\n",
                  init_ok ? 1 : 0, yaw_pos, pitch_pos);
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

    File f = LittleFS.open("/pingping.wav", "r");
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
        if (!isServoHealthy()) {
            initServos();
        }
        int time_ms = doc["time"] | 20;
        moveServos(doc["pan"] | 0, doc["tilt"] | 0, time_ms);
        Serial.println("{\"ok\":true}");

    } else if (strcmp(cmd, "center") == 0) {
        if (!isServoHealthy()) {
            initServos();
        }
        centerServos();
        Serial.println("{\"ok\":true}");

    } else if (strcmp(cmd, "ping") == 0) {
        if (!isServoHealthy()) {
            initServos(); // Re-init if UART is dead (USB reconnect scenario)
        }
        int yaw_pos   = scs.ReadPos(ID_YAW);
        int pitch_pos = scs.ReadPos(ID_PITCH);
        Serial.printf("{\"ok\":true,\"yaw\":%d,\"pitch\":%d}\n", yaw_pos, pitch_pos);

    } else if (strcmp(cmd, "scan") == 0) {
        if (!isServoHealthy()) {
            initServos(); // Re-init if UART is dead
        }
        // Wiggle both servos to confirm SCS communication works
        scs.WritePos(ID_YAW,   CTR_YAW   + 150, 20, 0); // yaw  right ~47°
        scs.WritePos(ID_PITCH, CTR_PITCH +  75, 20, 0); // tilt up    ~23°
        delay(700);
        scs.WritePos(ID_YAW,   CTR_YAW   - 150, 20, 0); // yaw  left
        delay(700);
        centerServos();
        Serial.println("{\"ok\":true,\"info\":\"SCS wiggle done\"}");

    } else if (strcmp(cmd, "diag") == 0) {
        diagServos();

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

    avatar.init();
    M5.Display.drawFastHLine(0, STATUS_Y, DISPLAY_W, TFT_DARKGREY);

    Serial.begin(115200);

    // Init servos AFTER M5.begin() and Serial.begin() so we can see errors
    // uart_driver_delete() in scs.begin() will override any M5Unified UART
    initServos();

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
