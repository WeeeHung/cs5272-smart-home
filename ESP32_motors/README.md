# ESP32-S3 + SG90 Servo POC (No-Space Build Path)

Use this folder if your toolchain fails on sketch paths with spaces.

## Sketch
- Main file: `ESP32_motors.ino`
- Behavior: immediate looping servo sequence with green LED start/end markers.

## Compile
From this folder:

```bash
arduino-cli compile --fqbn esp32:esp32:esp32s3:USBMode=hwcdc,CDCOnBoot=cdc,UploadMode=default .
```

## Upload
```bash
arduino-cli upload -p /dev/cu.usbmodemXXXX --fqbn esp32:esp32:esp32s3:USBMode=hwcdc,CDCOnBoot=cdc,UploadMode=default .
```

## Erase
```bash
python3 -m esptool --chip esp32s3 --port /dev/cu.usbmodemXXXX --baud 460800 erase_flash
```

## Monitor
```bash
arduino-cli monitor -p /dev/cu.usbmodemXXXX -c baudrate=115200
```

## Wiring
- SG90 signal (orange/yellow) -> `GPIO18`
- SG90 VCC (red) -> external `5V`
- SG90 GND (brown/black) -> external `GND`
- ESP32 GND -> same external `GND` (common ground)
