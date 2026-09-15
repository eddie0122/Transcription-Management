# Product Build Instructions: Audio Transcription and Translation

## 1. Objective

Build an application that transcribes uploaded audio files, audio extracted from uploaded videos, and live speech from a microphone or computer audio output. Optionally translate the resulting text using a user-configured external LLM.

The application has two primary modes: **From Files** and **Real Time**. Users must be able to review, download, and delete results from previous jobs.

This document preserves the requested features and adds implementation recommendations and acceptance criteria. Recommendations below are proposed defaults, not additional user-approved constraints.

## 2. Recommended Architecture and Platform Scope

Use a local-first application with a frontend, a local processing service, and platform-specific audio capture adapters. A desktop shell or local companion service is recommended to support computer audio capture reliably. Transcription runs on the user's computer; translation sends transcript text to the configured LLM endpoint.

Support two required deployment cases with the same product features:

| Deployment case | Frontend and backend | Transcription | Live audio capture | Deployment instructions |
| --- | --- | --- | --- | --- |
| 1. Windows / WSL2 with Docker Compose | Containerized frontend and processing service using Docker Desktop with the WSL2 backend | faster-whisper with NVIDIA CUDA | Capture on Windows and stream to the backend | Section 13 |
| 2. macOS without Docker | Frontend and processing service run directly on macOS | Native whisper.cpp with Apple Silicon Metal acceleration | Native macOS capture adapter; browser microphone capture may also be supported | Section 14 |

Both deployment cases are required delivery targets. Docker must not be required to install, build, start, or use the macOS deployment. CPU fallback is recommended; Intel Mac support is outside the initial required hardware matrix. Share UI, job management, translation, export, and retention logic wherever practical, with platform-specific adapters for inference and capture.

Recommended components:

- Frontend: a desktop UI using a web frontend or an equivalent native implementation.
- Processing service: job queue, media decoding, transcription adapters, translation client, exports, and retention scheduler.
- Storage: local files for media and exports; SQLite or equivalent for job metadata and transcript segments.
- Communication: request/response APIs for settings and jobs, plus an event stream for job progress and live transcript updates.

Keep inference and translation off the UI thread. Use bounded queues and prioritize live transcription over queued file processing. Record the effective configuration with each job so that later setting changes do not alter existing work.

## 3. Transcription Engines and Hardware

The intended engine name is **faster-whisper**, rather than “fast-whisper.” Hardware selection must select a compatible engine; NVIDIA and Apple Silicon are not interchangeable device options within faster-whisper.

| User setting | Recommended engine | Execution |
| --- | --- | --- |
| Auto | Select a compatible installed engine | Prefer supported GPU acceleration; show the effective choice |
| NVIDIA | faster-whisper / CTranslate2 | CUDA on compatible NVIDIA hardware |
| Apple Silicon | whisper.cpp | Native macOS with Metal acceleration |
| CPU — recommended fallback | Compatible installed adapter | CPU inference with an appropriate model and precision |

**Recommendation:** retain faster-whisper for NVIDIA and use whisper.cpp for Apple Silicon. whisper.cpp documents Apple Silicon support and Metal acceleration. This is a platform-fit recommendation, not a claim that one implementation is universally faster or more accurate. Benchmark the same multilingual audio on the intended hardware before choosing default models. Sources: [faster-whisper documentation](https://github.com/SYSTRAN/faster-whisper), [whisper.cpp documentation](https://github.com/ggml-org/whisper.cpp).

Requirements:

- Detect installed engines, available devices, model availability, and compatible compute types.
- Disable unavailable hardware choices and explain how to enable them.
- Never silently change an explicitly selected backend after an error. Offer a compatible fallback.
- Present only parameters supported by the active engine. Engine-specific model files and parameter names must be handled by adapters.
- Provide model download status, disk requirements, loading status, and actionable out-of-memory errors.
- Pin and document tested dependency versions. Run the Apple Silicon inference service natively on macOS.
- Use multilingual models when the source is not English. Do not use Whisper's built-in English translation task as the external LLM translation implementation.

### Transcription controls

Expose source language (`Auto-detect` or a supported language), model, and an accuracy/speed preset. Put advanced controls in a collapsible panel:

- Beam size, temperature, and supported compute precision.
- Voice activity detection and silence thresholds.
- Initial prompt or vocabulary hints, where supported.
- Segment timestamps; optional word timestamps where supported.
- For live mode: chunk duration, overlap, and speech endpoint settings.

Provide safe defaults and short explanations. File mode may favor accuracy; live mode should favor sustainable latency. Treat live settings as tunable buffering controls, not a guarantee of instantaneous recognition.

## 4. Translation Engine

Use an external LLM configured by the user. The initial integration should support an OpenAI-compatible chat completions API.

### Development and test preset

The LLM provider and connection details are pending. Use the following placeholders until replacement values are supplied:

| Field | Value |
| --- | --- |
| Preset name | TBA |
| Base URL | `TBA` |
| Model ID | `TBA` |
| API key | `TBA` |

Treat `TBA` as an unconfigured placeholder, not a working endpoint, model ID, or credential. Do not make LLM requests until the required values are supplied. Keep transcription available and show translation as awaiting configuration. Verify endpoint compatibility and model availability after configuration; do not claim integration tests have passed before then.

The LLM may run locally or remotely; its deployment location is TBA. Configure an endpoint reachable from the translation client, including from inside Docker when applicable.

### Configuration and behavior

- Allow users to create, edit, delete, and select named LLM presets.
- Each preset includes base URL, model ID, API key, timeout, and supported generation controls.
- Include a **Test Connection** action that makes a minimal request to the selected model. Report connection, authentication, model, and response errors separately.
- Store credentials securely and redact them from logs. Keep LLM calls in the processing service.
- Translation is optional and defaults to off. When enabled, require a target language and an LLM preset.
- Keep source-language selection separate from translation target-language selection. Populate source choices from engine capabilities; label translation quality as dependent on the selected LLM.
- Translate the source transcript faithfully. Preserve names, numbers, meaning, and segment association. Do not summarize or add commentary.
- Treat transcript content as input data, not instructions to the LLM.
- Split long transcripts into context-sized units with limited surrounding context. Preserve stable segment IDs and ordering.
- Translate finalized live segments. A translation delay must not block transcription or audio capture.
- Preserve original transcription if translation fails. Show per-job or per-segment translation errors and allow retry without retranscribing.
- Bound retries and handle timeouts, rate limits, context limits, and malformed responses.
- When translation is off, send no transcript content to the LLM.

## 5. From Files Mode

### Workflow

1. The user drags files into the upload area or selects one or more files.
2. The application lists each selected file with its name, type, size, and available duration. The user can remove files before starting.
3. The user adjusts transcription settings and optionally enables translation, selects a target language, and chooses an LLM preset.
4. The user selects **Start Processing**.
5. Each file runs as an independent job with visible status and progress.
6. Completed results appear with separate **Original** and **Translated** review/download actions. Show translated results only when available.

### Media handling

- Proposed initial formats: WAV, MP3, M4A, AAC, FLAC, OGG, MP4, MOV, MKV, and WebM, subject to decoder support. Publish the tested container/codec combinations.
- Validate actual media content, not only extensions. Show configurable file-size and duration limits before processing.
- Extract audio from video without modifying the input file. If several audio tracks exist, let the user select one and display the default track.
- Report videos without audio, unreadable files, and unsupported codecs individually; continue other valid jobs.
- Normalize audio to the active engine's required format. Preserve the time relationship to the original media for timestamped exports.
- Use streamed or chunked decoding for large media instead of loading entire files into memory.

### Job progress and recovery

Use explicit stages: **Queued → Preparing Audio → Transcribing → Translating, if enabled → Exporting → Completed**. Also support **Completed with translation errors**, **Failed**, and **Canceled**.

Show per-file stage, progress, and an error message where applicable. Base transcription progress on processed media duration and translation progress on completed units. Label estimates; use an indeterminate indicator where progress cannot be measured. Do not show overall 100% until required exports are ready.

Support canceling queued or running jobs, retrying failed stages, and viewing completed portions where available. After an application restart, retain completed results and mark interrupted jobs accurately; restarting a failed stage is acceptable for the first release.

## 6. Real Time Mode

### Audio sources

Support **Microphone** and **Computer Audio** as separately selectable sources. Microphone input must include a device selector and input-level meter. Computer Audio means audio played by the computer, including speech in meetings, videos, and other applications.

Use native capture where needed. Windows WASAPI supports loopback recording, and macOS ScreenCaptureKit provides screen/audio capture capabilities. Validate capture permissions and OS compatibility on each supported release. Sources: [Microsoft loopback recording](https://learn.microsoft.com/en-us/windows/win32/coreaudio/loopback-recording), [Apple ScreenCaptureKit](https://developer.apple.com/documentation/screencapturekit).

A browser-only capture path may be offered where supported, but it must not stand in for untested system-wide capture. Show a clear unavailable state if a source cannot be captured. Simultaneous microphone and computer capture is optional for a later release; it requires mixing, alignment, and echo handling.

### Session behavior

- Pin the session controls at the top: source/device, **Start**, **Stop**, recording indicator, elapsed time, and input level.
- Check permissions, model readiness, and source availability before recording starts. Do not begin recording automatically.
- Stream captured audio into bounded buffers and process speech in overlapping chunks with voice activity detection.
- Display partial text promptly and label it as provisional. Update an existing partial segment instead of appending duplicates.
- Finalize stable segments, remove overlap duplication, and queue finalized text for translation.
- Show the newest segment at the top. Each row contains a session-relative timestamp, original text, and translated text or translation status.
- Keep a stable segment ID through partial updates, finalization, and translation. Preserve reading position when the user scrolls away from the newest text.
- Clearly show silence, device disconnection, processing delay, and translation backlog. Never silently discard audio on overload; show a delay or pause capture with a recoverable error.
- On **Stop**, stop capture, flush buffered speech, finalize the transcript, and finish or report pending translation before showing the final session result.
- Save the captured audio so the user can download it after the session. Use a recoverable recording format or incremental writes so a crash does not lose the entire recording.

Exports must be in chronological order, even though the live UI is newest-first. Use a monotonic session clock for segment timing and store the session start time separately.

## 7. UI Layout

### Shared navigation

Place **Settings** in the top-right corner. Show a prominent **From Files / Real Time** mode selector. Keep an active session's recording indicator and Stop control accessible when navigating elsewhere; switching modes must not silently stop recording or cancel jobs.

### From Files screen — top to bottom

| Section | Required contents |
| --- | --- |
| Mode selector | From Files / Real Time |
| File input | Drag-and-drop zone, Select Files, selected-file list, remove action |
| Transcription — collapsible | Language, model, accuracy/speed preset, advanced parameters |
| Translation — collapsible | Enable toggle, target language, LLM preset |
| Processing controls | Start Processing and applicable cancellation controls |
| Progress | A separate row for each file, with stage and progress indicator |
| Completed results | Filename, Original, Translated when available, download actions |

### Real Time screen — top to bottom

| Section | Required contents |
| --- | --- |
| Mode selector | From Files / Real Time |
| Pinned session controls | Source/device, Start/Stop, recording state, timer, input level |
| Transcription — collapsible | Language, model, accuracy/speed preset, advanced live parameters |
| Translation — collapsible | Enable toggle, target language, LLM preset |
| Live transcript | Newest-first timestamped rows containing original and translated text |
| Session results | Download original transcript, translated transcript, and captured audio |

Keep translation status visible when its settings panel is collapsed. Disable changes to engine, model, source, and translation configuration during a running live session; apply edited defaults to the next session. Use labeled keyboard-accessible controls and text status indicators in addition to color.

### Visual design direction

Use a clean desktop interface with a centered content area and clearly separated panels. Prioritize readable transcripts, obvious recording status, and easy access to the next action. The following styling values are recommended starting points; preserve the required layout and behavior when refining them.

| Element | Design guidance |
| --- | --- |
| Content area | Center the main workspace with a maximum width of approximately 1,200 px; use 24 px outer padding on wide screens and 16 px on narrow screens |
| Background and panels | Use a light neutral page background, white panels, subtle borders, and restrained shadows |
| Accent and status colors | Use one primary accent for selected tabs and primary actions; use clearly labeled success, warning, error, and recording states with icons or text alongside color |
| Typography | Use a system sans-serif font with multilingual fallbacks; use approximately 16 px body/transcript text, 14 px secondary labels, and 20–24 px section headings |
| Transcript readability | Use a line height of approximately 1.5–1.7, natural wrapping, and selectable text; do not truncate transcript paragraphs |
| Spacing | Use a consistent 8 px spacing scale, 16–24 px panel padding, and approximately 24 px between major sections |
| Panel styling | Use consistent rounded corners of approximately 8–12 px; keep decorative elements subordinate to content |
| Buttons | Give Start Processing and Start clear primary emphasis; keep Stop prominent throughout recording and label secondary actions explicitly |
| Collapsible panels | Use a full-width labeled header, disclosure indicator, and a brief summary of selected settings; retain values when collapsed |

### Responsive transcript and result layout

- On wide screens, display original and translated text in two labeled columns within each timestamped segment row. Keep the translation paired with its original segment, including when it is pending or has failed.
- On narrow screens, stack the original text above its translation within the same segment. Switch to this layout when the available transcript width is below approximately 768 px, or earlier if readability requires it.
- When translation is off, let the original transcript use the full available width.
- Apply the same paired layout to the completed transcript review. Keep Original and Translated downloads separate and clearly labeled.
- Preserve newest-first ordering in the live view. Place a **Return to Latest** control when the user scrolls away, and avoid moving their reading position as updates arrive.
- Allow file rows, progress rows, and action groups to wrap on small screens. Keep filenames identifiable and expose the complete name when shortened visually.
- Keep the pinned live controls visible without covering transcript text. On narrow screens, wrap source information and secondary controls while retaining an immediately accessible Stop button.

### Interaction states and accessibility

- Provide useful empty states: an upload prompt in From Files mode and a source-selection/start prompt in Real Time mode.
- Distinguish preparing the model, recording, processing buffered speech, translating, completed, and failed states through clear labels.
- Show validation errors near the affected setting or file. Preserve valid selections when another input fails.
- Use visible keyboard focus, accessible control names, and sufficient text contrast. Collapsible sections must expose their expanded state to assistive technology.
- Announce meaningful recording and error changes accessibly without announcing every partial transcript update.
- Before delivery, verify both modes at wide and narrow window sizes, including translation on/off, long multilingual text, long filenames, empty states, and errors. Confirm that controls remain usable with keyboard navigation and increased text size.

## 8. Settings and Previous Jobs

Settings must include:

1. **LLM configuration:** named presets, endpoint, model, credentials, connection test, and default preset.
2. **Transcription configuration:** hardware preference, engine/model management, default source language, and default parameters.
3. **Previous jobs:** file jobs and live sessions, dates, status, languages, storage usage, result review, downloads, and deletion.
4. **Automatic deletion:** configurable retention period, default **7 days**.

### Retention rules — proposed semantics

- Calculate expiry from the time a job reaches a terminal state; failed and canceled jobs also expire.
- Delete the application's copies of uploaded media, extracted audio, live recordings, transcripts, translations, exports, and job metadata when the job expires.
- Do not delete user-owned source files outside the application's managed storage or files already downloaded elsewhere.
- Run cleanup on startup and periodically while running. Files that expire while the application is closed are removed at the next startup; do not promise deletion while the device is off.
- Exclude active jobs from expiry cleanup. A manual delete of an active job must first cancel processing and release file handles.
- Show each job's scheduled deletion time. Retention changes apply to existing inactive jobs and future jobs; preview affected jobs before applying a shorter period.
- Manual deletion must remove associated managed artifacts and prevent workers from recreating deleted results.
- Do not delete model downloads or LLM presets as part of job retention.

## 9. Results and Exports

Required outputs:

- Original transcript: UTF-8 TXT.
- Translated transcript: UTF-8 TXT when translation was requested and produced.
- Captured live audio: a documented standard format, with WAV recommended initially.
- Download access to retained input files and existing results from previous jobs.

Recommended additional exports: SRT and VTT for timestamped subtitles, and JSON for structured segments. Translation inherits the corresponding source segment timings; it does not create new audio alignment. Validate subtitle cue order and duration.

Use distinguishable filenames such as `meeting.original.txt`, `meeting.ko.txt`, and `meeting.recording.wav`. Preserve the display filename while using collision-safe internal identifiers. Mark partial exports clearly if transcription or translation is incomplete.

## 10. Reliability and Data Handling

- Store finalized segments incrementally with job/session ID, segment ID, start/end times, original text, translation text, and processing status.
- Keep original and translated text separate. Translation failure must never overwrite the original.
- Process transcription locally after required models are installed. Explain that translation sends text to the selected provider; provider-side retention is independent of local automatic deletion.
- Bind native local services and published Docker host ports to loopback by default and restrict access from unrelated origins. Inside containers, bind services to their container network interface so the proxy and health checks can reach them. Use a local session credential where appropriate.
- Protect file operations against unsafe filenames and path traversal. Avoid logging audio, full transcripts, or API keys by default.
- Handle unavailable models, insufficient memory, insufficient disk space, permission denial, device loss, and LLM failures with actionable messages.

## 11. Acceptance Criteria

The implementation is complete when the following are demonstrated on the supported platform matrix:

1. Multiple audio/video files can be queued, transcribed, reviewed, and downloaded independently.
2. A corrupt file or video without audio fails clearly without stopping other jobs.
3. Translation can be toggled off with no LLM content requests; when on, original and translated outputs remain separately available.
4. Once supplied, the development LLM preset can be saved and tested. While values remain TBA or the endpoint is unavailable, report the integration test as blocked rather than claiming success from a mock.
5. NVIDIA and Apple Silicon selections use their compatible accelerated adapters on actual hardware. Unsupported choices display actionable explanations.
6. Microphone speech and computer playback are each captured and transcribed on every supported platform, including permission-denial and device-loss cases.
7. Partial live text updates without duplicate committed segments; the UI is newest-first and exports are chronological.
8. Stopping a live session preserves final buffered speech and produces downloadable text and playable audio.
9. LLM timeouts preserve transcription and allow translation-only retry.
10. Cancellation, restart recovery, manual deletion, and seven-day expiry behave according to this specification. Verify expiry using controlled timestamps.
11. A sustained live-session test confirms that buffers remain bounded and processing delay is visible. Recommended initial target: first partial text within 2 seconds and finalized text within 5 seconds after a speech boundary on a documented reference machine/model; these are benchmark targets, not universal guarantees. Measure translation latency separately.
12. Evaluate transcription using reference transcripts in representative languages and noisy/silent samples. Report WER or CER as appropriate, timing, memory use, and known limitations. Do not claim a universal accuracy percentage.

## 12. Implementation Sequence and Deliverables

Recommended sequence:

1. Confirm the supported OS/hardware matrix; validate native capture and both inference adapters with small prototypes.
2. Implement persistent file jobs, transcription, TXT export, and progress reporting.
3. Add LLM presets, connection testing, translation, and translation-only retry.
4. Add live capture, partial/final segment handling, audio recording, and session exports.
5. Complete job history, retention cleanup, recovery, packaging, and platform acceptance tests.

Deliver source code, reproducible setup instructions for both required deployment cases, pinned dependencies, configuration examples, model installation guidance, packaging/run instructions, and a test report distinguishing verified behavior from untested hardware or blocked integrations. Include separate WSL2/Docker and native macOS deployment guides.

Speaker diarization, speaker identification, meeting bots, live audio translation playback, and cloud multi-user hosting are outside the initial scope unless requested separately.


## 13. Docker Compose Deployment on WSL2

### Required deployment model

Deliver a reproducible Docker Compose deployment for the frontend and backend. Use **Docker Desktop for Windows with its WSL2 backend and Linux containers**, with Compose commands run from an integrated WSL2 distribution. This is the primary supported Docker installation path; a separately installed Docker Engine inside WSL2 requires its own documented NVIDIA runtime setup and is not assumed by this configuration.

Docker documents NVIDIA GPU support for its Windows WSL2 backend. Compose can request GPU devices using device reservations. Sources: [Docker Desktop GPU support](https://docs.docker.com/desktop/features/gpu/), [Compose GPU configuration](https://docs.docker.com/compose/how-tos/gpu-support/).

Prerequisites:

- Windows with a supported NVIDIA GPU and an up-to-date Windows NVIDIA driver supporting WSL2.
- An updated WSL2 installation, Docker Desktop configured for the WSL2 backend, and integration enabled for the selected Linux distribution.
- Docker Compose v2, invoked as `docker compose`.
- Sufficient GPU memory for the selected model, disk space for images/models/media, and configured WSL2 memory capacity. Document reference-machine requirements after measurement.
- Verify GPU visibility inside a container before troubleshooting the application. GPU enumeration alone is insufficient: also run a short faster-whisper transcription to validate the CUDA/cuDNN dependencies.

### Services and network boundaries

| Component | Location | Responsibility |
| --- | --- | --- |
| Frontend / reverse proxy | Docker container | Serve the UI and proxy `/api/` and `/ws/` to the backend |
| Backend | Docker container with GPU access | Media processing, inference, translation requests, job persistence, exports, and retention cleanup |
| External LLM — provider TBA | Deployment location TBA | Serve the user-selected translation model |
| Microphone capture | Windows browser or native companion | Capture permissioned microphone input and stream it to the backend |
| Computer audio capture | Windows native companion | Capture WASAPI loopback audio and stream it to the backend |

Serve the frontend at `http://localhost:8080` by default. Use relative same-origin API and WebSocket URLs in the browser; Docker service names such as `backend` are for container-to-container communication, not browser URLs. Configure the proxy to support WebSocket upgrades, long-lived sessions, streamed responses, and the application's documented upload limits.

Publish only the frontend port to Windows loopback. The backend listens on `0.0.0.0:8000` inside its container and remains reachable through the internal Compose network. Host-side capture streams through the published proxy using an authenticated session. Do not publish the backend port or expose the application to the LAN by default.

### External LLM connectivity from Docker

The Docker LLM configuration is also pending. Replace these placeholders with values reachable from the backend container before enabling translation:

| Field | Docker deployment value |
| --- | --- |
| Base URL | `TBA` |
| Model ID | `TBA` |
| API key | `TBA` |

Docker Desktop provides `host.docker.internal` for reaching services on the host. Source: [Docker Desktop networking](https://docs.docker.com/desktop/features/networking/).

If the LLM runs on the Windows host, the host LLM server must listen on an interface reachable from Docker Desktop; a server bound only to Windows loopback may not be reachable through this hostname. Configure its bind address and Windows firewall access narrowly for the required connection. Test connectivity from the backend container, not only from a Windows browser. Do not substitute container-local `127.0.0.1` for the host address.

The application must remain usable for transcription while the LLM is unconfigured or unavailable. Do not activate a preset containing TBA values. Once valid environment values are supplied, treat them as first-run preset defaults; persist user edits and do not overwrite them on every container restart.

### Compose configuration contract

Include the following structure in the delivered `docker-compose.yml`. This is a **build specification example**, not a deployable application supplied with this document: the referenced Dockerfiles, proxy configuration, application entrypoint, and health endpoint must be implemented and tested with it.

```yaml
name: audio-transcription

services:
  frontend:
    build:
      context: ./frontend
      dockerfile: Dockerfile
    ports:
      - "127.0.0.1:${APP_PORT:-8080}:80"
    depends_on:
      backend:
        condition: service_healthy
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

  backend:
    build:
      context: ./backend
      dockerfile: Dockerfile
    environment:
      APP_HOST: "0.0.0.0"
      APP_PORT: "8000"
      DATA_DIR: /app/data
      MODEL_CACHE_DIR: /app/models
      TRANSCRIPTION_ENGINE: faster-whisper
      TRANSCRIPTION_DEVICE: cuda
      RETENTION_DAYS: "${RETENTION_DAYS:-7}"
      LLM_BASE_URL: "${LLM_BASE_URL:-TBA}"
      LLM_MODEL: "${LLM_MODEL:-TBA}"
      LLM_API_KEY_FILE: /run/secrets/llm_api_key
    secrets:
      - llm_api_key
    volumes:
      - app_data:/app/data
      - model_cache:/app/models
    expose:
      - "8000"
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"
      interval: 15s
      timeout: 5s
      retries: 5
      start_period: 60s
    stop_grace_period: 60s
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

volumes:
  app_data:
  model_cache:

secrets:
  llm_api_key:
    file: ./secrets/llm_api_key.txt
```

Implementation requirements for this contract:

- The backend image must include pinned, mutually compatible faster-whisper, CTranslate2, CUDA runtime, cuDNN, and media decoding dependencies. Use a tested CUDA runtime base and pin release images by tag or digest; never rely on `latest` for a release.
- The image must provide `python`, launch the backend on the configured host/port, and implement `/api/health`. Health indicates service/database availability without requiring an LLM connection or a model download to finish. Report model readiness separately in the UI.
- The frontend image must contain the built UI and reverse-proxy configuration targeting `backend:8000`. Preserve the `/api/` and `/ws/` route contracts when forwarding requests.
- Implement the named environment variables and secret-file loading. Do not assume that specifying an environment variable automatically configures the inference library's cache; explicitly direct model downloads to `MODEL_CACHE_DIR`.
- Run application processes as non-root where practical and initialize volume ownership accordingly. Use a single backend instance initially for SQLite, the job scheduler, and retention cleanup.
- Compose secrets mount the supplied file; protect the host file and exclude it from source control. Store later user-entered API keys using protected persistent credentials, never in frontend bundles or application logs.
- Provide a separate tested `docker-compose.cpu.yml` if CPU deployment is offered. It must work independently, with no NVIDIA reservation or required NVIDIA runtime. Do not assume merging an override automatically removes GPU reservations.

### Persistent data and lifecycle

Use `app_data` for the database, uploaded media, extracted audio, recordings, transcripts, translations, exports, and persisted settings. Use `model_cache` exclusively for downloaded models. Application-managed seven-day cleanup must operate on job data inside `app_data`; model files remain available across cleanup and upgrades.

Normal container recreation and `docker compose down` must preserve named volumes. Document that `docker compose down -v` destroys these volumes and must never be part of routine stop or upgrade instructions. Provide backup and restore instructions that stop writes or use a database-consistent snapshot, and back up the data and any credential-encryption key together.

Handle termination signals: stop accepting jobs, persist finalized segments, flush recordings where possible, and mark unfinished work as interrupted before shutdown. Recover interrupted jobs visibly on startup. Restart policies apply only while Docker Desktop and the host are running; retention resumes after downtime.

### WSL2 setup and operation guide to deliver

Store the source checkout in the WSL Linux filesystem where practical. Deliver a `.env.example` containing nonsecret deployment defaults and instructions to create the local secret file. Example commands, once application source and Dockerfiles exist:

```bash
cp .env.example .env
mkdir -p secrets
printf '%s' 'TBA' > secrets/llm_api_key.txt
chmod 600 secrets/llm_api_key.txt

docker compose -f docker-compose.yml config --quiet
docker compose -f docker-compose.yml up -d --build
docker compose -f docker-compose.yml ps
docker compose -f docker-compose.yml logs --tail=100 backend
```

The secret-file command above creates a TBA placeholder only; replace it with the actual credential before enabling translation. Open `http://localhost:8080`, verify the selected GPU/model, and run the LLM connection test after supplying the pending configuration. Stop with `docker compose -f docker-compose.yml down`. For an upgrade, back up persistent data, obtain the intended source release, and rebuild with `up -d --build`; document migrations and rollback compatibility.

### Live capture and Apple Silicon boundaries

Do not assume that a Linux container under WSL2 can directly capture the Windows microphone or Windows speaker output. Capture on Windows and stream timestamped audio to the container. Browser microphone capture may serve the microphone workflow; implement the native Windows companion for the required computer-audio workflow. Include companion installation, pairing, permissions, reconnect behavior, and recording-stop handling in the deployment guide. Keep captured audio and transcript timelines aligned if the connection pauses or drops.

Apple Silicon selection must not imply Metal access in the WSL2 Linux container. The second deployment case runs the entire application directly on macOS without Docker, as specified in Section 14. Disable unavailable Apple Silicon choices in the WSL2 deployment. Hardware choices reflect reachable, verified inference workers.

### Docker deliverables and acceptance checks

Add these files to the implementation deliverables: `docker-compose.yml`, frontend and backend Dockerfiles, proxy configuration, `.dockerignore` files, `.env.example`, secret setup instructions, and a WSL2 deployment README. Include host capture companion setup and the optional CPU Compose file where implemented.

Verify the following on an actual Windows/WSL2/NVIDIA machine:

1. A clean checkout builds and starts using the documented Compose command; the UI is reachable through localhost.
2. The GPU is visible in the backend, and a short transcription demonstrably uses CUDA.
3. After configuration is supplied, the backend reaches the selected LLM endpoint and model; pending TBA values or LLM unavailability do not block transcription.
4. File uploads, downloads, progress events, and a sustained live WebSocket session work through the proxy.
5. Windows microphone and computer audio are each tested end to end through their supported capture paths.
6. Jobs, credentials/settings, and models survive container recreation; expiry removes job artifacts but preserves models.
7. Shutdown/restart during a session produces a clear interrupted state and retains already persisted results.
8. Port publishing remains local-only, logs omit secrets, and normal restart/upgrade steps do not remove volumes.

Report Docker configuration checks separately from runtime verification. This document supplies deployment requirements and an example configuration; it does not claim that images have been built or that WSL2/GPU deployment has been tested.


## 14. Native macOS Deployment Without Docker

### Required deployment model

Run the frontend, backend, inference engine, and audio capture integration directly on macOS. Target Apple Silicon for the initial release. No Docker installation, container image, Docker Compose command, or containerized helper may be required for this deployment case.

Reuse the shared frontend and backend code from the WSL2 deployment where practical. Isolate CUDA-specific dependencies so the native macOS installation does not attempt to install or load them. Use a native whisper.cpp adapter for Apple Silicon inference and a native macOS audio capture adapter.

### Components and startup

| Component | Native macOS requirement |
| --- | --- |
| Frontend | Build the shared UI locally or ship prebuilt assets; serve them from the local backend or package them in a desktop shell |
| Backend | Run in an isolated native runtime environment, with pinned dependencies and persistent local storage |
| Inference | Install or bundle an arm64 whisper.cpp build and verify Metal acceleration with a sample transcription |
| Microphone | Request microphone permission and capture through the supported native or browser path |
| Computer audio | Integrate the native macOS capture adapter and document required capture permissions |
| External LLM | Provider, endpoint, model, and credentials remain TBA; configure a reachable local or remote endpoint later |

Recommended default: serve the built frontend and API from one local origin at `http://localhost:8080`, with relative `/api/` and `/ws/` routes. Bind native services to loopback. A desktop shell may wrap the same UI, provided equivalent routing, access controls, and lifecycle behavior are implemented.

Provide a single documented start command or launcher that starts the required native services, reports readiness, and opens the UI. Detect occupied ports and explain how to change the port consistently. Stop must release capture devices, flush recordings and finalized segments, and terminate owned child processes cleanly. Prevent accidental duplicate backend instances from opening the same job store.

### Installation and configuration deliverables

Deliver a native macOS setup guide and scripts with the following proposed interface. These are required implementation deliverables, not scripts already supplied with this specification:

```bash
./scripts/macos/setup.sh
./scripts/macos/start.sh
./scripts/macos/stop.sh
```

- `setup.sh`: check supported macOS version and arm64 architecture; install or verify pinned dependencies, prepare an isolated backend environment, build or locate frontend assets and the native inference/capture components, and initialize writable storage.
- `start.sh`: load native configuration, start the application and required adapters, verify service readiness, and print the local UI address. A desktop launcher can provide the same behavior.
- `stop.sh`: request graceful shutdown without deleting jobs, settings, credentials, or model downloads.
- Document the tested minimum macOS version, required build tools when building from source, media decoder dependencies, and microphone/computer-audio permission steps. Choose the minimum OS version based on the implemented capture APIs and actual validation.
- Provide a native configuration example separate from Docker configuration. It must select the Apple Silicon adapter, native data/model paths, local listen address/port, and seven-day retention default.
- Keep LLM provider, base URL, model ID, and API key values as `TBA`. Translation remains awaiting configuration until actual values are entered; transcription must work independently.
- Store user-entered credentials in macOS Keychain or an equivalent protected native credential store. Do not require Docker secret paths such as `/run/secrets/llm_api_key`.

### Storage and maintenance

Use a persistent user-owned application directory. Recommended default: `~/Library/Application Support/AudioTranscription/`, with distinct subdirectories for job data, models, and configuration. Treat the application name as a proposed default and keep the resolved location visible in the deployment guide.

Apply the same seven-day retention rules as the Docker deployment. Preserve models, settings, and credentials across normal application restarts and updates. Store temporary media in a managed location and clean it after job termination or expiry. Logs must be bounded and omit credentials and transcript contents by default.

Document backup, restore, upgrade, and uninstall behavior. Back up the database consistently with its associated files; explain how separately protected credentials are restored or re-entered. Uninstalling application binaries must not silently delete user job data. Sleep, logout, and device loss must produce an explicit stopped/interrupted session state, preserve saved results, and never restart recording without user action.

### Native macOS acceptance checks

Verify on an actual Apple Silicon Mac:

1. A fresh setup builds or installs and starts successfully without Docker installed or running.
2. Both From Files and Real Time modes present the same required UI and workflows as the WSL2 deployment.
3. A sample transcription uses the native whisper.cpp adapter with Metal acceleration; unavailable hardware choices are disabled with an explanation.
4. Microphone and computer audio each produce live transcripts and downloadable recordings; permission denial and device loss are handled clearly.
5. The shared API/event flow supports uploads, progress updates, partial/final live segments, and exports through the local UI.
6. TBA LLM values leave translation unconfigured while transcription remains usable. After real values are supplied, test translation separately and report blocked integration tests honestly.
7. Restart, upgrade, retention expiry, backup/restore, and graceful shutdown preserve or remove the correct artifacts.
8. The native setup has no dependency on CUDA, NVIDIA runtime components, Docker networking hostnames, Docker volumes, or Docker secrets.

Maintain a test matrix with separate results for **Windows + WSL2 + Docker Compose + NVIDIA** and **macOS native + Apple Silicon**. Passing one deployment case does not establish that the other is verified.
