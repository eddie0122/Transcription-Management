# Deployment 1 — Windows / WSL2 with Docker Compose (NVIDIA)

This guide covers the containerized deployment using **Docker Desktop for
Windows with the WSL2 backend and Linux containers**, with Compose commands
run from an integrated WSL2 distribution. A separately installed Docker
Engine inside WSL2 is not assumed and would require its own NVIDIA runtime
setup.

> Status: this configuration is delivered per the build specification but has
> **not yet been executed on a Windows/WSL2/NVIDIA machine** — see
> [TEST_REPORT.md](TEST_REPORT.md). Run the acceptance checks below on target
> hardware before relying on it.

## Prerequisites

- Windows with a supported NVIDIA GPU and an up-to-date Windows NVIDIA
  driver with WSL2 support.
- Updated WSL2; Docker Desktop configured for the WSL2 backend, with
  integration enabled for your Linux distribution.
- Docker Compose v2 (`docker compose`).
- Disk space for images, models (up to ~3.1 GB per large model), and media;
  GPU memory for the selected model (large-v3 needs roughly 4–5 GB with
  float16); WSL2 memory configured accordingly (`.wslconfig`).
- Keep the source checkout in the WSL Linux filesystem (e.g. `~/src/...`),
  not under `/mnt/c`, for performance.

### Verify the GPU before troubleshooting the app

```bash
docker run --rm --gpus all nvidia/cuda:12.2.2-base-ubuntu22.04 nvidia-smi
```

GPU enumeration alone is insufficient — after the app is up, also run a short
transcription (below) to validate the CUDA/cuDNN dependency chain end to end.

## Services and network boundaries

| Component | Location | Responsibility |
| --- | --- | --- |
| `frontend` | container | Serves the UI, proxies `/api/` and `/ws/` to the backend |
| `backend` | container (GPU) | Media processing, inference, translation requests, job persistence, exports, retention |
| External LLM | TBA | User-selected translation model |
| Microphone capture | Windows browser | `getUserMedia` → streamed to the backend over the proxied WebSocket |
| Computer audio capture | Windows native companion | WASAPI loopback → streamed to `/ws/ingest` through the proxy |

- The UI is published at `http://localhost:8080` on **Windows loopback
  only** (`127.0.0.1:${APP_PORT}`). Do not expose it to the LAN.
- The browser uses relative same-origin `/api/` and `/ws/` URLs. Docker
  service names like `backend` are container-to-container only.
- The backend listens on `0.0.0.0:8000` **inside its container** and is not
  published to the host; the proxy and health checks reach it over the
  internal Compose network.
- Host-side capture streams through the published proxy using the per-run
  session token (`/api/session`); WebSocket endpoints reject other origins.

## First start

```bash
cp .env.example .env

docker compose -f docker-compose.yml config --quiet   # validate configuration
docker compose -f docker-compose.yml up -d --build
docker compose -f docker-compose.yml ps
docker compose -f docker-compose.yml logs --tail=100 backend
```

Open `http://localhost:8080`. In **Settings → Transcription**, confirm the
NVIDIA option is available and download a model (models go to the
`model_cache` volume; downloads are explicitly directed to
`MODEL_CACHE_DIR`). Then upload a short file and confirm the job completes —
this exercises CUDA/cuDNN, ffmpeg, and the proxy in one pass.

Stop with `docker compose -f docker-compose.yml down`.

## External LLM connectivity

The LLM connection (base URL, model ID, API key, timeout) is configured
entirely in the UI: **Settings → LLM presets**. There is no environment- or
secret-file-based LLM configuration. While a preset contains TBA values:

- No preset containing TBA values can be activated; no LLM request is made.
- Transcription remains fully usable; translation shows *awaiting
  configuration*.

API keys entered in the UI are stored in the backend's protected file store
inside the `app_data` volume (0600 permissions), never in frontend bundles or
logs. If the LLM runs on the Windows host, use
`http://host.docker.internal:PORT/v1` and
make sure the host server listens on an interface reachable from Docker
Desktop (a server bound only to Windows loopback may not be reachable) with
a narrowly scoped firewall rule. **Test from the backend container**, not
only from a Windows browser:

```bash
docker compose exec backend python -c \
  "import urllib.request;print(urllib.request.urlopen('http://host.docker.internal:PORT/v1/models',timeout=5).status)"
```

Never substitute container-local `127.0.0.1` for the host address. Use the
UI's **Test Connection**, which reports connection, authentication, model,
and response errors separately.

## Live capture on Windows

A Linux container under WSL2 cannot capture the Windows microphone or
speaker output. Capture happens on Windows and streams to the backend:

- **Microphone**: the browser captures it (permission prompt) and streams
  16 kHz PCM over the proxied `/ws/live` WebSocket.
- **Computer audio**: run the native companion on Windows:

  ```powershell
  cd companion\windows
  py -3 -m pip install -r requirements.txt
  py -3 capture_companion.py --url http://localhost:8080
  ```

  Start a Real Time session with source **Computer Audio** in the UI first;
  the session shows “Waiting for the Windows capture companion” until the
  companion connects. The companion authenticates with the per-run session
  token it fetches from `/api/session`. If the connection pauses or drops,
  already-received audio and transcript stay aligned; reconnect and
  streaming resumes. Stop the session from the UI (the companion can be
  stopped with Ctrl+C at any time).

Apple Silicon choices are disabled in this deployment: a WSL2 Linux
container has no Metal access. The macOS deployment (Section 14 / the
[macOS guide](DEPLOY_MACOS.md)) is a separate, native installation.

## Persistent data and lifecycle

- `app_data` volume: database, uploads, extracted audio, recordings,
  transcripts, translations, exports, settings, file-store credentials.
- `model_cache` volume: downloaded models only. Retention cleanup operates
  on job data in `app_data`; models survive cleanup and upgrades.
- Normal `docker compose down` and container recreation **preserve** named
  volumes. **`docker compose down -v` destroys them** — never part of
  routine stop or upgrade steps.
- Termination signals: the backend stops accepting jobs, persists finalized
  segments, flushes recordings, and marks unfinished work interrupted;
  interrupted jobs are shown on the next startup with a retry option.
- Restart policies only apply while Docker Desktop and the host run;
  retention cleanup for downtime happens at the next startup.

### Backup / restore

1. Stop the stack (`docker compose down` — no `-v`).
2. Back up both volumes together, e.g.:
   ```bash
   docker run --rm -v audio-transcription_app_data:/data -v "$PWD:/backup" \
     alpine tar czf /backup/app_data.tgz -C /data .
   ```
3. Credentials entered in the UI use a file store inside `app_data`, so the
   volume backup already includes them; keep the backup itself protected.
4. Restore by recreating the volume and untarring before `up`.

### Upgrade

1. Back up persistent data (above).
2. Fetch the intended source release.
3. `docker compose -f docker-compose.yml up -d --build`.
4. Schema migrations, if any, run automatically at backend startup; the
   release notes state rollback compatibility. Roll back by restoring the
   backup and rebuilding the previous release.

## CPU-only variant

`docker-compose.cpu.yml` is a **standalone** file (not an override):

```bash
docker compose -f docker-compose.cpu.yml up -d --build
```

No NVIDIA reservation, no NVIDIA runtime, CPU inference (`int8` by default).
Expect much slower processing; prefer `tiny`/`base` models.

## Acceptance checks (run on real Windows/WSL2/NVIDIA hardware)

1. Clean checkout builds and starts with the documented commands; UI
   reachable at `http://localhost:8080`.
2. GPU visible in the backend (`docker compose exec backend python -c
   "import ctranslate2;print(ctranslate2.get_cuda_device_count())"`), and a
   short transcription demonstrably uses CUDA.
3. With configuration supplied, the backend reaches the LLM endpoint/model;
   TBA values or LLM unavailability do not block transcription.
4. Uploads, downloads, progress events, and a sustained live WebSocket
   session work through the proxy.
5. Windows microphone (browser) and computer audio (companion) each tested
   end to end.
6. Jobs, credentials/settings, and models survive `docker compose down` +
   `up`; expiry removes job artifacts but preserves models.
7. Shutdown/restart during a session yields a clear interrupted state with
   persisted results retained.
8. Port publishing stays loopback-only; logs contain no secrets; normal
   restart/upgrade steps never remove volumes.
