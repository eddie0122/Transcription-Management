#!/bin/bash
# Native macOS setup — no Docker required (or allowed) for this deployment.
# Verifies the platform, installs/verifies pinned dependencies, prepares an
# isolated backend environment, builds the native capture helper, and
# initializes writable storage. Idempotent: safe to re-run.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MIN_MACOS_MAJOR=13

say_step() { printf '\n==> %s\n' "$1"; }
die() { printf 'ERROR: %s\n' "$1" >&2; exit 1; }

say_step "Checking platform"
[ "$(uname -s)" = "Darwin" ] || die "This script is for macOS."
[ "$(uname -m)" = "arm64" ] || die "Apple Silicon (arm64) is required for the initial release. Intel Macs are outside the supported hardware matrix."
MACOS_MAJOR=$(sw_vers -productVersion | cut -d. -f1)
[ "$MACOS_MAJOR" -ge "$MIN_MACOS_MAJOR" ] || die "macOS $MIN_MACOS_MAJOR or newer is required (ScreenCaptureKit audio capture). Found $(sw_vers -productVersion)."
xcode-select -p >/dev/null 2>&1 || die "Xcode Command Line Tools are required. Run: xcode-select --install"

say_step "Checking Python"
PYTHON="${PYTHON:-python3}"
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 9), sys.version' \
  || die "Python 3.9+ is required (macOS ships 3.9 with the Command Line Tools)."

say_step "Media decoder"
if command -v ffmpeg >/dev/null; then
  echo "ffmpeg found: full format matrix (WAV, MP3, M4A, AAC, FLAC, OGG, MP4, MOV, MKV, WebM)."
elif command -v brew >/dev/null; then
  echo "Installing ffmpeg via Homebrew..."
  brew install ffmpeg
else
  echo "WARNING: ffmpeg not found and Homebrew is not installed."
  echo "The built-in CoreAudio decoder (afconvert) will be used instead."
  echo "Tested without ffmpeg: WAV, M4A/AAC, MP3, AIFF (default audio track only)."
  echo "For video files (MP4/MOV/MKV/WebM), OGG/FLAC, and audio-track selection,"
  echo "install Homebrew (https://brew.sh) and run: brew install ffmpeg"
fi

say_step "whisper.cpp (Apple Silicon / Metal inference)"
WHISPER_TAG=v1.7.6
LOCAL_BIN="$ROOT/native/whisper.cpp/build/bin/whisper-cli"
if [ -x "$LOCAL_BIN" ]; then
  echo "Found local build: $LOCAL_BIN"
elif command -v whisper-cli >/dev/null; then
  echo "Found whisper-cli on PATH: $(command -v whisper-cli)"
elif command -v brew >/dev/null; then
  echo "Installing whisper-cpp via Homebrew..."
  brew install whisper-cpp
else
  command -v cmake >/dev/null || die "Building whisper.cpp needs cmake (or install Homebrew and re-run for 'brew install whisper-cpp'). Install cmake from https://cmake.org/download/."
  echo "Building whisper.cpp $WHISPER_TAG from source (Metal enabled)..."
  if [ ! -d "$ROOT/native/whisper.cpp" ]; then
    git clone --depth 1 --branch "$WHISPER_TAG" \
      https://github.com/ggml-org/whisper.cpp.git "$ROOT/native/whisper.cpp"
  fi
  cmake -S "$ROOT/native/whisper.cpp" -B "$ROOT/native/whisper.cpp/build" \
    -DCMAKE_BUILD_TYPE=Release -DWHISPER_BUILD_TESTS=OFF
  cmake --build "$ROOT/native/whisper.cpp/build" -j "$(sysctl -n hw.ncpu)" --target whisper-cli
fi

say_step "Backend environment (isolated venv, pinned dependencies)"
cd "$ROOT/backend"
[ -d .venv ] || "$PYTHON" -m venv .venv
.venv/bin/pip install -q -r requirements.txt
echo "Backend dependencies installed."

say_step "Computer-audio capture helper (ScreenCaptureKit)"
if [ ! -x "$ROOT/native/macos-capture/system-audio-capture" ] \
   || [ "$ROOT/native/macos-capture/main.swift" -nt "$ROOT/native/macos-capture/system-audio-capture" ]; then
  (cd "$ROOT/native/macos-capture" && \
   swiftc -O -framework ScreenCaptureKit -framework CoreMedia \
     -framework AVFoundation main.swift -o system-audio-capture)
  echo "Built system-audio-capture."
else
  echo "system-audio-capture already built."
fi
echo "NOTE: the first Computer Audio session will request the Screen & System"
echo "Audio Recording permission for your terminal (or the launching app)."

say_step "Initializing storage"
DATA_DIR="${DATA_DIR:-$HOME/Library/Application Support/AudioTranscription}"
mkdir -p "$DATA_DIR"
echo "Data directory: $DATA_DIR (job data, models, configuration)"
if [ ! -f "$ROOT/scripts/macos/native.env" ]; then
  cp "$ROOT/scripts/macos/native.env.example" "$ROOT/scripts/macos/native.env"
  echo "Created scripts/macos/native.env from the example — review it if needed."
fi

printf '\nSetup complete. Start the app with: ./scripts/macos/start.sh\n'
printf 'Then download a model in Settings → Transcription → Models.\n'
