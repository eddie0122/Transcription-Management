# Audio Transcription

A local-first application that transcribes uploaded audio files, audio
extracted from video files, and live speech from a microphone or computer
audio output — and optionally translates the transcript with a
user-configured external LLM.

Transcription runs entirely on your own machine (Whisper-family models);
nothing leaves the machine unless you explicitly enable translation, in which
case only transcript text is sent to the LLM endpoint you configured.

## Feature overview

- **From Files** — batch-transcribe audio/video files (WAV, MP3, M4A, AAC,
  FLAC, OGG, MP4, MOV, MKV, WebM, decoder-dependent) with per-file progress,
  cancel, and retry.
- **Real Time** — live transcription from a microphone or computer audio,
  with provisional partial text, finalized segments, and a recoverable WAV
  recording of the session.
- **Talkie** — a two-way live interpreter for calls: what you say into the
  microphone is transcribed, translated, displayed, and spoken in the other
  person's language; what they say in the call (computer audio) comes back
  to you the same way. Both directions run at once, with a half-duplex
  pause while the product itself is speaking.
- **Translation (optional, off by default)** — segment-by-segment
  translation through any OpenAI-compatible chat completions endpoint,
  configured in the UI. Original text is never overwritten; failed segments
  can be retried without retranscribing.
- **Exports** — plain text (TXT), subtitles (SRT, VTT), and structured JSON,
  for the original and (when produced) the translation. Talkie exports carry
  speaker prefixes and keep each speaker's cue timings (speakers may
  overlap).
- **Previous Jobs** — review, download, and delete any past job or live
  session from its own tab.
- **Automatic deletion** — every job's stored data expires after a
  configurable retention period (default 7 days).

## Supported environments

| Deployment | Transcription engine | Acceleration |
| --- | --- | --- |
| Windows / WSL2 with Docker Compose | faster-whisper (CTranslate2) | NVIDIA CUDA |
| Native macOS (no Docker) | whisper.cpp | Apple Silicon Metal |
| Either | the installed engine | CPU fallback |

The UI, job management, translation, exports, and retention behave the same
in both deployments; only the inference and audio-capture adapters differ.

## The UI

The header has four mode tabs plus Settings:

- **From Files** — the file workflow (below).
- **Real Time** — live session controls, live transcript, session results.
- **Talkie** — language pair, engine/translation and voice-output settings,
  two "now speaking" banners (to them / to me), and the conversation view.
- **Previous Jobs** — every past file job and live session with status,
  language, date, storage use, download buttons, review, translation retry,
  and deletion. The summary line shows total managed storage.
- **⚙ Settings** (top right) — a dialog with three sections:
  - **LLM presets** — create/edit/test/delete named LLM connections
    (base URL, model ID, API key, timeout, generation params). This is the
    only place LLM connection details are configured — there is no
    environment- or file-based LLM configuration.
  - **Transcription** — hardware preference, model download/delete
    management, and default source language / default LLM preset.
  - **Automatic deletion** — the retention period. Shortening it previews
    the jobs that would be deleted before applying.

While a live or Talkie session is running, a persistent recording chip with
a Stop button stays visible in the header from every tab; switching tabs
never stops a session or cancels jobs. One live-type session (Real Time or
Talkie) runs at a time.

## How to use

### Transcribe files

1. On **From Files**, drag files onto the drop zone or click **Select
   Files**. Each file is listed with type, size, and duration; remove any
   before starting.
2. Adjust the **Transcription** panel (language, model, accuracy preset,
   hardware) and optionally enable **Translation** (target language + LLM
   preset required).
3. Click **Start Processing**. Each file becomes an independent job with
   visible stages (Preparing Audio → Transcribing → Translating →
   Exporting) and a cancel button.
4. Finished jobs appear under **Completed results** with the default
   downloads — **original TXT and SRT** (plus translated TXT and SRT when
   translation ran). Click **Review** to read the transcript
   segment-by-segment and download every format (TXT, SRT, VTT, JSON,
   retained input).

A failed file (corrupt media, video without audio, unsupported codec) fails
individually with a clear error and a Retry button; other jobs continue.

### Live transcription

1. On **Real Time**, pick a source — **Microphone** (with device selector
   and level meter) or **Computer Audio** (native capture on macOS; on
   Windows run the capture companion, see the WSL2 guide).
2. Click **Start**. Recording never starts on its own, and the first use
   triggers the OS permission prompt.
3. Provisional text appears within seconds and firms up as segments
   finalize; the view is newest-first (exports are chronological). Finalized
   segments queue for translation when enabled — translation delays never
   block transcription.
4. Click **Stop**. Buffered speech is flushed, the transcript is finalized,
   and the session results offer the transcript exports plus the recorded
   WAV. The session also appears in **Previous Jobs**.

### Interpret a call (Talkie)

The scenario: you (mother tongue = *my language*) talk with someone whose
mother tongue is *their language* over Zoom or a similar app.

1. On **Talkie**, pick **My language** and **Their language**, your
   microphone, and (under *Engine & translation*) a configured LLM preset —
   translation is mandatory in this mode, so the session refuses to start
   without one.
2. Under **Voice output** choose the voice engine and, for system voices,
   the output device for each direction (see *Routing into a call* below).
   **Test** buttons play a sample sentence through the same path a session
   uses.
3. Click **Start**. Wear headphones so the microphone does not hear the call.
4. When you speak, the utterance is transcribed in your language, translated
   to theirs, shown in the **To them** banner and spoken by the chosen voice.
5. When they speak, the call's playback (computer audio) is transcribed in
   their language, translated to yours, shown in the **To me** banner and
   spoken to you.
6. **Pause listening while speaking** (default on) mutes speech detection on
   both channels while the product talks, so computer-audio capture never
   re-interprets the product's own voice. Speech during that moment is not
   transcribed; this is the walkie-talkie trade-off. Turn it off only with
   headphones and a virtual output device that keeps the product's voice out
   of the capture path.
7. **Stop** finalizes the conversation; exports carry `Me:`/`Them:` prefixes
   and the session keeps one recording per speaker. Replay buttons on each
   row and banner re-speak a translation.

Utterance endpointing is silence-based (700 ms by default in Talkie);
adjust it under *Advanced parameters* if translations start too early or
too late. Every finalized utterance is translated on its own for latency.

#### Voice engines

| Engine | Where | Output routing |
| --- | --- | --- |
| Browser voice | any deployment; voices installed in the browser/OS | default output only |
| macOS system voice | native macOS (`say`) | per direction |
| Windows voice | Docker/WSL2, via the Windows capture companion (SAPI) | per direction |

Per-direction routing uses the browser's output-device selection
(`setSinkId`), available in Chrome and Edge; other browsers show the
selectors hidden with an explanation. Output devices appear after the
microphone permission is granted. Voice preferences (engine, voices, output
devices) are stored in the browser.

#### Routing into a call

For the other person to *hear* the translation, the "to them" voice must
reach the meeting app's microphone input:

- Install a virtual audio device (macOS: e.g. BlackHole; Windows: e.g. VB-CABLE).
- In Talkie → Voice output, set **Output for "to them"** to that virtual
  device and **Output for "to me"** to your headphones.
- In the meeting app, select the virtual device as the microphone (or an
  aggregate/mixed device if the other side should also hear your own
  voice). Keep the meeting app's speaker on your headphones; computer-audio
  capture still captures it.
- Without a virtual device, the meeting app's "share computer sound" option
  sends everything the computer plays to the other side, including the
  translations meant for you.

Talkie always sends utterance text to the configured LLM. System voices run
locally (`say` or the companion on the Windows host); browser voices may be
local or online depending on the browser and voice chosen.

### Configure translation

Open **Settings → LLM presets** and create a preset with your endpoint's
base URL (e.g. `http://host:port/v1`), model ID, API key, and timeout, then
use **Test Connection** — it reports connection, authentication, model, and
response errors separately. Presets still containing `TBA` placeholder
values cannot be activated and are never contacted. API keys are stored in
the macOS Keychain (native) or a 0600-permission file store inside the data
volume (Docker) — never in the frontend, logs, or exports.

## Setting parameters

### Accuracy / speed presets

Every preset carries anti-repetition safeguards (temperature-fallback
ladder, no cross-window text conditioning, voice activity detection); they
differ in decoding search width:

| Preset | beam_size / best_of | Intended use |
| --- | --- | --- |
| Fast | 1 | Live sessions, quick drafts |
| Balanced | 3 | General use |
| Accuracy | 5 | File jobs (default in file mode) |

### Advanced parameters (From Files → Transcription → Advanced)

Any value set here overrides its preset counterpart:

- **Beam size**, **Temperature** — decoding search width and sampling
  temperature (0 = deterministic start of the fallback ladder).
- **Compute precision** — faster-whisper only: `auto`, `float16`,
  `int8_float16`, `int8`, `float32`.
- **Voice activity detection** — skips silence before decoding
  (faster-whisper; whisper.cpp handles silence internally).
- **Word timestamps** — where supported by the active engine.
- **Audio track** — track index for multi-track video.
- **Initial prompt / vocabulary hints** — names, jargon, spellings.
- **Extra engine parameters** — full access to the active engine:
  - *faster-whisper (NVIDIA/CPU in Docker)*: one `key=value` per line; any
    parameter of the installed library's `transcribe()` is accepted, e.g.
    `repetition_penalty=1.15`, `no_repeat_ngram_size=3`,
    `hallucination_silence_threshold=2`, `patience=2`. Values parse as JSON
    where possible; a bare key means `true`. Unknown names fail the job
    with the full supported list.
  - *whisper.cpp (macOS)*: raw `whisper-cli` flags appended to the
    command, e.g. `-et 2.8 --suppress-nst` (see `whisper-cli -h`). Managed
    input/model/output flags cannot be overridden.

### Live parameters (Real Time → Transcription → Advanced)

Buffering controls, not latency guarantees: max chunk duration, speech
endpoint silence, and partial update interval.

## Deployment

### Windows / WSL2 with Docker Compose (NVIDIA)

Full guide: [docs/DEPLOY_WSL2_DOCKER.md](docs/DEPLOY_WSL2_DOCKER.md).

```bash
cp .env.example .env      # port, retention, proxy body size — no secrets
docker compose -f docker-compose.yml up -d --build
```

Open `http://localhost:8080` (published on Windows loopback only), download
a model in Settings → Transcription, and upload a short file to validate the
CUDA chain. A standalone CPU variant exists as `docker-compose.cpu.yml`.
Computer-audio capture and Windows voices (for Talkie) use the Windows
companion (`companion/windows/capture_companion.py`).

`.env` values: `APP_PORT` (default 8080), `RETENTION_DAYS` (default 7),
`MAX_BODY_SIZE` (default 2g). LLM connections are configured in the UI, not
in `.env`.

### Native macOS (Apple Silicon, no Docker)

Full guide: [docs/DEPLOY_MACOS.md](docs/DEPLOY_MACOS.md).

```bash
./scripts/macos/setup.sh    # verifies arm64/macOS 13+, builds whisper.cpp + capture helper
./scripts/macos/start.sh    # starts the backend and prints the UI address
./scripts/macos/stop.sh     # graceful shutdown; deletes nothing
```

Native configuration lives in `scripts/macos/native.env` (from
`native.env.example`); data in
`~/Library/Application Support/AudioTranscription/`. Computer-audio capture
uses ScreenCaptureKit and requires the *Screen & System Audio Recording*
permission on first use.

### Backend environment variables (both deployments)

`APP_HOST`, `APP_PORT`, `DATA_DIR`, `MODEL_CACHE_DIR`,
`TRANSCRIPTION_ENGINE`, `TRANSCRIPTION_DEVICE`, `WHISPER_CPP_BIN`,
`RETENTION_DAYS`, `MAX_UPLOAD_MB` (default 2048), `MAX_DURATION_MIN`
(default 300), `MAX_QUEUED_JOBS` (default 100), `FRONTEND_DIST`,
`MACOS_CAPTURE_BIN`. All are optional with sensible per-platform defaults.

## Data handling

- All job data (uploads, extracted audio, recordings, transcripts,
  translations, exports) lives under the application's managed data
  directory and is deleted when the job expires or is deleted manually.
  Model downloads and LLM presets are never deleted by retention.
- Services bind to loopback by default; the Docker deployment publishes only
  the frontend port on `127.0.0.1`. Live-audio ingest requires a per-run
  session token.
- When translation is off, no transcript content is sent anywhere.
  Provider-side retention of translated text is independent of local
  automatic deletion. Talkie sessions always translate, so every utterance
  of both speakers is sent to the selected LLM preset.

## Verification status

See [docs/TEST_REPORT.md](docs/TEST_REPORT.md) for what has been verified on
which hardware, and what remains untested.
