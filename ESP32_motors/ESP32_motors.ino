#include <ESP32Servo.h>
#include <WiFi.h>

// SG90 servo signal pin (change if needed)
static const int SERVO_PIN = 18;

// Wi-Fi credentials (hardcoded as requested)
static const char* WIFI_SSID = "dark";
static const char* WIFI_PASSWORD = "wwwwwwww";

// LED behavior options:
// - If your board has a simple onboard LED, set LED_MODE_GPIO to 1 and choose LED_PIN.
// - If your board uses an RGB pixel LED, set LED_MODE_GPIO to 0 to disable the GPIO blink fallback.
static const int LED_MODE_GPIO = 1;
static const int LED_PIN = 48;
static const int LED_ON_LEVEL = HIGH;
static const int LED_OFF_LEVEL = LOW;

// Servo pulse limits for SG90 (microseconds)
static const int SERVO_MIN_US = 500;
static const int SERVO_MAX_US = 2400;

// Motion sequence parameters
static const int ANGLE_CENTER = 90;
static const int ANGLE_LEFT = 20;
static const int ANGLE_RIGHT = 160;
static const int STEP_DELAY_MS = 12;
static const int HOLD_MS = 350;

Servo sg90;
int currentAngle = ANGLE_CENTER;
String serialBuffer = "";
int lastWifiStatus = WL_IDLE_STATUS;

void setGreenLed(bool on) {
  if (LED_MODE_GPIO == 1) {
    digitalWrite(LED_PIN, on ? LED_ON_LEVEL : LED_OFF_LEVEL);
  }
}

void blinkGreen(int times, int onMs, int offMs) {
  for (int i = 0; i < times; ++i) {
    setGreenLed(true);
    delay(onMs);
    setGreenLed(false);
    delay(offMs);
  }
}

void moveServoSmooth(int targetAngle, int stepDelayMs) {
  if (targetAngle < 0) {
    targetAngle = 0;
  } else if (targetAngle > 180) {
    targetAngle = 180;
  }

  if (targetAngle == currentAngle) {
    sg90.write(targetAngle);
    delay(stepDelayMs);
    return;
  }

  int step = (targetAngle > currentAngle) ? 1 : -1;
  for (int a = currentAngle; a != targetAngle; a += step) {
    sg90.write(a);
    delay(stepDelayMs);
  }

  sg90.write(targetAngle);
  currentAngle = targetAngle;
}

void runMovementLoopOnce() {
  // Start-of-loop marker
  blinkGreen(2, 120, 120);

  moveServoSmooth(ANGLE_CENTER, STEP_DELAY_MS);
  delay(HOLD_MS);

  moveServoSmooth(ANGLE_LEFT, STEP_DELAY_MS);
  delay(HOLD_MS);

  moveServoSmooth(ANGLE_RIGHT, STEP_DELAY_MS);
  delay(HOLD_MS);

  moveServoSmooth(ANGLE_CENTER, STEP_DELAY_MS);
  delay(HOLD_MS);

  // End-of-loop marker
  blinkGreen(1, 250, 120);
}

void runActionAndReturnNeutral(const String &action) {
  // Start marker for a command-triggered action.
  blinkGreen(2, 80, 80);

  if (action == "LEFT") {
    moveServoSmooth(ANGLE_LEFT, STEP_DELAY_MS);
    delay(HOLD_MS);
  } else if (action == "RIGHT") {
    moveServoSmooth(ANGLE_RIGHT, STEP_DELAY_MS);
    delay(HOLD_MS);
  } else if (action == "CENTER") {
    moveServoSmooth(ANGLE_CENTER, STEP_DELAY_MS);
    delay(HOLD_MS);
  } else {
    // Default command: one full sequence.
    runMovementLoopOnce();
  }

  // Always return to neutral state after action.
  moveServoSmooth(ANGLE_CENTER, STEP_DELAY_MS);
  delay(120);

  // End marker for completed action.
  blinkGreen(1, 180, 80);
}

void processCommandCenterSignal(const String &rawCommand) {
  String command = rawCommand;
  command.trim();
  command.toUpperCase();
  if (command.length() == 0) {
    return;
  }
  Serial.print("SIGNAL_RECEIVED: ");
  Serial.println(command);

  // Proxy command center signals to motor actions.
  // Supported commands (via serial):
  // TURN/SEQ, LEFT, RIGHT, CENTER, STATUS.
  if (command == "TURN" || command == "SEQ") {
    Serial.println("ACK: ACTION=SEQ");
    runActionAndReturnNeutral("SEQ");
  } else if (command == "LEFT") {
    Serial.println("ACK: ACTION=LEFT");
    runActionAndReturnNeutral("LEFT");
  } else if (command == "RIGHT") {
    Serial.println("ACK: ACTION=RIGHT");
    runActionAndReturnNeutral("RIGHT");
  } else if (command == "CENTER") {
    Serial.println("ACK: ACTION=CENTER");
    runActionAndReturnNeutral("CENTER");
  } else if (command == "STATUS") {
    Serial.print("STATUS: WIFI=");
    Serial.print(WiFi.status() == WL_CONNECTED ? "CONNECTED" : "DISCONNECTED");
    Serial.print(", IP=");
    Serial.print(WiFi.localIP());
    Serial.print(", ANGLE=");
    Serial.println(currentAngle);
  } else {
    Serial.print("ERR: UNKNOWN_COMMAND=");
    Serial.println(command);
  }
}

void logWifiStatusChange() {
  const int currentStatus = WiFi.status();
  if (currentStatus == lastWifiStatus) {
    return;
  }

  lastWifiStatus = currentStatus;
  if (currentStatus == WL_CONNECTED) {
    Serial.print("WIFI_STATUS: CONNECTED, IP=");
    Serial.println(WiFi.localIP());
  } else {
    Serial.print("WIFI_STATUS: DISCONNECTED, CODE=");
    Serial.println(currentStatus);
  }
}

void pollSerialCommands() {
  while (Serial.available() > 0) {
    const char c = static_cast<char>(Serial.read());
    if (c == '\n' || c == '\r') {
      if (serialBuffer.length() > 0) {
        processCommandCenterSignal(serialBuffer);
        serialBuffer = "";
      }
    } else {
      serialBuffer += c;
    }
  }
}

void setup() {
  Serial.begin(115200);
  delay(200);

  if (LED_MODE_GPIO == 1) {
    pinMode(LED_PIN, OUTPUT);
    setGreenLed(false);
  }

  WiFi.mode(WIFI_STA);
  Serial.print("WIFI: CONNECTING TO SSID=");
  Serial.println(WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  const unsigned long wifiStartMs = millis();
  int wifiAttempt = 0;
  while (WiFi.status() != WL_CONNECTED && (millis() - wifiStartMs) < 10000UL) {
    wifiAttempt++;
    Serial.print("WIFI_CONNECT_ATTEMPT=");
    Serial.print(wifiAttempt);
    Serial.print(", STATUS_CODE=");
    Serial.println(WiFi.status());
    delay(500);
  }
  lastWifiStatus = WiFi.status();
  if (lastWifiStatus == WL_CONNECTED) {
    Serial.print("WIFI_STATUS: CONNECTED, IP=");
    Serial.println(WiFi.localIP());
  } else {
    Serial.print("WIFI_STATUS: NOT_CONNECTED_AFTER_BOOT, CODE=");
    Serial.println(lastWifiStatus);
  }

  ESP32PWM::allocateTimer(0);
  ESP32PWM::allocateTimer(1);
  ESP32PWM::allocateTimer(2);
  ESP32PWM::allocateTimer(3);

  sg90.setPeriodHertz(50);
  sg90.attach(SERVO_PIN, SERVO_MIN_US, SERVO_MAX_US);
  sg90.write(ANGLE_CENTER);
  currentAngle = ANGLE_CENTER;
  delay(500);

  Serial.println("READY: ESP32 motor node listening");
  Serial.println("CMD: TURN | SEQ | LEFT | RIGHT | CENTER | STATUS");
  blinkGreen(1, 220, 120);
}

void loop() {
  logWifiStatusChange();
  pollSerialCommands();
  delay(10);
}
