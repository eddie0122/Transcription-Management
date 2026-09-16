"""Transcription engine adapters.

Two engines, selected by hardware preference:
  - whisper.cpp  (Apple Silicon / Metal, also CPU) — external `whisper-cli`
  - faster-whisper / CTranslate2 (NVIDIA CUDA, also CPU) — Python library

The adapters normalize engine-specific parameters and model files. An
explicitly selected backend is never silently replaced: resolution raises
EngineError with an actionable message and a suggested compatible fallback
instead.
"""
import inspect
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import config
from .media import tail

HF_BASE = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"

# Known ggml models for whisper.cpp: name -> (filename, approx bytes, multilingual)
WHISPERCPP_MODELS = {
    "tiny":            ("ggml-tiny.bin",            77_700_000, True),
    "tiny.en":         ("ggml-tiny.en.bin",         77_700_000, False),
    "base":            ("ggml-base.bin",           148_000_000, True),
    "base.en":         ("ggml-base.en.bin",        148_000_000, False),
    "small":           ("ggml-small.bin",          488_000_000, True),
    "small.en":        ("ggml-small.en.bin",       488_000_000, False),
    "medium":          ("ggml-medium.bin",       1_530_000_000, True),
    "large-v3":        ("ggml-large-v3.bin",     3_100_000_000, True),
    "large-v3-turbo":  ("ggml-large-v3-turbo.bin", 1_620_000_000, True),
    "large-v3-turbo-q5_0": ("ggml-large-v3-turbo-q5_0.bin", 574_000_000, True),
}

# faster-whisper model identifiers (downloaded by the library into
# MODEL_CACHE_DIR/faster-whisper; approximate sizes for disk-space checks).
FASTERWHISPER_MODELS = {
    "tiny": 78_000_000, "base": 148_000_000, "small": 488_000_000,
    "medium": 1_530_000_000, "large-v3": 3_100_000_000,
    "large-v3-turbo": 1_620_000_000,
}

# Accuracy/speed presets -> engine-neutral optimized defaults. Every preset
# carries the anti-repetition safeguards (temperature fallback ladder, no
# cross-window text conditioning, VAD); they differ in decoding search width.
# Any key can be overridden per job from the Advanced panel.
PRESETS = {
    "fast":     {"beam_size": 1, "best_of": 1, "temperature": 0.0,
                 "temperature_fallback": True,
                 "condition_on_previous_text": False, "vad": True},
    "balanced": {"beam_size": 3, "best_of": 3, "temperature": 0.0,
                 "temperature_fallback": True,
                 "condition_on_previous_text": False, "vad": True},
    "accuracy": {"beam_size": 5, "best_of": 5, "temperature": 0.0,
                 "temperature_fallback": True,
                 "condition_on_previous_text": False, "vad": True},
}


class EngineError(Exception):
    def __init__(self, message: str, fallback: Optional[str] = None):
        super().__init__(message)
        self.fallback = fallback


def whispercpp_binary() -> Optional[str]:
    candidates = [config.WHISPER_CPP_BIN] if config.WHISPER_CPP_BIN else []
    candidates += [
        shutil.which("whisper-cli") or "",
        shutil.which("whisper-cpp") or "",
        str(Path(__file__).resolve().parents[2] / "native" / "whisper.cpp" /
            "build" / "bin" / "whisper-cli"),
        "/opt/homebrew/bin/whisper-cli",
        "/usr/local/bin/whisper-cli",
    ]
    for c in candidates:
        if c and Path(c).is_file() and os.access(c, os.X_OK):
            return c
    return None


def _fasterwhisper_available():
    try:
        import faster_whisper  # noqa: F401
        return faster_whisper
    except ImportError:
        return None


def cuda_device_count() -> int:
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count()
    except Exception:
        return 0


_detect_cache: Dict[str, Any] = {}
_detect_lock = threading.Lock()


def detect(force: bool = False) -> Dict[str, Any]:
    """Installed engines, devices, model availability, compute types."""
    with _detect_lock:
        if _detect_cache and not force:
            return _detect_cache
        wc_bin = whispercpp_binary()
        fw = _fasterwhisper_available()
        cuda = cuda_device_count() if fw else 0
        wc_models = installed_models("whispercpp")
        fw_models = installed_models("fasterwhisper")
        info = {
            "platform": {"os": "macOS" if config.IS_MACOS else os.uname().sysname,
                         "arch": os.uname().machine},
            "engines": {
                "whispercpp": {
                    "available": bool(wc_bin), "binary": wc_bin,
                    "metal": bool(wc_bin) and config.IS_MACOS and config.IS_ARM64,
                    "models_installed": wc_models,
                    "install_hint": "Install with `brew install whisper-cpp`, or build "
                                    "from source and set WHISPER_CPP_BIN.",
                },
                "fasterwhisper": {
                    "available": bool(fw),
                    "version": getattr(fw, "__version__", None) if fw else None,
                    "cuda_devices": cuda,
                    "models_installed": fw_models,
                    "install_hint": "pip install faster-whisper (bundled in the Docker "
                                    "backend image).",
                },
            },
            "devices": _device_options(bool(wc_bin), bool(fw), cuda),
            "decoders": None,  # filled by API layer
        }
        _detect_cache.clear()
        _detect_cache.update(info)
        return info


def _device_options(has_wc: bool, has_fw: bool, cuda: int) -> List[Dict[str, Any]]:
    apple_ok = has_wc and config.IS_MACOS and config.IS_ARM64
    nvidia_ok = has_fw and cuda > 0
    cpu_ok = has_wc or has_fw
    opts = [
        {"id": "auto", "label": "Auto", "available": apple_ok or nvidia_ok or cpu_ok,
         "detail": "Selects a compatible installed engine, preferring supported GPU acceleration.",
         "how_to_enable": "Install whisper.cpp (macOS) or faster-whisper (NVIDIA/CPU)."},
        {"id": "nvidia", "label": "NVIDIA (faster-whisper / CUDA)", "available": nvidia_ok,
         "detail": f"CTranslate2 CUDA devices detected: {cuda}." if nvidia_ok else
                   ("faster-whisper is not installed." if not has_fw else
                    "No CUDA device is visible. In Docker, verify the GPU reservation "
                    "and NVIDIA driver; run the WSL2 GPU check in the deployment guide."),
         "how_to_enable": "Use the Docker/WSL2 deployment with an NVIDIA GPU, or install "
                          "faster-whisper with CUDA/cuDNN on a machine with an NVIDIA GPU."},
        {"id": "apple", "label": "Apple Silicon (whisper.cpp / Metal)", "available": apple_ok,
         "detail": "Native whisper.cpp with Metal acceleration." if apple_ok else
                   ("whisper.cpp binary not found." if config.IS_MACOS else
                    "Apple Silicon inference requires running natively on macOS; it is "
                    "not available inside the Docker/WSL2 deployment."),
         "how_to_enable": "On an Apple Silicon Mac, run scripts/macos/setup.sh "
                          "(installs whisper.cpp)."},
        {"id": "cpu", "label": "CPU", "available": cpu_ok,
         "detail": "CPU inference via an installed adapter. Slower; prefer small models.",
         "how_to_enable": "Install whisper.cpp or faster-whisper."},
    ]
    return opts


def models_catalog() -> Dict[str, Any]:
    wc_installed = installed_models("whispercpp")
    fw_installed = installed_models("fasterwhisper")
    return {
        "whispercpp": [
            {"id": name, "file": fn, "bytes": size, "multilingual": multi,
             "installed": name in wc_installed}
            for name, (fn, size, multi) in WHISPERCPP_MODELS.items()
        ],
        "fasterwhisper": [
            {"id": name, "bytes": size, "multilingual": True,
             "installed": name in fw_installed}
            for name, size in FASTERWHISPER_MODELS.items()
        ],
    }


def installed_models(engine: str) -> List[str]:
    out = []
    if engine == "whispercpp":
        d = config.MODEL_CACHE_DIR / "whispercpp"
        for name, (fn, _size, _m) in WHISPERCPP_MODELS.items():
            if (d / fn).is_file():
                out.append(name)
    else:
        d = config.MODEL_CACHE_DIR / "faster-whisper"
        if d.is_dir():
            for name in FASTERWHISPER_MODELS:
                if (d / name / "model.bin").is_file():
                    out.append(name)
    return out


def download_model(engine: str, model: str,
                   on_progress: Callable[[int, int], None],
                   cancel: Optional[threading.Event] = None) -> None:
    """Download a model with progress and a disk-space check."""
    if engine == "whispercpp":
        if model not in WHISPERCPP_MODELS:
            raise EngineError(f"Unknown whisper.cpp model '{model}'.")
        fn, size, _m = WHISPERCPP_MODELS[model]
        dest_dir = config.MODEL_CACHE_DIR / "whispercpp"
        dest_dir.mkdir(parents=True, exist_ok=True)
        _check_disk(dest_dir, size)
        dest, part = dest_dir / fn, dest_dir / (fn + ".part")
        url = f"{HF_BASE}/{fn}"
        req = urllib.request.Request(url, headers={"User-Agent": "AudioTranscription/0.1"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(part, "wb") as f:
            total = int(resp.headers.get("Content-Length") or size)
            done = 0
            while True:
                if cancel is not None and cancel.is_set():
                    raise EngineError("Model download canceled.")
                chunk = resp.read(1024 * 512)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                on_progress(done, total)
        part.rename(dest)
    elif engine == "fasterwhisper":
        fw = _fasterwhisper_available()
        if not fw:
            raise EngineError("faster-whisper is not installed in this deployment.")
        if model not in FASTERWHISPER_MODELS:
            raise EngineError(f"Unknown faster-whisper model '{model}'.")
        dest = config.MODEL_CACHE_DIR / "faster-whisper" / model
        dest.mkdir(parents=True, exist_ok=True)
        _check_disk(dest, FASTERWHISPER_MODELS[model])
        on_progress(0, FASTERWHISPER_MODELS[model])
        # Explicitly directed download; the library reports no byte progress.
        fw.download_model(model, output_dir=str(dest))
        on_progress(FASTERWHISPER_MODELS[model], FASTERWHISPER_MODELS[model])
    else:
        raise EngineError(f"Unknown engine '{engine}'.")


def delete_model(engine: str, model: str) -> None:
    if engine == "whispercpp" and model in WHISPERCPP_MODELS:
        fn = WHISPERCPP_MODELS[model][0]
        p = config.MODEL_CACHE_DIR / "whispercpp" / fn
        if p.is_file():
            p.unlink()
    elif engine == "fasterwhisper" and model in FASTERWHISPER_MODELS:
        p = config.MODEL_CACHE_DIR / "faster-whisper" / model
        if p.is_dir():
            shutil.rmtree(p)


def _check_disk(directory: Path, needed: int) -> None:
    free = shutil.disk_usage(str(directory)).free
    if free < needed + 500_000_000:
        raise EngineError(
            f"Not enough disk space: this model needs ~{needed / 1e9:.1f} GB plus "
            f"working room, but only {free / 1e9:.1f} GB is free at {directory}."
        )


# --------------------------------------------------------------------------
# Adapters


class WhisperCppEngine:
    name = "whispercpp"

    # Parameters this adapter supports (drives the UI's advanced panel).
    capabilities = {
        "beam_size": True, "temperature": True, "initial_prompt": True,
        "word_timestamps": False, "vad": False,
        "compute_types": [],  # precision fixed by the ggml model file
        # advanced.extra_args: raw whisper-cli flags appended to the command
        "extra_mode": "cli",
    }

    # Flags the adapter owns (input, model, JSON output contract); extra
    # arguments may not override them.
    _MANAGED_FLAGS = {"-m", "--model", "-f", "--file", "-of", "--output-file",
                      "-oj", "--output-json", "-ojf", "--output-json-full",
                      "-h", "--help"}

    @classmethod
    def _extra_args(cls, opts: Dict[str, Any]) -> List[str]:
        raw = str(opts.get("extra_args") or "").strip()
        if not raw:
            return []
        try:
            args = shlex.split(raw)
        except ValueError as e:
            raise EngineError(f"Could not parse extra whisper-cli arguments: {e}")
        clash = sorted(cls._MANAGED_FLAGS.intersection(args))
        if clash:
            raise EngineError(
                "Extra whisper-cli arguments may not override managed flags: "
                + ", ".join(clash) + ".")
        return args

    def __init__(self, device: str):
        self.device = device  # 'apple' or 'cpu'
        self.binary = whispercpp_binary()
        if not self.binary:
            raise EngineError(
                "whisper.cpp binary not found. Install it with `brew install "
                "whisper-cpp` or run scripts/macos/setup.sh, or set WHISPER_CPP_BIN."
            )

    def model_path(self, model: str) -> Path:
        if model not in WHISPERCPP_MODELS:
            raise EngineError(f"Unknown whisper.cpp model '{model}'.")
        p = config.MODEL_CACHE_DIR / "whispercpp" / WHISPERCPP_MODELS[model][0]
        if not p.is_file():
            size = WHISPERCPP_MODELS[model][1]
            raise EngineError(
                f"Model '{model}' is not downloaded (~{size / 1e6:.0f} MB). "
                "Download it from Settings → Transcription → Models."
            )
        return p

    def transcribe(self, wav_path: Path, opts: Dict[str, Any],
                   on_progress: Optional[Callable[[float], None]] = None,
                   cancel: Optional[threading.Event] = None,
                   nice: bool = False) -> Dict[str, Any]:
        model_file = self.model_path(opts.get("model", "base"))
        out_prefix = wav_path.with_suffix("")
        cmd = [self.binary, "-m", str(model_file), "-f", str(wav_path),
               "-oj", "-of", str(out_prefix), "--print-progress",
               "-t", str(max(1, min(8, os.cpu_count() or 4)))]
        lang = opts.get("language") or "auto"
        cmd += ["-l", lang]
        if opts.get("beam_size"):
            cmd += ["-bs", str(int(opts["beam_size"]))]
        if opts.get("best_of"):
            cmd += ["-bo", str(int(opts["best_of"]))]
        if opts.get("temperature") is not None:
            cmd += ["-tp", str(float(opts["temperature"]))]
        if not opts.get("temperature_fallback", True):
            cmd += ["-nf"]  # disable the temperature retry ladder
        if not opts.get("condition_on_previous_text", False) \
                and not opts.get("initial_prompt"):
            # No cross-window text conditioning, so a repetition glitch in one
            # window cannot cascade through the rest of the file. -mc 0 also
            # discards initial-prompt tokens, so conditioning stays on when the
            # user supplied vocabulary hints (whisper.cpp's entropy heuristic
            # still guards against loops there).
            cmd += ["-mc", "0"]
        if opts.get("initial_prompt"):
            cmd += ["--prompt", str(opts["initial_prompt"])[:800]]
        if self.device == "cpu":
            cmd += ["-ng"]  # disable GPU explicitly for the CPU selection
        cmd += self._extra_args(opts)
        if nice and hasattr(os, "nice"):
            preexec = lambda: os.nice(10)  # noqa: E731
        else:
            preexec = None
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, preexec_fn=preexec)
        stderr_buf: List[str] = []

        def _drain_stderr():
            for line in proc.stderr:  # type: ignore[union-attr]
                stderr_buf.append(line)
                if len(stderr_buf) > 400:
                    del stderr_buf[:200]
                m = re.search(r"progress\s*=\s*(\d+)%", line)
                if m and on_progress:
                    on_progress(min(1.0, int(m.group(1)) / 100.0))

        t = threading.Thread(target=_drain_stderr, daemon=True)
        t.start()
        while True:
            try:
                proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if cancel is not None and cancel.is_set():
                    proc.kill()
                    proc.wait()
                    raise EngineError("Transcription canceled.")
        t.join(timeout=5)
        json_path = Path(str(out_prefix) + ".json")
        if proc.returncode != 0 or not json_path.is_file():
            err = tail("".join(stderr_buf))
            if "failed to load model" in err or "out of memory" in err.lower():
                raise EngineError(
                    "The model failed to load — likely insufficient memory. Try a "
                    f"smaller model or close other applications. Details: {err}"
                )
            raise EngineError(f"whisper.cpp failed (exit {proc.returncode}): {err}")
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        finally:
            json_path.unlink(missing_ok=True)
        segments = []
        for s in data.get("transcription", []):
            offs = s.get("offsets", {})
            text = (s.get("text") or "").strip()
            # Drop pure non-speech annotations emitted by the model, e.g.
            # [BLANK_AUDIO], [MUSIC], (speaking in foreign language).
            if not text or re.fullmatch(r"[\[\(][^\]\)]*[\]\)]", text):
                continue
            segments.append({
                "start_ms": int(offs.get("from", 0)),
                "end_ms": int(offs.get("to", 0)),
                "text": text,
            })
        detected = (data.get("result") or {}).get("language") or lang
        return {"language": detected, "segments": segments}


class FasterWhisperEngine:
    name = "fasterwhisper"

    capabilities = {
        "beam_size": True, "temperature": True, "initial_prompt": True,
        "word_timestamps": True, "vad": True,
        "compute_types": ["auto", "float16", "int8_float16", "int8", "float32"],
        # advanced.extra: key/value map of any transcribe() parameter
        "extra_mode": "kwargs",
    }

    _model_cache: Dict[str, Any] = {}
    _model_lock = threading.Lock()

    def __init__(self, device: str):
        self.device = device  # 'nvidia' or 'cpu'
        self.fw = _fasterwhisper_available()
        if not self.fw:
            raise EngineError(
                "faster-whisper is not installed in this deployment. Use the "
                "Docker/WSL2 deployment for NVIDIA, or `pip install faster-whisper`."
            )
        if device == "nvidia" and cuda_device_count() == 0:
            raise EngineError(
                "No CUDA device is visible to CTranslate2. Verify the NVIDIA driver, "
                "the Compose GPU reservation, and the WSL2 GPU check in the "
                "deployment guide.", fallback="cpu",
            )

    def _load(self, model: str, compute_type: str):
        if model not in FASTERWHISPER_MODELS:
            raise EngineError(f"Unknown faster-whisper model '{model}'.")
        model_dir = config.MODEL_CACHE_DIR / "faster-whisper" / model
        if not (model_dir / "model.bin").is_file():
            raise EngineError(
                f"Model '{model}' is not downloaded. Download it from Settings → "
                "Transcription → Models."
            )
        device = "cuda" if self.device == "nvidia" else "cpu"
        if compute_type in ("", "auto", None):
            compute_type = "float16" if device == "cuda" else "int8"
        key = f"{model}|{device}|{compute_type}"
        with self._model_lock:
            if key not in self._model_cache:
                try:
                    self._model_cache.clear()  # hold at most one loaded model
                    self._model_cache[key] = self.fw.WhisperModel(
                        str(model_dir), device=device, compute_type=compute_type)
                except (RuntimeError, ValueError) as e:
                    msg = str(e)
                    if "out of memory" in msg.lower():
                        raise EngineError(
                            f"GPU out of memory loading '{model}'. Use a smaller model "
                            "or a quantized compute type (int8_float16)."
                        )
                    raise EngineError(f"Failed to load model: {msg}")
            return self._model_cache[key]

    @staticmethod
    def _validated_extra(model, extra: Any) -> Dict[str, Any]:
        """Any other faster-whisper transcribe() parameter, passed through
        after validation against the installed library's actual signature."""
        if not extra:
            return {}
        if not isinstance(extra, dict):
            raise EngineError("Advanced 'extra' parameters must be a key/value map.")
        valid = set(inspect.signature(model.transcribe).parameters) - {"audio"}
        unknown = sorted(set(extra) - valid)
        if unknown:
            raise EngineError(
                "Unknown faster-whisper parameter(s): " + ", ".join(unknown)
                + ". Supported parameters: " + ", ".join(sorted(valid)) + ".")
        return dict(extra)

    def transcribe(self, wav_path: Path, opts: Dict[str, Any],
                   on_progress: Optional[Callable[[float], None]] = None,
                   cancel: Optional[threading.Event] = None,
                   nice: bool = False) -> Dict[str, Any]:
        model = self._load(opts.get("model", "base"), opts.get("compute_type", "auto"))
        # Guard against Whisper repetition loops: give the decoder its
        # temperature-fallback ladder (retries a segment when the output is
        # repetitive; a bare scalar disables the retry entirely), and do not
        # feed each window's text into the next one, so a glitch in one
        # 30-second window cannot cascade through the rest of the file.
        base_temp = float(opts.get("temperature") or 0.0)
        if opts.get("temperature_fallback", True):
            temperature: Any = [t for t in (base_temp, 0.2, 0.4, 0.6, 0.8, 1.0)
                                if t >= base_temp]
        else:
            temperature = base_temp
        kwargs: Dict[str, Any] = {
            "beam_size": int(opts.get("beam_size") or 5),
            "temperature": temperature,
            "condition_on_previous_text": bool(
                opts.get("condition_on_previous_text", False)),
            "vad_filter": bool(opts.get("vad", True)),
            "word_timestamps": bool(opts.get("word_timestamps", False)),
        }
        if opts.get("best_of"):
            kwargs["best_of"] = int(opts["best_of"])
        if opts.get("language"):
            kwargs["language"] = opts["language"]
        if opts.get("initial_prompt"):
            kwargs["initial_prompt"] = str(opts["initial_prompt"])[:800]
        kwargs.update(self._validated_extra(model, opts.get("extra")))
        segments_iter, info = model.transcribe(str(wav_path), **kwargs)
        duration = getattr(info, "duration", None) or 1.0
        segments = []
        for s in segments_iter:
            if cancel is not None and cancel.is_set():
                raise EngineError("Transcription canceled.")
            text = (s.text or "").strip()
            if text:
                seg = {"start_ms": int(s.start * 1000), "end_ms": int(s.end * 1000),
                       "text": text}
                if kwargs["word_timestamps"] and s.words:
                    seg["words"] = [{"start_ms": int(w.start * 1000),
                                     "end_ms": int(w.end * 1000), "word": w.word}
                                    for w in s.words]
                segments.append(seg)
            if on_progress:
                on_progress(min(1.0, s.end / duration))
        return {"language": getattr(info, "language", None) or opts.get("language"),
                "segments": segments}


def resolve(device_setting: str):
    """Map the user's hardware setting to a compatible engine instance.

    Raises EngineError (never silently substitutes) when an explicit choice
    is unavailable; `fallback` suggests a compatible alternative.
    """
    sysinfo = detect(force=True)
    wc = sysinfo["engines"]["whispercpp"]["available"]
    fw = sysinfo["engines"]["fasterwhisper"]["available"]
    cuda = sysinfo["engines"]["fasterwhisper"]["cuda_devices"]
    apple_ok = wc and config.IS_MACOS and config.IS_ARM64

    if device_setting == "apple":
        if not config.IS_MACOS:
            raise EngineError(
                "Apple Silicon inference runs natively on macOS only; it is not "
                "available in this deployment.", fallback="nvidia" if cuda else "cpu")
        return WhisperCppEngine("apple"), "whisper.cpp (Metal, Apple Silicon)"
    if device_setting == "nvidia":
        eng = FasterWhisperEngine("nvidia")  # raises with fallback if no CUDA
        return eng, "faster-whisper (CUDA)"
    if device_setting == "cpu":
        if wc:
            return WhisperCppEngine("cpu"), "whisper.cpp (CPU)"
        if fw:
            return FasterWhisperEngine("cpu"), "faster-whisper (CPU)"
        raise EngineError("No transcription engine is installed. Install whisper.cpp "
                          "or faster-whisper (see the deployment guide).")
    # auto
    if apple_ok:
        return WhisperCppEngine("apple"), "whisper.cpp (Metal, Apple Silicon) — auto"
    if fw and cuda > 0:
        return FasterWhisperEngine("nvidia"), "faster-whisper (CUDA) — auto"
    if wc:
        return WhisperCppEngine("cpu"), "whisper.cpp (CPU) — auto"
    if fw:
        return FasterWhisperEngine("cpu"), "faster-whisper (CPU) — auto"
    raise EngineError(
        "No transcription engine is installed. On macOS run scripts/macos/setup.sh; "
        "in Docker use the provided backend image."
    )
