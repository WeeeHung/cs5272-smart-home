#include <ESP32Servo.h>
#include <WiFi.h>
#include <WebServer.h>
#include <WiFiUdp.h>

// SG90 servo signal pin (change if needed)
static const int SERVO_PIN = 18;

// Wi-Fi credentials (edit here for your network).
static const char *WIFI_SSID = "dark";
static const char *WIFI_PASSWORD = "wwwwwwww";

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
WebServer httpServer(80);
WiFiUDP presenceUdp;
unsigned long lastPresenceMs = 0;

static const unsigned int PRESENCE_PORT = 4210;
static const unsigned long PRESENCE_INTERVAL_MS = 5000UL;
static const char* NODE_ID = "motor_b";

// Drive the status LED (green indicator) on boards with direct GPIO LED.
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

// Smoothly move to target angle one degree per step.
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

// Demonstration sequence used for TURN/SEQ command.
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

// Execute one command action and always return to neutral afterwards.
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

// Parse and dispatch command-center signals received over serial or HTTP.
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

// Validate that incoming command is supported by this node.
bool isKnownCommand(const String &commandUpper) {
  return commandUpper == "TURN" || commandUpper == "SEQ" || commandUpper == "LEFT" ||
         commandUpper == "RIGHT" || commandUpper == "CENTER" || commandUpper == "STATUS";
}

// Minimal parser for {"command":"..."} body without extra dependencies.
String extractCommandFromJsonBody(const String &body) {
  const String key = "\"command\"";
  int keyPos = body.indexOf(key);
  if (keyPos < 0) {
    return "";
  }

  int colonPos = body.indexOf(':', keyPos + key.length());
  if (colonPos < 0) {
    return "";
  }

  int firstQuote = body.indexOf('"', colonPos + 1);
  if (firstQuote < 0) {
    return "";
  }

  int secondQuote = body.indexOf('"', firstQuote + 1);
  if (secondQuote < 0) {
    return "";
  }

  return body.substring(firstQuote + 1, secondQuote);
}

// HTTP health endpoint for command-center discovery probes.
void handleHealth() {
  String json = "{\"ok\":true,\"service\":\"esp32-motor-node\",\"node\":\"";
  json += NODE_ID;
  json += "\",\"ip\":\"";
  json += WiFi.localIP().toString();
  json += "\"}";
  httpServer.send(200, "application/json", json);
}

// HTTP command endpoint for command-center trigger forwarding.
void handleCommand() {
  String command = "";
  if (httpServer.hasArg("plain")) {
    command = extractCommandFromJsonBody(httpServer.arg("plain"));
  }
  if (command.length() == 0 && httpServer.hasArg("command")) {
    command = httpServer.arg("command");
  }

  command.trim();
  command.toUpperCase();
  if (command.length() == 0) {
    httpServer.send(400, "application/json", "{\"ok\":false,\"error\":\"missing_command\"}");
    return;
  }
  if (!isKnownCommand(command)) {
    String err = "{\"ok\":false,\"error\":\"unknown_command\",\"command\":\"";
    err += command;
    err += "\"}";
    httpServer.send(400, "application/json", err);
    return;
  }

  processCommandCenterSignal(command);
  String ok = "{\"ok\":true,\"command\":\"";
  ok += command;
  ok += "\"}";
  httpServer.send(200, "application/json", ok);
}

// Broadcast periodic presence beacon for fast discovery on local network.
void broadcastPresenceIfDue() {
  if (WiFi.status() != WL_CONNECTED) {
    return;
  }
  const unsigned long now = millis();
  if (now - lastPresenceMs < PRESENCE_INTERVAL_MS) {
    return;
  }
  lastPresenceMs = now;

  String msg = "ESP32_PRESENCE node=";
  msg += NODE_ID;
  msg += " ip=";
  msg += WiFi.localIP().toString();
  msg += " port=80";

  presenceUdp.beginPacket("255.255.255.255", PRESENCE_PORT);
  presenceUdp.print(msg);
  presenceUdp.endPacket();

  Serial.print("PRESENCE_BROADCAST: ");
  Serial.println(msg);
}

// Log Wi-Fi status transitions so connectivity issues are visible.
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

// Read line-delimited serial commands and dispatch when newline arrives.
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
  // ESP32-S3 USB-CDC: after reset the host re-enumerates the port. A fixed delay is more
  // reliable than while (!Serial), which can misbehave on some Windows + core combinations.
  delay(2000);
  Serial.println();
  Serial.println("BOOT: ESP32_motors starting");
  Serial.flush();

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
    httpServer.on("/health", HTTP_GET, handleHealth);
    httpServer.on("/command", HTTP_POST, handleCommand);
    httpServer.on("/command", HTTP_GET, handleCommand);
    httpServer.begin();
    Serial.println("HTTP_SERVER: LISTENING_ON_PORT=80");
    presenceUdp.begin(PRESENCE_PORT);
    Serial.print("PRESENCE: UDP_BROADCAST_PORT=");
    Serial.println(PRESENCE_PORT);
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
  // Main service loop: network servicing, discovery beacon, and serial commands.
  logWifiStatusChange();
  if (WiFi.status() == WL_CONNECTED) {
    httpServer.handleClient();
    broadcastPresenceIfDue();
  }
  pollSerialCommands();
  delay(10);
}
