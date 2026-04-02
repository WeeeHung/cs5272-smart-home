import os
import wave
import json
import subprocess
import urllib.request
import importlib.metadata
import pyaudio
import numpy as np
from openwakeword.model import Model

# Repo layout: cs5272-smart-home/{whisper.cpp,llama.cpp,models/,PI_voice_controller/}
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPT_DIR)

# Configuration (wake model lives next to this script under models/)
WAKE_WORD_MODEL_PATH = os.path.join(_SCRIPT_DIR, "models", "hey_homie.tflite")
WAKE_WORD_NAME = "hey_homie"

AUDIO_RATE = 16000
CHUNK_SIZE = 1280
RECORD_SECONDS = 4
WAV_OUTPUT_FILENAME = os.path.join(_SCRIPT_DIR, "command.wav")

_WHISPER_CLI = os.path.join(_REPO_ROOT, "whisper.cpp", "build", "bin", "whisper-cli")
_WHISPER_GGML = os.path.join(_REPO_ROOT, "whisper.cpp", "models", "ggml-base.en.bin")
_LLAMA_CLI = os.path.join(_REPO_ROOT, "llama.cpp", "build", "bin", "llama-cli")
_LLAMA_GGUF = os.path.join(_REPO_ROOT, "models", "tinyllama-1.1b-chat.Q4_K_M.gguf")

_VOICE_CONFIG_PATH = os.path.join(_SCRIPT_DIR, "config.json")
_DEFAULT_LOCATIONS = ["living_room", "bedroom"]
_DEFAULT_ACTIONS = ["turn_demo", "left_once", "right_once"]

WHISPER_CMD = [
    _WHISPER_CLI,
    "-m",
    _WHISPER_GGML,
    "-f",
    WAV_OUTPUT_FILENAME,
    "-nt",
]
LLAMA_CMD = [
    _LLAMA_CLI,
    "-m",
    _LLAMA_GGUF,
    "-n",
    "30",
    "--temp",
    "0.1",
    "-p",
]

COMMAND_CENTER_URL = "http://127.0.0.1:8080/trigger-location"


def create_openwakeword_model():
    """Load custom .tflite wake model; requires openWakeWord >= 0.6 (see PI_voice_controller/requirements.txt)."""
    try:
        return Model(wakeword_models=[WAKE_WORD_MODEL_PATH], inference_framework="tflite")
    except TypeError as e:
        msg = str(e)
        if "wakeword_models" in msg and "unexpected keyword" in msg:
            try:
                ver = importlib.metadata.version("openwakeword")
            except importlib.metadata.PackageNotFoundError:
                ver = "unknown"
            raise RuntimeError(
                f"openWakeWord {ver} is too old for this script (AudioFeatures got stray kwargs). "
                "Upgrade: pip install -U 'openwakeword>=0.6.0'\n"
                "On Python 3.13, if TFLite fails to import, also try: pip install ai-edge-litert"
            ) from e
        raise


def _default_voice_config():
    return {
        "command_center_url": COMMAND_CENTER_URL,
        "locations": list(_DEFAULT_LOCATIONS),
        "actions": list(_DEFAULT_ACTIONS),
        "sync_locations_from_command_center": False,
    }


def _fetch_location_keys_from_command_center(trigger_url: str, timeout_s: float = 2.0):
    """Return sorted location keys from command center GET /nodes, or []."""
    base = trigger_url.rstrip("/").rsplit("/", 1)[0]
    req = urllib.request.Request(f"{base}/nodes", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as response:
            data = json.loads(response.read().decode())
    except Exception as exc:
        print(f"Could not sync locations from command center: {exc}")
        return []
    loc_map = data.get("location_map") or {}
    if isinstance(loc_map, dict) and loc_map:
        return sorted(loc_map.keys())
    return []


def load_voice_config():
    """Merge config.json (next to this script) over defaults; optional location sync."""
    cfg = _default_voice_config()
    if os.path.isfile(_VOICE_CONFIG_PATH):
        with open(_VOICE_CONFIG_PATH, encoding="utf-8") as f:
            user = json.load(f)
        if isinstance(user, dict):
            for key, value in user.items():
                if value is not None:
                    cfg[key] = value
    if cfg.get("sync_locations_from_command_center"):
        fetched = _fetch_location_keys_from_command_center(str(cfg["command_center_url"]))
        if fetched:
            cfg["locations"] = fetched
    locs = cfg.get("locations")
    acts = cfg.get("actions")
    if not isinstance(locs, list) or not locs:
        cfg["locations"] = list(_DEFAULT_LOCATIONS)
    if not isinstance(acts, list) or not acts:
        cfg["actions"] = list(_DEFAULT_ACTIONS)
    return cfg


def record_audio(pyaudio_instance):
    print("Listening for command...")
    stream = pyaudio_instance.open(format=pyaudio.paInt16, channels=1, rate=AUDIO_RATE, input=True, frames_per_buffer=1024)
    
    frames = []
    for _ in range(0, int(AUDIO_RATE / 1024 * RECORD_SECONDS)):
        data = stream.read(1024, exception_on_overflow=False)
        frames.append(data)
        
    stream.stop_stream()
    stream.close()

    with wave.open(WAV_OUTPUT_FILENAME, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(pyaudio_instance.get_sample_size(pyaudio.paInt16))
        wf.setframerate(AUDIO_RATE)
        wf.writeframes(b''.join(frames))
    print("Recording saved.")

def transcribe_audio():
    print("Transcribing with Whisper...")
    result = subprocess.run(WHISPER_CMD, capture_output=True, text=True)
    transcript = result.stdout.strip()
    print(f"Transcript: {transcript}")
    return transcript

def extract_intent(transcript, locations, actions):
    print("Extracting intent with TinyLlama...")
    loc_str = ", ".join(str(x) for x in locations)
    act_str = ", ".join(str(x) for x in actions)
    prompt = f"""You are a smart home parser. Read the user command and output ONLY valid JSON with 'location' and 'action' keys. 
Available locations: {loc_str}. 
Available actions: {act_str}.
Command: "{transcript}"
JSON Output:"""
    
    cmd = LLAMA_CMD + [prompt]
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    output = result.stdout.replace(prompt, "").strip()
    print(f"LLM Output: {output}")
    
    try:
        start = output.find('{')
        end = output.rfind('}') + 1
        json_str = output[start:end]
        return json.loads(json_str)
    except Exception as e:
        print("Failed to parse LLM output into JSON.")
        return None

def trigger_actuator(intent, command_center_url):
    print(f"Triggering command center: {intent}")
    data = json.dumps(intent).encode('utf-8')
    req = urllib.request.Request(command_center_url, data=data, headers={'Content-Type': 'application/json'}, method='POST')
    
    try:
        with urllib.request.urlopen(req) as response:
            print(f"Success! Status: {response.getcode()}")
    except Exception as e:
        print(f"Failed to reach Command Center: {e}")

def main():
    cfg = load_voice_config()
    print(f"Voice config locations: {cfg['locations']}")

    # Initialize OpenWakeWord
    print("Loading wake word model...")
    oww_model = create_openwakeword_model()
    
    p = pyaudio.PyAudio()
    mic_stream = p.open(format=pyaudio.paInt16, channels=1, rate=AUDIO_RATE, input=True, frames_per_buffer=CHUNK_SIZE)
    
    print(f"Waiting for wake word 'hey homie'...")
    
    try:
        while True:
            # Get audio from mic
            audio_data = np.frombuffer(mic_stream.read(CHUNK_SIZE, exception_on_overflow=False), dtype=np.int16)
            
            # Feed to openWakeWord
            prediction = oww_model.predict(audio_data)
            
            # Check if the confidence score is high enough (0.5 is usually a good baseline)
            if prediction[WAKE_WORD_NAME] > 0.5:
                print("\nWake word detected!")
                
                # We need to pause the continuous mic stream so we can record the command
                mic_stream.stop_stream()
                
                record_audio(p)
                transcript = transcribe_audio()
                
                if transcript:
                    intent = extract_intent(transcript, cfg["locations"], cfg["actions"])
                    if intent and "location" in intent and "action" in intent:
                        trigger_actuator(intent, cfg["command_center_url"])
                        
                print(f"\nWaiting for wake word 'hey homie'...")
                # Restart the listening stream
                mic_stream.start_stream()
                
    except KeyboardInterrupt:
        print("Stopping...")
    finally:
        mic_stream.close()
        p.terminate()

if __name__ == "__main__":
    main()