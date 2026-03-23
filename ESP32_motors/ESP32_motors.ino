#include <ESP32Servo.h>

// SG90 servo signal pin (change if needed)
static const int SERVO_PIN = 18;

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

void setup() {
  if (LED_MODE_GPIO == 1) {
    pinMode(LED_PIN, OUTPUT);
    setGreenLed(false);
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
}

void loop() {
  runMovementLoopOnce();
}
