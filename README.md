# Embedded Software Design - System Overview

This repository contains two cooperating components:
- a Raspberry Pi 4 controller node (decision brain),
- and ESP32 motor node firmware (action executor).

## 1) Raspberry Pi 4 Node (Controller)

The Raspberry Pi 4 is the local intelligence layer that:
- runs local speech-to-text (STT),
- can optionally run local LLM and vision models,
- decides what physical action to trigger based on input,
- sends control signals to the corresponding ESP32 node.

### Responsibilities
- **Input processing**: audio (STT), optional camera/image, optional text commands.
- **Decision logic**: map interpreted intent to an action key.
- **Routing/configuration**: maintain a configuration of:
  - which ESP32 corresponds to which action/switch,
  - how to address each ESP32 (for example by ID/topic/address).
- **Dispatch**: send trigger signals to the selected ESP32 reliably.

## 2) ESP32 Motor Node (Executor)

The ESP32 motor firmware:
- supports configurable behavior and pin settings,
- receives trigger signals from the Raspberry Pi 4,
- executes motor movement for the requested action,
- returns the motor to neutral state after action,
- enters low-power mode while waiting for the next trigger.

### Responsibilities
- **Configuration**: motor pin, neutral angle, action angles/timing, power mode.
- **Action execution**: move servo/motor according to received command.
- **Safe reset**: return to neutral state after each action.
- **Energy saving**: reduce power draw when idle (for example detach servo/sleep).

## High-Level Flow

1. User input is captured on Raspberry Pi 4 (voice/image/text).
2. Local models infer intent and resolve an action.
3. Pi checks action-to-ESP32 mapping.
4. Pi sends signal to target ESP32.
5. ESP32 executes movement, returns to neutral, then idles in low-power mode.
