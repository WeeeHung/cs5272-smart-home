import os
import re
import sys
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
_WHISPER_MODEL_DIR = os.path.join(_REPO_ROOT, "whisper.cpp", "models")
# Prefer English base; fall back to smaller models. Skip for-tests-*.bin stubs (< ~1 MiB).
_WHISPER_MODEL_CANDIDATES = (
    "ggml-base.en.bin",
    "ggml-base.bin",
    "ggml-small.en.bin",
    "ggml-small.bin",
    "ggml-tiny.en.bin",
    "ggml-tiny.bin",
)
_WHISPER_MODEL_MIN_BYTES = 1_000_000
_LLAMA_CLI = os.path.join(_REPO_ROOT, "llama.cpp", "build", "bin", "llama-cli")
_LLAMA_MODEL_DIR = os.path.join(_REPO_ROOT, "models")
_LLAMA_MODEL_MIN_BYTES = 50_000_000
_LLAMA_MODEL_CANDIDATES = (
    "tinyllama-1.1b-chat.Q4_K_M.gguf",
    "tinyllama-1.1b-chat-v1.0-q4_k_m.gguf",
    "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf",
)

_VOICE_CONFIG_PATH = os.path.join(_SCRIPT_DIR, "config.json")
_DEFAULT_LOCATIONS = ["living_room", "bedroom"]
_DEFAULT_ACTIONS = ["turn_demo", "left_once", "right_once"]

_DEFAULT_WAKE_REFRACTORY_S = 2.5
_DEFAULT_WAKE_THRESHOLD = 0.5
_DEFAULT_WAKE_MIN_INTERVAL_S = 3.0
_DEFAULT_WAKE_SILENCE_FLUSH_S = 0.0
# llama-cli on Pi CPU can be slow; avoid hanging forever if REPL or runaway generation.
_LLAMA_SUBPROCESS_TIMEOUT_S = 180

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
        # Absolute path to a ggml model, or None: env PI_VOICE_WHISPER_MODEL, then whisper.cpp/models/*.
        "whisper_model_path": None,
        # Absolute path to TinyLlama (or compatible) .gguf, or None: env PI_VOICE_LLAMA_MODEL, then models/*.
        "llama_model_path": None,
        # After each command, ignore wake scores until this many seconds have passed (OWW buffer flush).
        "wake_refractory_s": _DEFAULT_WAKE_REFRACTORY_S,
        # OpenWakeWord score threshold for hey_homie (0–1).
        "wake_threshold": _DEFAULT_WAKE_THRESHOLD,
        # Minimum seconds between accepted wake activations; 0 disables (backstop for double-fires).
        "wake_min_interval_s": _DEFAULT_WAKE_MIN_INTERVAL_S,
        # After reopening the mic, drain hardware while feeding silence through predict (seconds); 0 disables.
        "wake_silence_flush_s": _DEFAULT_WAKE_SILENCE_FLUSH_S,
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

    def _coerce_float(v, default):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    r = _coerce_float(cfg.get("wake_refractory_s"), _DEFAULT_WAKE_REFRACTORY_S)
    cfg["wake_refractory_s"] = r if r >= 0 else _DEFAULT_WAKE_REFRACTORY_S

    thr = _coerce_float(cfg.get("wake_threshold"), _DEFAULT_WAKE_THRESHOLD)
    cfg["wake_threshold"] = min(1.0, max(0.05, thr))

    mi = _coerce_float(cfg.get("wake_min_interval_s"), _DEFAULT_WAKE_MIN_INTERVAL_S)
    cfg["wake_min_interval_s"] = max(0.0, mi)

    sf = _coerce_float(cfg.get("wake_silence_flush_s"), _DEFAULT_WAKE_SILENCE_FLUSH_S)
    cfg["wake_silence_flush_s"] = sf if sf >= 0 else _DEFAULT_WAKE_SILENCE_FLUSH_S

    return cfg


def resolve_whisper_model_path(cfg):
    """
    Return path to a usable ggml model, or None.
    Order: PI_VOICE_WHISPER_MODEL, config whisper_model_path, then known names under whisper.cpp/models/.
    """
    env = os.environ.get("PI_VOICE_WHISPER_MODEL", "").strip()
    if env:
        env = os.path.expanduser(env)
        if os.path.isfile(env) and os.path.getsize(env) >= _WHISPER_MODEL_MIN_BYTES:
            return os.path.abspath(env)
    p = cfg.get("whisper_model_path")
    if isinstance(p, str) and p.strip():
        p = os.path.expanduser(p.strip())
        if os.path.isfile(p) and os.path.getsize(p) >= _WHISPER_MODEL_MIN_BYTES:
            return os.path.abspath(p)
    for name in _WHISPER_MODEL_CANDIDATES:
        fp = os.path.join(_WHISPER_MODEL_DIR, name)
        if os.path.isfile(fp) and os.path.getsize(fp) >= _WHISPER_MODEL_MIN_BYTES:
            return os.path.abspath(fp)
    return None


def resolve_llama_model_path(cfg):
    """
    Return path to a usable .gguf for llama-cli, or None.
    Order: PI_VOICE_LLAMA_MODEL, config llama_model_path, known names under repo models/, then any models/tinyllama*.gguf.
    """
    env = os.environ.get("PI_VOICE_LLAMA_MODEL", "").strip()
    if env:
        env = os.path.expanduser(env)
        if os.path.isfile(env) and os.path.getsize(env) >= _LLAMA_MODEL_MIN_BYTES:
            return os.path.abspath(env)
    p = cfg.get("llama_model_path")
    if isinstance(p, str) and p.strip():
        p = os.path.expanduser(p.strip())
        if os.path.isfile(p) and os.path.getsize(p) >= _LLAMA_MODEL_MIN_BYTES:
            return os.path.abspath(p)
    for name in _LLAMA_MODEL_CANDIDATES:
        fp = os.path.join(_LLAMA_MODEL_DIR, name)
        if os.path.isfile(fp) and os.path.getsize(fp) >= _LLAMA_MODEL_MIN_BYTES:
            return os.path.abspath(fp)
    try:
        for fn in sorted(os.listdir(_LLAMA_MODEL_DIR)):
            low = fn.lower()
            if not low.endswith(".gguf") or "tinyllama" not in low:
                continue
            fp = os.path.join(_LLAMA_MODEL_DIR, fn)
            if os.path.isfile(fp) and os.path.getsize(fp) >= _LLAMA_MODEL_MIN_BYTES:
                return os.path.abspath(fp)
    except OSError:
        pass
    return None


def llama_infer_cmd(model_path):
    """
    Flags tuned for Pi / embedded Linux: CPU-only, modest ctx, optional no-mmap (SD/mmap quirks).
    Set PI_VOICE_LLAMA_MMAP=1 to omit --no-mmap (e.g. older llama-cli without the flag).
    Uses --single-turn so llama-cli exits after one -p reply (default is interactive REPL).
    """
    cmd = [
        _LLAMA_CLI,
        "-m",
        model_path,
    ]
    if os.environ.get("PI_VOICE_LLAMA_MMAP", "").strip() != "1":
        cmd.append("--no-mmap")
    cmd.extend(
        [
            "-c",
            "1024",
            "-ngl",
            "0",
            "-n",
            "64",
            "--temp",
            "0.1",
            # Keep output pipe-friendly (helps subprocess capture).
            "--simple-io",
            "--no-display-prompt",
            # Reduce extra formatting/logging that can confuse JSON parsing.
            "-co",
            "off",
            "-lv",
            "0",
            # Exit after one -p completion; default llama-cli is interactive REPL (hangs subprocess.run).
            "--single-turn",
            "-p",
        ]
    )
    return cmd


def _parse_first_intent_json(text: str):
    """
    First JSON object in text that is a dict with 'location' and 'action'.
    Uses JSONDecoder.raw_decode so multiple trailing objects or prose do not break parsing.
    Prefers content inside a ```json ... ``` fence, then scans full text.
    """
    if not text or not text.strip():
        return None
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    regions = []
    if fence:
        regions.append(fence.group(1).strip())
    regions.append(text)

    dec = json.JSONDecoder()
    for region in regions:
        i = 0
        while True:
            brace = region.find("{", i)
            if brace < 0:
                break
            try:
                obj, end = dec.raw_decode(region, brace)
            except json.JSONDecodeError:
                i = brace + 1
                continue
            if isinstance(obj, dict) and "location" in obj and "action" in obj:
                return obj
            i = end
    return None


def whisper_transcribe_cmd(model_path):
    return [
        _WHISPER_CLI,
        "-m",
        model_path,
        "-f",
        WAV_OUTPUT_FILENAME,
        "-nt",
        "-otxt",
    ]


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


def flush_openwakeword_with_silence(oww_model, mic_stream, read_n, duration_s):
    """Drain the capture device while feeding silence through predict (clears OWW streaming buffer)."""
    if duration_s <= 0:
        return
    silence = np.zeros(CHUNK_SIZE, dtype=np.int16)
    deadline = time.monotonic() + duration_s
    while time.monotonic() < deadline:
        mic_stream.read(read_n, exception_on_overflow=False)
        oww_model.predict(silence)


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

def transcribe_audio(whisper_model_path):
    print("Transcribing with Whisper...")
    txt_sidecar = WAV_OUTPUT_FILENAME + ".txt"
    if os.path.isfile(txt_sidecar):
        try:
            os.remove(txt_sidecar)
        except OSError:
            pass
    result = subprocess.run(
        whisper_transcribe_cmd(whisper_model_path), capture_output=True, text=True
    )
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

def extract_intent(transcript, locations, actions, llama_model_path):
    print("Extracting intent with TinyLlama...")
    loc_str = ", ".join(str(x) for x in locations)
    act_str = ", ".join(str(x) for x in actions)
    prompt = f"""You are a smart home parser. Read the user command and output ONLY valid JSON with 'location' and 'action' keys.
Output exactly one JSON object on a single line; no examples, no markdown, no explanation.
Available locations: {loc_str}.
Available actions: {act_str}.
Command: "{transcript}"
JSON Output:"""

    cmd = llama_infer_cmd(llama_model_path) + [prompt]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_LLAMA_SUBPROCESS_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        print(
            f"llama-cli timed out after {_LLAMA_SUBPROCESS_TIMEOUT_S}s "
            "(model too slow or still in interactive mode)."
        )
        proc = getattr(exc, "process", None)
        if proc is not None:
            try:
                proc.kill()
            except OSError:
                pass
        return None

    raw_out = (result.stdout or "").strip()
    raw_err = (result.stderr or "").strip()
    # Many llama-cli builds print the REPL banner and -p completion on stderr only; stdout is empty.
    combined = "\n".join(s for s in (raw_out, raw_err) if s)
    output = combined.replace(prompt, "").strip()
    if not output and combined:
        output = combined
    tail_log = output[-1500:] if len(output) > 1500 else output
    print(f"LLM Output (tail): {tail_log!r}" if tail_log else "LLM Output: (empty)")

    if not output or "Failed to load" in combined:
        print(f"llama-cli model was: {llama_model_path!r}")
        if raw_out:
            tail = raw_out[-2000:] if len(raw_out) > 2000 else raw_out
            print(f"llama-cli stdout (last part): {tail}")
        elif raw_err:
            print("llama-cli stdout: (empty); completion text may be on stderr only.")
        if raw_err:
            tail = raw_err[-1200:] if len(raw_err) > 1200 else raw_err
            print(f"llama-cli stderr (last part): {tail}")
        if result.returncode != 0:
            print(f"llama-cli exited with code {result.returncode}")
        if "Failed to load" in combined:
            print(
                "Hint: confirm file size matches a full TinyLlama GGUF (~600+ MiB for Q4_K_M); "
                "try `PI_VOICE_LLAMA_MMAP=1 python3 ...` if --no-mmap is unsupported; "
                "rebuild llama.cpp if the binary is old (needs -ngl / --no-mmap)."
            )

    intent = _parse_first_intent_json(output)
    if intent is None:
        print("Failed to parse LLM output into JSON.")
        if output:
            dbg = output[-1200:] if len(output) > 1200 else output
            print(f"Unparsed buffer (tail): {dbg!r}")
        return None
    return intent

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

    whisper_model = resolve_whisper_model_path(cfg)
    if not whisper_model:
        print(
            "No Whisper ggml model found. Expected a real model (not for-tests-*.bin) under\n"
            f"  {_WHISPER_MODEL_DIR}\n"
            "e.g. ggml-base.en.bin (run whisper.cpp/models/download-ggml-model.sh base.en) or\n"
            "ggml-tiny.en.bin. Override with config.json \"whisper_model_path\" or env PI_VOICE_WHISPER_MODEL."
        )
        sys.exit(1)
    if not os.path.isfile(_WHISPER_CLI):
        print(f"whisper-cli not found at {_WHISPER_CLI} (build whisper.cpp first).")
        sys.exit(1)
    print(f"Whisper model: {whisper_model}")

    llama_model = resolve_llama_model_path(cfg)
    if not llama_model:
        print(
            "No Llama .gguf model found under\n"
            f"  {_LLAMA_MODEL_DIR}\n"
            "Expected TinyLlama ~Q4_K_M (≥50 MiB), e.g. tinyllama-1.1b-chat.Q4_K_M.gguf or\n"
            "tinyllama-1.1b-chat-v1.0-q4_k_m.gguf. Set \"llama_model_path\" in config.json or PI_VOICE_LLAMA_MODEL."
        )
        sys.exit(1)
    if not os.path.isfile(_LLAMA_CLI):
        print(f"llama-cli not found at {_LLAMA_CLI} (build llama.cpp first).")
        sys.exit(1)
    try:
        _sz = os.path.getsize(llama_model)
        print(f"Llama model: {llama_model} ({_sz // (1024 * 1024)} MiB)")
    except OSError:
        print(f"Llama model: {llama_model}")

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

        wake_refractory_until = 0.0
        last_accepted_wake_at = None
        wake_thr = float(cfg["wake_threshold"])
        wake_ref_s = float(cfg["wake_refractory_s"])
        wake_min_iv = float(cfg["wake_min_interval_s"])
        wake_flush_s = float(cfg["wake_silence_flush_s"])

        while True:
            raw = np.frombuffer(mic_stream.read(read_n, exception_on_overflow=False), dtype=np.int16)
            if capture_rate != AUDIO_RATE:
                audio_data = resample_int16_to_rate(raw, capture_rate, AUDIO_RATE, CHUNK_SIZE)
            else:
                audio_data = raw

            prediction = oww_model.predict(audio_data)
            now = time.monotonic()
            try:
                score = float(prediction[WAKE_WORD_NAME])
            except (KeyError, TypeError, ValueError):
                score = 0.0

            refractory_ok = now >= wake_refractory_until
            interval_ok = True
            if wake_min_iv > 0 and last_accepted_wake_at is not None:
                interval_ok = (now - last_accepted_wake_at) >= wake_min_iv

            if refractory_ok and interval_ok and score > wake_thr:
                last_accepted_wake_at = now
                print("\nWake word detected!")

                # Release ALSA/PortAudio device before opening a second stream in record_audio;
                # stop_stream() alone keeps the device busy → OSError -9985 Device unavailable.
                mic_stream.stop_stream()
                mic_stream.close()
                mic_stream = None

                try:
                    record_audio(p, input_dev, capture_rate)
                    transcript = transcribe_audio(whisper_model)

                    if transcript:
                        intent = extract_intent(
                            transcript, cfg["locations"], cfg["actions"], llama_model
                        )
                        if intent and "location" in intent and "action" in intent:
                            trigger_actuator(intent, cfg["command_center_url"])
                finally:
                    print(f"\nWaiting for wake word 'hey homie'...")
                    mic_stream = open_mic_stream(p, input_dev, capture_rate, read_n)
                    flush_openwakeword_with_silence(oww_model, mic_stream, read_n, wake_flush_s)
                    wake_refractory_until = time.monotonic() + wake_ref_s

    except KeyboardInterrupt:
        print("Stopping...")
    finally:
        if mic_stream is not None:
            mic_stream.close()
        p.terminate()

if __name__ == "__main__":
    main()