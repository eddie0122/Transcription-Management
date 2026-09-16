"""Application configuration.

All environment-specific values are read from environment variables so the
same code serves both deployment cases (WSL2/Docker and native macOS).
"""
import os
import platform
import secrets
from pathlib import Path


def _default_data_dir() -> Path:
    env = os.environ.get("DATA_DIR")
    if env:
        return Path(env)
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "AudioTranscription"
    return Path.cwd() / "data"


IS_MACOS = platform.system() == "Darwin"
IS_ARM64 = platform.machine() in ("arm64", "aarch64")

DATA_DIR = _default_data_dir()
MODEL_CACHE_DIR = Path(os.environ.get("MODEL_CACHE_DIR") or (DATA_DIR / "models"))
JOBS_DIR = DATA_DIR / "jobs"
TMP_DIR = DATA_DIR / "tmp"
SECRETS_DIR = DATA_DIR / "secrets"  # file-based secret store (non-macOS)
DB_PATH = DATA_DIR / "app.db"
LOG_DIR = DATA_DIR / "logs"

APP_HOST = os.environ.get("APP_HOST", "127.0.0.1")
APP_PORT = int(os.environ.get("APP_PORT", "8080"))

# Engine hints. "auto" lets the backend pick a compatible installed engine.
TRANSCRIPTION_ENGINE = os.environ.get("TRANSCRIPTION_ENGINE", "auto")
TRANSCRIPTION_DEVICE = os.environ.get("TRANSCRIPTION_DEVICE", "auto")
WHISPER_CPP_BIN = os.environ.get("WHISPER_CPP_BIN", "")

RETENTION_DAYS_DEFAULT = int(os.environ.get("RETENTION_DAYS", "7"))

# Upload/processing limits (surfaced in the UI before processing starts).
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "2048"))
MAX_DURATION_MIN = int(os.environ.get("MAX_DURATION_MIN", "300"))
MAX_QUEUED_JOBS = int(os.environ.get("MAX_QUEUED_JOBS", "100"))

# LLM connections (URL, model, key) are configured entirely in the UI
# (Settings → LLM presets) and stored via the credential store; there is no
# environment- or file-based LLM configuration.

FRONTEND_DIST = os.environ.get(
    "FRONTEND_DIST",
    str(Path(__file__).resolve().parents[2] / "frontend" / "static"),
)

# Path to the macOS system-audio capture helper (ScreenCaptureKit).
MACOS_CAPTURE_BIN = os.environ.get(
    "MACOS_CAPTURE_BIN",
    str(Path(__file__).resolve().parents[2] / "native" / "macos-capture" / "system-audio-capture"),
)

# Live-session defaults (tunable buffering controls, not latency guarantees).
LIVE_PARTIAL_INTERVAL_S = 1.5
LIVE_SILENCE_MS_DEFAULT = 600
LIVE_MAX_CHUNK_S_DEFAULT = 10.0
LIVE_MAX_BACKLOG_S = 60.0        # warn above this
LIVE_HARD_BACKLOG_S = 180.0      # ask the client to pause capture above this

SAMPLE_RATE = 16000  # engine input format: 16 kHz mono s16le

# Per-run session token required for live-audio ingest websockets.
SESSION_TOKEN = secrets.token_urlsafe(24)


def ensure_dirs() -> None:
    for d in (DATA_DIR, MODEL_CACHE_DIR, JOBS_DIR, TMP_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(SECRETS_DIR, 0o700)
    except OSError:
        pass


def is_tba(value) -> bool:
    return not value or str(value).strip().upper() in ("TBA", "TBD", "CHANGEME", "")
