# Deployment 2 — Native macOS (Apple Silicon), no Docker

The frontend, backend, inference engine, and audio capture all run directly
on macOS. **No Docker installation, image, Compose command, or containerized
helper is required or used.** The native setup has no dependency on CUDA,
NVIDIA runtime components, Docker networking hostnames, Docker volumes, or
Docker secrets.

## Requirements

- Apple Silicon Mac (arm64). Intel Macs are outside the initial hardware
  matrix.
- **macOS 13 (Ventura) or newer** — chosen because computer-audio capture
  uses ScreenCaptureKit audio capture (macOS 13+). Validated on macOS 15.6
  (see [TEST_REPORT.md](TEST_REPORT.md)).
- Xcode Command Line Tools (`xcode-select --install`) — provides Python 3.9
  and the Swift compiler used for the capture helper.
- Recommended: [Homebrew](https://brew.sh) for `ffmpeg` (full media format
  matrix) and `whisper-cpp`. Without Homebrew, setup builds whisper.cpp from
  source (needs cmake) and falls back to the built-in CoreAudio decoder.

### Media decoders (tested combinations)

| Decoder | Formats |
| --- | --- |
| ffmpeg (recommended) | WAV, MP3, M4A, AAC, FLAC, OGG, MP4, MOV, MKV, WebM; audio-track selection for multi-track video |
| CoreAudio fallback (afconvert, no install) | WAV, AIFF, M4A/AAC, MP3 — default audio track only |

## Install and run

```bash
./scripts/macos/setup.sh
./scripts/macos/start.sh      # prints and opens http://localhost:8080
./scripts/macos/stop.sh
```

- `setup.sh` verifies macOS version and arm64, installs or verifies pinned
  dependencies (`backend/requirements.txt`), prepares an isolated virtualenv,
  builds/locates `whisper-cli` (Metal) and the ScreenCaptureKit capture
  helper, and initializes writable storage. Idempotent.
- `start.sh` loads `scripts/macos/native.env`, refuses to start a duplicate
  backend against the same job store, detects an occupied port (change
  `APP_PORT` in `native.env`; UI, API, and WebSocket all follow it), waits
  for readiness, and opens the UI. Everything is served from one local
  origin at `http://localhost:8080` with relative `/api/` and `/ws/`
  routes, bound to loopback.
- `stop.sh` requests graceful shutdown: capture devices are released,
  recordings and finalized segments are flushed, owned child processes
  (whisper-cli, capture helper) terminate, and no jobs, settings,
  credentials, or model downloads are deleted.

After first start, download a model in **Settings → Transcription → Models**
(`base` ≈ 148 MB is a good default; `large-v3-turbo-q5_0` ≈ 574 MB for much
better accuracy).

## Permissions

- **Microphone**: captured by your browser; grant the browser's microphone
  permission on first use.
- **Computer Audio**: the native helper uses ScreenCaptureKit. The first
  session triggers the *Screen & System Audio Recording* permission prompt
  for the app that launched the backend (your terminal, typically). Grant it
  in System Settings → Privacy & Security, then start the session again.
  Denial produces a clear in-UI error, never a silent failure.

## Configuration

`scripts/macos/native.env` (created from `native.env.example`) holds the
native configuration: listen address/port, Apple Silicon adapter selection,
data/model paths, and the seven-day retention default. It contains no Docker
values and no secrets.

**The LLM connection is configured entirely in the UI** (Settings → LLM
presets: base URL, model ID, API key, timeout). Until real values replace the
TBA placeholders, translation shows *awaiting configuration* and
transcription works independently. API keys are stored in the **macOS
Keychain** (service `AudioTranscription`), never in files, logs, or the
frontend.

## Storage and maintenance

Data lives in `~/Library/Application Support/AudioTranscription/` (the
resolved location is printed by `setup.sh` and configurable via `DATA_DIR`):

```
app.db      job metadata and transcript segments (SQLite, WAL)
jobs/       per-job media, extracted audio, recordings, exports
models/     downloaded models (never deleted by retention)
tmp/        transient chunk files, cleaned with their session/job
secrets/    file-store fallback (Keychain is primary on macOS)
logs/       bounded logs; no credentials or transcript contents by default
```

- Retention follows the same seven-day default as the Docker deployment:
  expiry counts from job completion (failed/canceled included), cleanup runs
  at startup and hourly, jobs expiring while the app is closed are removed at
  next startup, active jobs are never expired, models/settings/credentials
  are never part of job retention.
- **Backup**: stop the app first (`stop.sh` — guarantees a consistent
  SQLite state), then copy the whole data directory. Keychain items are not
  in the folder; after restoring to a new machine, re-enter API keys (or
  export/import the Keychain item separately).
- **Upgrade**: stop, update the source checkout, re-run `setup.sh` (re-pins
  dependencies), start. Jobs, settings, credentials, and models persist.
- **Uninstall**: delete the repo checkout — this never deletes your job
  data. Remove `~/Library/Application Support/AudioTranscription/` and the
  `AudioTranscription` Keychain items only if you also want the data gone.
- Sleep, logout, or device loss during a session produce an explicit
  interrupted/stopped state, preserve everything already saved (the
  recording WAV is written incrementally and repaired on next startup), and
  never restart recording without user action.

## Acceptance checks (Apple Silicon)

Tracked with results in [TEST_REPORT.md](TEST_REPORT.md):

1. Fresh setup and start with Docker neither installed nor running.
2. Both modes present the same UI/workflows as the WSL2 deployment.
3. Sample transcription uses whisper.cpp with Metal; unavailable hardware
   options are disabled with explanations.
4. Microphone and computer audio each produce live transcripts and
   downloadable recordings; permission denial and device loss handled.
5. Uploads, progress, partial/final live segments, and exports flow through
   the local UI.
6. TBA LLM values leave translation unconfigured; transcription unaffected.
7. Restart, upgrade, retention expiry, backup/restore, graceful shutdown
   preserve or remove exactly the right artifacts.
8. No CUDA/NVIDIA/Docker dependencies anywhere in the native path.
