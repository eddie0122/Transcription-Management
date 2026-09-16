# Test Report

Date: 2026-09-15 · Application version 0.1.0

This report distinguishes **verified behavior** from **untested hardware**
and **blocked integrations**, per the delivery requirements. Passing one
deployment case does not establish that the other is verified.

## Test matrix summary

| Deployment case | Status |
| --- | --- |
| macOS native + Apple Silicon | **Verified** on the reference machine below (details per area; UI checks partially verified — see §6) |
| Windows + WSL2 + Docker Compose + NVIDIA | **Not verified** — configuration delivered and validated for structure only; no Windows/NVIDIA hardware was available. Run the acceptance checks in [DEPLOY_WSL2_DOCKER.md](DEPLOY_WSL2_DOCKER.md). |
| External LLM integration (real endpoint) | **Blocked** — provider, base URL, model ID, and API key remain TBA. The translation client was exercised against a local mock only (see §4); this is explicitly **not** claimed as a passed integration test. |
| Talkie mode (two-way interpreter), backend | **Verified** on the reference machine via scripted clients (§8); browser UI, real call routing, and Windows voices **not verified** |

## Reference machine

- Apple M2, 16 GB RAM, macOS 15.6 (arm64)
- Python 3.9.6 (system), whisper.cpp v1.7.6 built from source with Metal
- Engine binary: `native/whisper.cpp/build/bin/whisper-cli`
- Model: `ggml-base.bin` (148 MB, multilingual), downloaded through the
  application's own model-download API
- Media decoder: CoreAudio fallback (afconvert) — ffmpeg was not installed
  on the test machine, so the ffmpeg paths (video containers, audio-track
  selection) are **untested** here

## 1. From Files mode — verified

- Upload of multiple files in one request; each file becomes an independent
  job with its own status/progress; results reviewed and downloaded via API
  (acceptance 1).
- A corrupt file (garbage bytes named `.mp3`) failed individually with an
  actionable decoder message while the valid file in the same batch
  completed (acceptance 2).
- Full pipeline stages observed: Queued → Preparing Audio → Transcribing →
  (Translating) → Exporting → Completed, with per-stage progress labels.
- Exports produced and validated: `*.original.txt`, `.srt`, `.vtt`,
  `.segments.json`; translated variants when translation ran; download
  filenames use the display name, storage uses collision-safe job IDs.
- Explicit source language (`en`) honored; auto-detect exercised (the
  Korean-voiced synthetic speech was detected as Korean and transcribed
  phonetically — correct engine behavior for accented input).
- Cancellation of a running job → `canceled`, subprocess terminated
  (acceptance 10, partial).
- Manual deletion removed rows and all managed files; subsequent GET → 404;
  workers cannot recreate deleted results (rows deleted before files).
- Restart recovery: killing the backend mid-job produced `interrupted` with
  a clear message at next startup; completed results were retained
  (acceptance 10, partial).

## 2. Real Time mode — verified via WebSocket client

A scripted client streamed 16 kHz PCM at real-time pacing over `/ws/live`
(the same protocol the browser uses):

- Partial text updated a **stable segment ID** in place (13 partial updates
  across 2 segments; zero duplicate committed segments) (acceptance 7).
- First partial arrived ~1.5 s after speech started; partials refreshed at
  the configured 1.0 s interval; finalization followed the 600 ms endpoint
  silence within ~0.5 s inference time — within the recommended 2 s / 5 s
  benchmark targets **on this machine/model**; these are not universal
  guarantees.
- Forced segment split at the 10 s max-chunk boundary worked.
- Stop flushed buffered speech, finalized the transcript, and returned
  downloadable artifacts; the recorded WAV was playable with the correct
  duration (13.48 s for 13.48 s of received audio) (acceptance 8).
- Live UI order is newest-first (client-side); exports are chronological
  (verified in `original.txt` ordering) (acceptance 7).
- Companion ingest (`/ws/ingest`): PCM streamed on a second authenticated
  socket fed the active session; wrong token rejected with HTTP 403.
- Overload behavior: streaming at ~12× real time stayed within the bounded
  queue on this machine; the backlog-warning and pause-capture paths exist
  but were **not** driven to their thresholds — sustained-load soak testing
  remains open (acceptance 11 partially verified: buffers bounded by
  design, delay surfaced via backlog events; long-duration soak untested).
- Microphone capture through a real browser (getUserMedia → worklet →
  WebSocket) and the ScreenCaptureKit computer-audio helper were **not**
  end-to-end tested in this environment (no interactive browser/permission
  grant available). The helper builds and, when permission is denied,
  reports a clear actionable error (exit 2) which the backend surfaces —
  the permission-denial path of acceptance 6 is verified; the
  permission-granted capture path is untested pending an interactive run.

## 3. Engines and hardware — verified on Apple Silicon only

- whisper.cpp adapter with Metal ran on real hardware; effective engine is
  recorded per job (“whisper.cpp (Metal, Apple Silicon)”) (acceptance 5,
  macOS half).
- Unavailable choices (NVIDIA, and Apple/CPU before install) are reported
  disabled with actionable how-to-enable text; an explicitly selected
  unavailable backend fails with a message plus a suggested compatible
  fallback — never a silent substitution.
- Model management: download with progress events, disk-space check,
  `.part` staging, delete; model files live outside job retention.
- faster-whisper/CUDA on NVIDIA is **untested** (no hardware); the adapter
  code paths (load errors, OOM messaging, segment streaming) are delivered
  but unverified (acceptance 5, NVIDIA half: open).
- Performance (base model, beam 5, Metal): 149.3 s of audio transcribed in
  7.5 s (~20× real time), peak RSS ≈ 350 MB.

## 4. Translation — client verified against a mock; real integration blocked

- TBA guard: presets with TBA values report `configured: false`; Test
  Connection returns `unconfigured` **without making any request**; jobs
  with such a preset keep the transcription and mark translation “awaiting
  configuration” (acceptance 4: reported as **blocked**, not passed).
- With translation **off**, no LLM request of any kind is made (verified by
  the mock receiving zero requests for non-translating jobs) (acceptance 3).
- Against a local OpenAI-compatible mock: connection/auth/model errors
  reported separately (401 → auth; refused connection → connection);
  batch translation preserved segment IDs and pairing; original and
  translated outputs remained separately downloadable.
- LLM unreachable mid-job → `completed with translation errors`; original
  transcription preserved per segment; **Retry translation** re-ran only
  translation and upgraded the job to `completed` (acceptance 9 —
  connection-failure variant; real timeout behavior against a slow real
  endpoint remains untested).
- API keys: stored in macOS Keychain (verified store/load/delete via the
  `security` CLI), redacted from API responses; only opaque references in
  the database.

## 5. Retention and data handling — verified

- Default 7 days; expiry computed from terminal state; failed/canceled jobs
  expire too.
- Controlled-timestamp test: rewriting `expires_at` into the past and
  restarting removed exactly the expired jobs' rows and directories while
  preserving models and presets (acceptance 10, expiry half).
- Shortening retention returns a preview (409 + affected jobs) requiring
  explicit confirmation; lengthening applies immediately to inactive jobs.
- Loopback binding by default; WebSocket origin checks; per-run session
  token for non-browser ingest; filename sanitization + path-traversal
  guards on upload and download paths.

## 6. UI — partially verified

The UI is served correctly (index/CSS/JS over the backend) and its JS passed
syntax validation, but **no interactive browser session was run**: visual
layout at wide/narrow widths, keyboard navigation, screen-reader
announcements, long-filename wrapping, and increased-text-size behavior are
implemented per spec but need a human pass (spec §7 pre-delivery checks
remain open).

## 7. Accuracy evaluation (acceptance 12) — limited, synthetic

No universal accuracy percentage is claimed. Reference-transcript tests used
macOS `say` synthetic speech (a caveat in itself — TTS is cleaner than real
speech but the available voice is Korean-accented for English):

| Sample | Model | Result |
| --- | --- | --- |
| Korean sentences, native Korean TTS voice, 12 s | base | **CER 0.0 %** (52 ref chars) |
| English sentences, Korean-accented TTS voice, 12 s | base | **WER 45 %** (31 ref words) — accent-driven; illustrates why model choice and accent matter; larger models are expected to improve this and should be benchmarked on the intended audio |
| 10 s digital silence | base | No transcript output (the `[BLANK_AUDIO]` annotation is filtered) |

Noisy-sample evaluation and real-speech corpora (e.g. LibriSpeech excerpts,
real meeting audio) remain to be run on the intended hardware.

## Known limitations

- Word timestamps are unavailable in the whisper.cpp adapter (supported in
  faster-whisper only); the UI hides unsupported controls.
- Live overlap handling uses utterance endpointing (silence-based) with
  forced splits at the max-chunk limit; a forced split can cut mid-word.
- Whisper can hallucinate short phrases (e.g. “Thanks for watching.”) on
  near-silent or trailing audio; annotation-only segments are filtered, but
  hallucinated plain text is inherent to the model family.
- Real Time mode captures one source at a time; simultaneous microphone +
  computer-audio capture exists only in Talkie mode (two fixed channels).
- One live-type session (Real Time or Talkie) at a time; file jobs queue
  behind it by design (live has priority).
- Talkie's half-duplex gate drops speech that happens while the product is
  speaking (by design); the browser voice engine cannot choose an output
  device; per-direction routing needs Chrome/Edge.

## 8. Talkie mode (added 2026-09-16) — backend verified, UI/hardware open

Scripted clients drove the same protocols the browser uses, against the
reference machine (whisper.cpp Metal, `base` model, local mock LLM, macOS
`say` voices, and a stub in place of the ScreenCaptureKit helper so the
"them" channel could be fed over `/ws/ingest`):

- `/ws/talkie`: Korean speech streamed as the microphone and English speech
  streamed as computer audio **at the same time** produced partials and
  finals with the right `speaker`; both channels shared one segment index
  space and one timeline; segment start times overlapped as sent.
- Direction: `me` utterances were translated to English and `them`
  utterances to Korean (the mock records the requested target); every
  translation carried `speak: true`; each utterance was translated on its
  own (no batching).
- Half-duplex gate: while the client reported `tts: playing`, 3.5 s of
  English fed to the computer-audio channel produced **no** utterance;
  after `idle`, the next English utterance was transcribed and translated
  again.
- Stop: status `completed`; artifacts include TXT/SRT/VTT/JSON plus
  `recording.me.wav` and `recording.them.wav` with correct durations;
  TXT/SRT lines carry `Me:`/`Them:` prefixes; overlapping cues keep their
  timings per speaker; `/api/jobs/{id}/segments` returns `speaker`.
- Crash recovery: killing the backend mid-session left the job
  `interrupted` at restart with both recordings' headers repaired (3.0 s
  declared for 3.0 s received).
- Regression: the Real Time flow (partials, finals, translation, stop,
  artifacts, timings unchanged, no speaker prefixes) and a file job ran
  unchanged after the `SpeechChannel` extraction.
- TTS API: `/api/tts/voices` listed 148 macOS voices with language codes;
  `POST /api/tts` (macOS voice) returned a valid 22.05 kHz WAV (2.5 s for a
  Korean sentence); leading-dash text is safe (stdin); over-limit text →
  400; unknown voice → 400; companion engine without a companion → 503.
- Companion protocol: a simulated companion connected to `/ws/companion`
  (bad token → 403), published two voices, answered a request with WAV that
  `/api/tts` relayed unchanged, an error reply surfaced as 400 with its
  message, and disconnecting unregistered the voices (503 afterwards).

**Not verified** (needs an interactive/hardware pass):

- Browser UI: microphone permission, output-device selectors (`setSinkId`,
  Chrome/Edge), browser-voice playback, banners/rows/replay, prefs in
  `localStorage`, the `tts playing/idle` gating messages sent by the
  playback queue.
- A real call: routing "to them" into a virtual device used as the meeting
  app's microphone, echo behaviour without headphones.
- The real ScreenCaptureKit helper in Talkie (the same helper Real Time
  uses; the stub only proved the spawn/stop path).
- Windows: WASAPI capture and SAPI synthesis in `capture_companion.py`
  (pywin32 code path), Edge/Chrome voices.
- Real LLM endpoint (still TBA) and translation quality for conversational
  speech.
