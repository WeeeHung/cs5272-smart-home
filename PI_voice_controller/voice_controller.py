import os
import wave
import json
import time
import subprocess
import urllib.request
import importlib.metadata
import pyaudio
import numpy as np
from scipy import signal
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
# If hardware rejects 16 kHz (common on Pi + USB), we capture at native rate and resample.
_FALLBACK_CAPTURE_RATES = (48000, 44100, 32000, 22050, 8000)
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
    # Sidecar text is reliable when whisper prints nothing (e.g. zero segments on silence).
    "-otxt",
]
# Ignore wake scores briefly after reopening the mic (ALSA pop / transient often false-triggers).
WAKE_COOLDOWN_S = 0.85
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

# Bundled in PyPI wheels but often missing after `pip install git+...`; same files as openWakeWord v0.5.1 release.
_OWW_RELEASE_ASSETS = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1"
_OWW_PREPROCESSOR_CACHE = os.path.join(_SCRIPT_DIR, "models", ".openwakeword_cache")


def ensure_openwakeword_preprocessor_models():
    """Download melspectrogram + embedding TFLite models if absent (fixes missing site-packages/resources)."""
    os.makedirs(_OWW_PREPROCESSOR_CACHE, exist_ok=True)
    paths = {}
    for name in ("melspectrogram.tflite", "embedding_model.tflite"):
        path = os.path.join(_OWW_PREPROCESSOR_CACHE, name)
        paths[name] = path
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            continue
        url = f"{_OWW_RELEASE_ASSETS}/{name}"
        print(f"Downloading openWakeWord preprocessor model {name} ...")
        req = urllib.request.Request(url, headers={"User-Agent": "cs5272-smart-home-voice/1"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read()
        except Exception as exc:
            raise RuntimeError(
                f"Could not download {url} (needed for openWakeWord audio features). "
                "Check network or place the file manually under "
                f"{_OWW_PREPROCESSOR_CACHE}"
            ) from exc
        with open(path, "wb") as f:
            f.write(data)
    return paths["melspectrogram.tflite"], paths["embedding_model.tflite"]


def create_openwakeword_model():
    """Load custom .tflite wake model; requires openWakeWord >= 0.6 (see PI_voice_controller/requirements.txt)."""
    mel_path, emb_path = ensure_openwakeword_preprocessor_models()
    try:
        return Model(
            wakeword_models=[WAKE_WORD_MODEL_PATH],
            inference_framework="tflite",
            melspec_model_path=mel_path,
            embedding_model_path=emb_path,
        )
    except TypeError as e:
        msg = str(e)
        if "wakeword_models" in msg and "unexpected keyword" in msg:
            try:
                ver = importlib.metadata.version("openwakeword")
            except importlib.metadata.PackageNotFoundError:
                ver = "unknown"
            raise RuntimeError(
                f"openWakeWord {ver} is too old for this script (AudioFeatures got stray kwargs).\n"
                "Usually: pip install -U 'openwakeword>=0.6.0'\n"
                "Python 3.13 on Linux: PyPI needs tflite-runtime (missing); GitHub install needs "
                "speexdsp-ns (often missing for cp313). Use --no-deps, then requirements file:\n"
                "  pip install 'openwakeword @ git+https://github.com/dscripka/openWakeWord.git' --no-deps\n"
                "  pip install -r PI_voice_controller/requirements-py313-linux.txt"
            ) from e
        raise


def _default_voice_config():
    return {
        "command_center_url": COMMAND_CENTER_URL,
        "locations": list(_DEFAULT_LOCATIONS),
        "actions": list(_DEFAULT_ACTIONS),
        "sync_locations_from_command_center": False,
        # None = auto-pick PortAudio input (USB name first); try 16 kHz then 48k/44.1k + resample.
        "input_device_index": None,
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


def _input_device_candidates(p):
    """(index, name) for devices with at least one input channel; USB-like names first."""
    found = []
    for i in range(p.get_device_count()):
        info = p.get_device_info_by_index(i)
        if int(info.get("maxInputChannels", 0)) < 1:
            continue
        name = str(info.get("name", ""))
        found.append((i, name))
    found.sort(key=lambda t: (0 if "usb" in t[1].lower() else 1, t[0]))
    return found


def _sample_rates_to_try(p, device_index):
    """Prefer 16 kHz, then device default, then common USB rates."""
    rates = [AUDIO_RATE]
    if device_index is not None:
        dr = int(float(p.get_device_info_by_index(device_index).get("defaultSampleRate", 0)))
        if dr > 0 and dr not in rates:
            rates.append(dr)
    for r in _FALLBACK_CAPTURE_RATES:
        if r not in rates:
            rates.append(r)
    return rates


def _probe_mic(p, device_index, sample_rate, frames_per_buffer):
    stream = open_mic_stream(p, device_index, sample_rate, frames_per_buffer)
    stream.close()


def open_mic_stream(p, device_index, sample_rate, frames_per_buffer):
    kw = dict(
        format=pyaudio.paInt16,
        channels=1,
        rate=int(sample_rate),
        input=True,
        frames_per_buffer=frames_per_buffer,
    )
    if device_index is not None:
        kw["input_device_index"] = device_index
    return p.open(**kw)


def resample_int16_to_rate(pcm_i16: np.ndarray, src_rate: int, dst_rate: int, num_out: int) -> np.ndarray:
    """Resample mono int16 PCM to exactly num_out samples at implied dst_rate/source duration."""
    if src_rate == dst_rate:
        if pcm_i16.size == num_out:
            return pcm_i16.astype(np.int16, copy=False)
        if pcm_i16.size > num_out:
            return pcm_i16[:num_out].astype(np.int16, copy=False)
        return np.pad(pcm_i16, (0, num_out - pcm_i16.size)).astype(np.int16)
    x = pcm_i16.astype(np.float64)
    y = signal.resample(x, num_out)
    return np.clip(np.round(y), -32768, 32767).astype(np.int16)


def resolve_input_device_and_rate(p, cfg):
    """
    Pick (device_index, capture_sample_rate). Many ALSA USB devices only allow 48k/44.1k;
    we then resample to AUDIO_RATE for openWakeWord and Whisper.
    """
    env = os.environ.get("PI_VOICE_INPUT_DEVICE", "").strip()
    if env.isdigit():
        idx = int(env)
        last_err = None
        for sr in _sample_rates_to_try(p, idx):
            try:
                _probe_mic(p, idx, sr, CHUNK_SIZE)
                print(f"Using input device {idx} at {sr} Hz (PI_VOICE_INPUT_DEVICE); resample to {AUDIO_RATE} Hz: {sr != AUDIO_RATE}")
                return idx, sr
            except OSError as e:
                last_err = e
        raise RuntimeError(f"PI_VOICE_INPUT_DEVICE={idx}: no sample rate worked (last: {last_err})") from last_err

    pref = cfg.get("input_device_index")
    if pref is not None:
        idx = int(pref)
        last_err = None
        for sr in _sample_rates_to_try(p, idx):
            try:
                _probe_mic(p, idx, sr, CHUNK_SIZE)
                print(f"Using input device {idx} at {sr} Hz (config.json); resample to {AUDIO_RATE} Hz: {sr != AUDIO_RATE}")
                return idx, sr
            except OSError as e:
                last_err = e
        raise RuntimeError(
            f"config input_device_index={idx}: no sample rate worked (last: {last_err}). "
            "See README: arecord -l and PortAudio device list."
        ) from last_err

    for sr in _sample_rates_to_try(p, None):
        try:
            _probe_mic(p, None, sr, CHUNK_SIZE)
            print(f"Using default input device at {sr} Hz; resample to {AUDIO_RATE} Hz: {sr != AUDIO_RATE}")
            return None, sr
        except OSError:
            continue

    last_err = None
    for i, name in _input_device_candidates(p):
        for sr in _sample_rates_to_try(p, i):
            try:
                _probe_mic(p, i, sr, CHUNK_SIZE)
                print(f"Using input device {i}: {name!r} at {sr} Hz; resample to {AUDIO_RATE} Hz: {sr != AUDIO_RATE}")
                return i, sr
            except OSError as e:
                last_err = e

    raise RuntimeError(
        f"No input device worked (last error: {last_err}). "
        "Check USB mic: lsusb, arecord -l. Set input_device_index in config.json or PI_VOICE_INPUT_DEVICE. "
        "List PortAudio devices: python3 -c \"import pyaudio as py; p=py.PyAudio(); "
        "[print(i, p.get_device_info_by_index(i)['name']) for i in range(p.get_device_count()) "
        "if p.get_device_info_by_index(i)['maxInputChannels']>0]; p.terminate()\""
    ) from last_err


def record_audio(pyaudio_instance, input_device_index, capture_rate):
    print("Listening for command...")
    n_in = int(round(RECORD_SECONDS * capture_rate))
    block = min(1024, max(1, n_in))
    stream = open_mic_stream(pyaudio_instance, input_device_index, capture_rate, block)

    frames = []
    total = 0
    while total < n_in:
        chunk = min(block, n_in - total)
        data = stream.read(chunk, exception_on_overflow=False)
        frames.append(data)
        total += chunk
        
    stream.stop_stream()
    stream.close()

    raw = np.frombuffer(b"".join(frames), dtype=np.int16)[:n_in]
    if capture_rate != AUDIO_RATE:
        n_out = int(round(RECORD_SECONDS * AUDIO_RATE))
        raw = resample_int16_to_rate(raw, capture_rate, AUDIO_RATE, n_out)

    with wave.open(WAV_OUTPUT_FILENAME, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(pyaudio_instance.get_sample_size(pyaudio.paInt16))
        wf.setframerate(AUDIO_RATE)
        wf.writeframes(raw.tobytes())
    print("Recording saved.")

def transcribe_audio():
    print("Transcribing with Whisper...")
    txt_sidecar = WAV_OUTPUT_FILENAME + ".txt"
    if os.path.isfile(txt_sidecar):
        try:
            os.remove(txt_sidecar)
        except OSError:
            pass
    result = subprocess.run(WHISPER_CMD, capture_output=True, text=True)
    transcript = (result.stdout or "").strip()
    if not transcript and os.path.isfile(txt_sidecar):
        try:
            with open(txt_sidecar, encoding="utf-8", errors="replace") as f:
                transcript = f.read().strip()
        except OSError:
            pass
    if not transcript:
        err = (result.stderr or "").strip()
        if err:
            tail = err[-800:] if len(err) > 800 else err
            print(f"Whisper stderr (last part): {tail}")
        if result.returncode != 0:
            print(f"Whisper exited with code {result.returncode}")
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
    mic_stream = None
    try:
        input_dev, capture_rate = resolve_input_device_and_rate(p, cfg)
        read_n = max(1, int(round(CHUNK_SIZE * capture_rate / AUDIO_RATE)))
        mic_stream = open_mic_stream(p, input_dev, capture_rate, read_n)

        print(f"Waiting for wake word 'hey homie'...")

        wake_ignore_until = 0.0
        while True:
            raw = np.frombuffer(mic_stream.read(read_n, exception_on_overflow=False), dtype=np.int16)
            if capture_rate != AUDIO_RATE:
                audio_data = resample_int16_to_rate(raw, capture_rate, AUDIO_RATE, CHUNK_SIZE)
            else:
                audio_data = raw

            if time.monotonic() < wake_ignore_until:
                continue

            prediction = oww_model.predict(audio_data)

            if prediction[WAKE_WORD_NAME] > 0.5:
                print("\nWake word detected!")

                # Release ALSA/PortAudio device before opening a second stream in record_audio;
                # stop_stream() alone keeps the device busy → OSError -9985 Device unavailable.
                mic_stream.stop_stream()
                mic_stream.close()
                mic_stream = None

                try:
                    record_audio(p, input_dev, capture_rate)
                    transcript = transcribe_audio()

                    if transcript:
                        intent = extract_intent(transcript, cfg["locations"], cfg["actions"])
                        if intent and "location" in intent and "action" in intent:
                            trigger_actuator(intent, cfg["command_center_url"])
                finally:
                    print(f"\nWaiting for wake word 'hey homie'...")
                    mic_stream = open_mic_stream(p, input_dev, capture_rate, read_n)
                    wake_ignore_until = time.monotonic() + WAKE_COOLDOWN_S

    except KeyboardInterrupt:
        print("Stopping...")
    finally:
        if mic_stream is not None:
            mic_stream.close()
        p.terminate()

if __name__ == "__main__":
    main()