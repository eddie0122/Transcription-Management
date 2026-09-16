"""Real Time mode: bounded audio buffering, energy-based voice activity
detection, partial/final segment handling, crash-recoverable WAV recording,
and a non-blocking translation queue.

The per-channel audio pipeline lives in SpeechChannel so that a session can
own one channel (Real Time) or two concurrent channels (Talkie mode, see
talkie.py) without duplicating the endpointing and inference logic.

Timing uses a monotonic session clock derived from the received sample
count; the wall-clock session start is stored separately. The live UI is
newest-first, exports are chronological (exports.py sorts by segment index).
"""
import array
import asyncio
import contextlib
import logging
import math
import struct
import threading
import time
import wave
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from . import config, db, engines, exports, translate
from .events import bus, job_event
from .jobs import dir_size, job_dir, retention_days

log = logging.getLogger("live")

BYTES_PER_S = config.SAMPLE_RATE * 2  # s16le mono
MIN_UTTERANCE_S = 0.4                 # ignore blips shorter than this
MIN_TRANSCRIBE_S = 1.1                # pad shorter buffers with silence
LEVEL_INTERVAL_S = 0.2
SILENCE_NOTICE_S = 5.0


class RecoverableWavWriter:
    """WAV writer with incremental header patching so a crash loses at most
    a few seconds, and the file stays playable."""

    PATCH_INTERVAL_S = 5.0

    def __init__(self, path: Path):
        self.path = path
        self._f = open(path, "wb")
        self._data_bytes = 0
        self._last_patch = time.monotonic()
        self._write_header()

    def _write_header(self) -> None:
        f = self._f
        f.seek(0)
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + self._data_bytes))
        f.write(b"WAVEfmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, 1, config.SAMPLE_RATE,
                            BYTES_PER_S, 2, 16))
        f.write(b"data")
        f.write(struct.pack("<I", self._data_bytes))

    def append(self, pcm: bytes) -> None:
        self._f.seek(0, 2)
        self._f.write(pcm)
        self._data_bytes += len(pcm)
        now = time.monotonic()
        if now - self._last_patch >= self.PATCH_INTERVAL_S:
            self._last_patch = now
            self._write_header()
            self._f.flush()

    def close(self) -> None:
        self._write_header()
        self._f.flush()
        self._f.close()


def repair_wav(path: Path) -> None:
    """Fix header sizes after a crash (data length from actual file size)."""
    try:
        size = path.stat().st_size
        if size < 44:
            return
        with open(path, "r+b") as f:
            f.seek(4)
            f.write(struct.pack("<I", size - 8))
            f.seek(40)
            f.write(struct.pack("<I", size - 44))
    except OSError:
        pass


def rms_level(pcm: bytes) -> float:
    if not pcm:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not samples:
        return 0.0
    acc = 0.0
    step = max(1, len(samples) // 512)
    n = 0
    for i in range(0, len(samples), step):
        acc += float(samples[i]) ** 2
        n += 1
    return math.sqrt(acc / max(n, 1)) / 32768.0


class SpeechChannel:
    """One audio stream: bounded frame queue, VAD endpointing, provisional
    (partial) and finalized utterances, and a recoverable WAV recording.

    All coordination runs on the event loop; inference runs in a thread.
    `alloc_index` hands out segment indices (shared between channels of one
    session), `on_final` receives each recognized utterance. `infer_lock`,
    when given, serializes inference across channels so two chunks never
    compete for the same accelerator.
    """

    def __init__(self, name: str, speaker: str, settings: Dict[str, Any], engine: Any,
                 job_id: str, wav_path: Path, alloc_index: Callable[[], int],
                 send_json: Callable[[Dict[str, Any]], Awaitable[None]],
                 on_final: Callable[[Dict[str, Any]], Awaitable[None]],
                 cancel: threading.Event, infer_lock: Optional[threading.Lock] = None,
                 session_t0: Optional[float] = None):
        self.name = name
        self.speaker = speaker
        self.settings = settings
        self.engine = engine
        self.job_id = job_id
        self.alloc_index = alloc_index
        self.send_json = send_json
        self.on_final = on_final
        self.cancel = cancel
        self.infer_lock = infer_lock
        # With a session clock, times are offset by when this stream's first
        # frame arrived so several channels share one timeline; without one,
        # times are relative to this channel's own recording.
        self.session_t0 = session_t0
        adv = settings.get("advanced") or {}
        self.silence_ms = int(adv.get("silence_ms", config.LIVE_SILENCE_MS_DEFAULT))
        self.max_chunk_s = float(adv.get("max_chunk_s", config.LIVE_MAX_CHUNK_S_DEFAULT))
        self.partial_interval = float(adv.get("partial_interval_s", config.LIVE_PARTIAL_INTERVAL_S))
        self.running = True
        self.gated = False
        # audio state
        self.frames: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=2048)
        self.samples_received = 0
        self.clock_offset_ms = 0          # session-clock offset of this stream's first sample
        self._first_frame_seen = False
        self.utt_buf = bytearray()
        self.utt_start_sample = 0
        self.in_speech = False
        self.silence_run_ms = 0.0
        self.noise_floor = 0.008
        self.last_level_sent = 0.0
        self.last_speech_time = time.monotonic()
        self.silence_notified = False
        self.partial_index: Optional[int] = None
        self.partial_busy = False
        self.last_partial_at = 0.0
        self.wav = RecoverableWavWriter(wav_path)

    # ------------- input -------------

    def feed(self, pcm: bytes) -> Dict[str, Any]:
        """Called from the receiver with s16le/16k/mono bytes. Returns backlog
        info so the caller can signal overload."""
        if not self.running:
            return {"accepted": False}
        try:
            self.frames.put_nowait(pcm)
        except asyncio.QueueFull:
            # Bounded queue full: never silently discard — report overload.
            return {"accepted": False, "overload": True}
        backlog_s = sum(len(f) for f in self.frames._queue) / BYTES_PER_S  # type: ignore[attr-defined]
        return {"accepted": True, "backlog_s": backlog_s}

    def set_gate(self, gated: bool) -> None:
        """While gated, speech detection is suspended (audio is still
        recorded). Used while the product itself is speaking."""
        self.gated = gated

    def _ms_at(self, sample: int) -> int:
        return self.clock_offset_ms + int(sample / config.SAMPLE_RATE * 1000)

    def _ingest(self, pcm: bytes) -> None:
        if not self._first_frame_seen:
            self._first_frame_seen = True
            if self.session_t0 is not None:
                self.clock_offset_ms = max(0, int((time.monotonic() - self.session_t0) * 1000))
        self.wav.append(pcm)
        self.samples_received += len(pcm) // 2

    # ------------- processing -------------

    async def process_loop(self) -> None:
        try:
            while self.running:
                try:
                    pcm = await asyncio.wait_for(self.frames.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    await self._maybe_silence_notice()
                    await self._maybe_partial()
                    continue
                self._ingest(pcm)
                await self._vad_feed(pcm)
                await self._maybe_partial()
                await self._maybe_send_level(pcm)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("live processing error (%s)", self.name)
            with contextlib.suppress(Exception):
                await self.send_json({"type": "error", "recoverable": False,
                                      "message": f"Processing failed: {e}"})

    async def drain(self) -> None:
        """Move whatever audio is still queued into the utterance buffer."""
        while not self.frames.empty():
            pcm = self.frames.get_nowait()
            self._ingest(pcm)
            self.utt_buf.extend(pcm)
            if not self.in_speech:
                self.utt_start_sample = self.samples_received - len(self.utt_buf) // 2
                self.in_speech = True

    async def flush(self) -> None:
        """Finalize buffered speech at session end."""
        self.running = False
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(self.drain(), timeout=60)
        if len(self.utt_buf) / BYTES_PER_S >= MIN_UTTERANCE_S:
            await self._finalize_utterance()
        self.utt_buf = bytearray()

    def close(self) -> None:
        self.running = False
        if self.wav:
            self.wav.close()
            self.wav = None

    async def _vad_feed(self, pcm: bytes) -> None:
        frame_ms = len(pcm) / BYTES_PER_S * 1000
        level = rms_level(pcm)
        is_speech = level > max(0.006, self.noise_floor * 2.5)
        if self.gated:
            # The product is speaking: end any open utterance and treat the
            # rest as silence so its own voice is never transcribed.
            if self.in_speech:
                await self._finalize_utterance()
            return
        if not is_speech:
            self.noise_floor = 0.95 * self.noise_floor + 0.05 * max(level, 0.001)
        if self.in_speech:
            self.utt_buf.extend(pcm)
            if is_speech:
                self.silence_run_ms = 0.0
                self.last_speech_time = time.monotonic()
                self.silence_notified = False
            else:
                self.silence_run_ms += frame_ms
            utt_s = len(self.utt_buf) / BYTES_PER_S
            if self.silence_run_ms >= self.silence_ms or utt_s >= self.max_chunk_s:
                await self._finalize_utterance()
        elif is_speech:
            self.in_speech = True
            self.silence_run_ms = 0.0
            self.last_speech_time = time.monotonic()
            self.silence_notified = False
            # include this frame; start time = current position minus its length
            self.utt_start_sample = self.samples_received - len(pcm) // 2
            self.utt_buf = bytearray(pcm)

    async def _maybe_send_level(self, pcm: bytes) -> None:
        now = time.monotonic()
        if now - self.last_level_sent >= LEVEL_INTERVAL_S:
            self.last_level_sent = now
            backlog = self.frames.qsize()
            payload = {"type": "level", "channel": self.name, "speaker": self.speaker,
                       "rms": round(rms_level(pcm), 4),
                       "elapsed_ms": self._ms_at(self.samples_received)}
            backlog_s = backlog * 0.25  # frames are ~250 ms
            if backlog_s > config.LIVE_MAX_BACKLOG_S:
                payload["backlog_s"] = round(backlog_s, 1)
            await self.send_json(payload)

    async def _maybe_silence_notice(self) -> None:
        if (not self.silence_notified and
                time.monotonic() - self.last_speech_time > SILENCE_NOTICE_S):
            self.silence_notified = True
            await self.send_json({"type": "status", "state": "recording",
                                  "detail": "silence", "channel": self.name})

    async def _maybe_partial(self) -> None:
        if (not self.in_speech or self.partial_busy or self.gated or
                len(self.utt_buf) / BYTES_PER_S < MIN_UTTERANCE_S):
            return
        now = time.monotonic()
        if now - self.last_partial_at < self.partial_interval:
            return
        self.last_partial_at = now
        if self.partial_index is None:
            self.partial_index = self.alloc_index()
        idx = self.partial_index
        buf = bytes(self.utt_buf)
        start_ms = self._ms_at(self.utt_start_sample)
        self.partial_busy = True
        try:
            text = await asyncio.to_thread(self._transcribe_buffer, buf)
        finally:
            self.partial_busy = False
        if text is None or not self.running or self.partial_index != idx:
            return
        end_ms = start_ms + int(len(buf) / BYTES_PER_S * 1000)
        await self.send_json({"type": "partial", "segment": {
            "id": f"{self.job_id[:8]}-{idx}", "index": idx, "speaker": self.speaker,
            "start_ms": start_ms, "end_ms": end_ms, "text": text}})

    async def _finalize_utterance(self) -> None:
        buf = bytes(self.utt_buf)
        start_sample = self.utt_start_sample
        self.utt_buf = bytearray()
        self.in_speech = False
        self.silence_run_ms = 0.0
        if self.partial_index is not None:
            idx = self.partial_index
            self.partial_index = None
        else:
            idx = self.alloc_index()
        if len(buf) / BYTES_PER_S < MIN_UTTERANCE_S:
            return
        start_ms = self._ms_at(start_sample)
        end_ms = start_ms + int(len(buf) / BYTES_PER_S * 1000)
        text = await asyncio.to_thread(self._transcribe_buffer, buf)
        if text is None:
            text = ""
        seg_id = f"{self.job_id[:8]}-{idx}"
        if not text.strip():
            # Nothing recognized: retract the provisional row if one was shown.
            await self.send_json({"type": "retract", "seg_id": seg_id, "index": idx,
                                  "speaker": self.speaker})
            return
        await self.on_final({"id": seg_id, "index": idx, "speaker": self.speaker,
                             "start_ms": start_ms, "end_ms": end_ms, "text": text})

    def _transcribe_buffer(self, buf: bytes) -> Optional[str]:
        """Blocking chunk transcription (runs in a thread)."""
        if len(buf) / BYTES_PER_S < MIN_TRANSCRIBE_S:
            buf = buf + b"\x00" * int((MIN_TRANSCRIBE_S - len(buf) / BYTES_PER_S) * BYTES_PER_S)
        tmp = config.TMP_DIR / f"live-{self.job_id[:8]}-{self.name}-{time.monotonic_ns()}.wav"
        lock = self.infer_lock or contextlib.nullcontext()
        try:
            with wave.open(str(tmp), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(config.SAMPLE_RATE)
                w.writeframes(buf)
            from .jobs import JobManager
            opts = JobManager._engine_opts(self.settings)
            opts["vad"] = False  # endpointing already applied
            with lock:
                result = self.engine.transcribe(tmp, opts, cancel=self.cancel)
            return " ".join(s["text"] for s in result["segments"]).strip()
        except engines.EngineError as e:
            log.warning("live chunk transcription failed: %s", e)
            return None
        finally:
            tmp.unlink(missing_ok=True)
            Path(str(tmp.with_suffix("")) + ".json").unlink(missing_ok=True)


def resolve_engine_for(settings: Dict[str, Any]):
    """Resolve the engine and fail fast if the model is missing rather than
    mid-session. Returns (engine, effective_label)."""
    engine, effective = engines.resolve(settings.get("device", "auto"))
    if hasattr(engine, "model_path"):
        engine.model_path(settings.get("model", "base"))
    elif hasattr(engine, "_load"):
        engine._load(settings.get("model", "base"),
                     (settings.get("advanced") or {}).get("compute_type", "auto"))
    return engine, effective


class LiveSession:
    """One live session bound to a websocket, with a single audio channel."""

    def __init__(self, send_json, settings: Dict[str, Any]):
        self.send_json = send_json
        self.settings = settings
        self.state = "starting"
        self.job: Optional[Dict[str, Any]] = None
        self.cancel = threading.Event()
        self.engine = None
        self.effective = ""
        self.channel: Optional[SpeechChannel] = None
        self.next_index = 0
        self.tasks: List[asyncio.Task] = []
        self.translation_q: "asyncio.Queue[Optional[Dict[str, Any]]]" = asyncio.Queue()
        self.translation_errors = False
        self.translation_enabled = False
        self.preset: Optional[Dict[str, Any]] = None
        self.stopped_event = asyncio.Event()
        self._loop = asyncio.get_event_loop()

    def _alloc_index(self) -> int:
        idx = self.next_index
        self.next_index += 1
        return idx

    # ------------- lifecycle -------------

    async def start(self) -> None:
        """Validate readiness (permissions are validated client/adapter-side),
        create the job, open the recording, and start workers. Never starts
        capture by itself — audio only flows once the client/adapter sends it."""
        self.engine, self.effective = resolve_engine_for(self.settings)
        self.settings["effective_engine"] = self.effective
        self.translation_enabled = bool(self.settings.get("translate"))
        if self.translation_enabled:
            self.preset = db.get_preset(self.settings.get("llm_preset_id") or "")
            if not self.preset or not translate.preset_is_configured(self.preset):
                # Keep transcription available; show translation as awaiting config.
                self.translation_enabled = False
                await self.send_json({
                    "type": "warning", "code": "translation_unconfigured",
                    "message": "Translation is awaiting configuration: the selected LLM "
                               "preset is missing or contains TBA values. The session "
                               "will transcribe only."})
        name = time.strftime("Live session %Y-%m-%d %H.%M.%S")
        self.job = db.create_job("live", name, self.settings, status="recording",
                                 stage="transcribing")
        db.update_job(self.job["id"], session_started_wall=time.time())
        d = job_dir(self.job["id"])
        d.mkdir(parents=True, exist_ok=True)
        self.channel = SpeechChannel(
            "main", "", self.settings, self.engine, self.job["id"], d / "recording.wav",
            self._alloc_index, self.send_json, self._on_final, self.cancel)
        self.state = "recording"
        loop = asyncio.get_event_loop()
        self.tasks = [loop.create_task(self.channel.process_loop()),
                      loop.create_task(self._translation_loop())]
        bus.publish_threadsafe(job_event(db.get_job(self.job["id"])))
        await self.send_json({"type": "status", "state": "recording",
                              "job_id": self.job["id"], "engine": self.effective})

    def feed(self, pcm: bytes) -> Dict[str, Any]:
        """Called from the websocket receiver with s16le/16k/mono bytes."""
        if self.state != "recording" or self.channel is None:
            return {"accepted": False}
        return self.channel.feed(pcm)

    feed_ingest = feed  # external capture companion streams into the same channel

    async def stop(self, reason: str = "user") -> None:
        """Stop capture, flush buffered speech, finish or report pending
        translation, close the recording, export, and report the result."""
        if self.state in ("stopping", "stopped"):
            await self.stopped_event.wait()
            return
        self.state = "stopping"
        await self.send_json({"type": "status", "state": "stopping",
                              "detail": "Flushing buffered speech"})
        await self.channel.flush()
        # Signal translation completion and wait bounded.
        await self.translation_q.put(None)
        for t in self.tasks:
            if t is not asyncio.current_task():
                with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(t, timeout=90)
        pending = [s for s in db.get_segments(self.job["id"])
                   if s["translation_status"] == "pending"]
        for s in pending:
            self.translation_errors = True
            db.set_segment_translation(self.job["id"], s["seg_index"], None, "error",
                                       "Translation did not complete before the session "
                                       "ended. Use Retry translation.")
        self.channel.close()
        self._finish_job(interrupted=(reason == "disconnect"))
        self.state = "stopped"
        self.stopped_event.set()
        job = db.get_job(self.job["id"])
        bus.publish_threadsafe(job_event(job))
        with contextlib.suppress(Exception):
            await self.send_json({"type": "stopped", "job_id": self.job["id"],
                                  "status": job["status"],
                                  "artifacts": job["artifacts"]})

    async def abort(self) -> None:
        """Websocket dropped without an explicit stop: preserve everything and
        mark the session interrupted."""
        await self.stop(reason="disconnect")

    def _finish_job(self, interrupted: bool) -> None:
        job = db.get_job(self.job["id"])
        segments = db.get_segments(self.job["id"])
        base = Path(job["display_name"]).stem
        target = self.settings.get("target_language") if self.settings.get("translate") else None
        artifacts = exports.export_all(job, segments, job_dir(job["id"]), base, target,
                                       partial=interrupted or self.translation_errors)
        rec = job_dir(job["id"]) / "recording.wav"
        if rec.is_file():
            artifacts.append({"kind": "recording", "label": f"{base}.recording.wav",
                              "file": rec.name, "bytes": rec.stat().st_size})
        finish_session_job(job["id"], artifacts, interrupted, self.translation_errors)

    # ------------- segments / translation -------------

    async def _on_final(self, seg: Dict[str, Any]) -> None:
        idx, seg_id, text = seg["index"], seg["id"], seg["text"]
        db.upsert_segment(self.job["id"], idx, seg_id, seg["start_ms"], seg["end_ms"], text)
        t_status = "pending" if self.translation_enabled else "none"
        if self.settings.get("translate") and not self.translation_enabled:
            t_status = "error"  # awaiting configuration, recorded below
            db.set_segment_translation(self.job["id"], idx, None, "error",
                                       "Translation awaiting configuration.")
            self.translation_errors = True
        elif self.translation_enabled:
            db.set_segment_translation(self.job["id"], idx, None, "pending")
        out = {"id": seg_id, "index": idx, "start_ms": seg["start_ms"],
               "end_ms": seg["end_ms"], "text": text, "translation_status": t_status}
        await self.send_json({"type": "final", "segment": out})
        if self.translation_enabled:
            await self.translation_q.put({"seg_index": idx, "seg_id": seg_id, "text": text})

    async def _translation_loop(self) -> None:
        """Translate finalized segments without ever blocking capture or
        transcription. Batches whatever is pending each cycle."""
        closing = False
        while True:
            batch: List[Dict[str, Any]] = []
            item = await self.translation_q.get()
            if item is None:
                break
            batch.append(item)
            # small gather window for batching
            with contextlib.suppress(asyncio.TimeoutError):
                while len(batch) < translate.BATCH_SEG_LIMIT:
                    nxt = await asyncio.wait_for(self.translation_q.get(), timeout=0.4)
                    if nxt is None:
                        closing = True
                        break
                    batch.append(nxt)
            await asyncio.to_thread(self._translate_batch_blocking, batch)
            if closing:
                break

    def _translate_batch_blocking(self, batch: List[Dict[str, Any]]) -> None:
        def on_batch(segs, results, error):
            for s in segs:
                if results is not None:
                    text = results.get(s["seg_index"], "")
                    db.set_segment_translation(self.job["id"], s["seg_index"], text, "done")
                    payload = {"type": "translation", "seg_index": s["seg_index"],
                               "seg_id": s["seg_id"], "status": "done", "text": text}
                else:
                    self.translation_errors = True
                    db.set_segment_translation(self.job["id"], s["seg_index"], None,
                                               "error", error or "Translation failed.")
                    payload = {"type": "translation", "seg_index": s["seg_index"],
                               "seg_id": s["seg_id"], "status": "error", "error": error}
                asyncio.run_coroutine_threadsafe(self._send_safe(payload), self._loop)

        translate.translate_segments(self.preset, batch,
                                     self.settings.get("target_language"),
                                     self.settings.get("language"),
                                     on_batch, cancel=self.cancel)

    async def _send_safe(self, payload: Dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            await self.send_json(payload)


def finish_session_job(job_id: str, artifacts: List[Dict[str, Any]],
                       interrupted: bool, translation_errors: bool) -> None:
    """Record artifacts and the terminal status of a live/talkie session."""
    db.update_job(job_id, artifacts=artifacts, stage="done", progress=1.0,
                  storage_bytes=dir_size(job_dir(job_id)))
    if interrupted:
        status = "interrupted"
    elif translation_errors:
        status = "completed_with_translation_errors"
    else:
        status = "completed"
    db.mark_terminal(job_id, status, retention_days(),
                     error="Connection lost during the session; results up to the "
                           "interruption were preserved." if interrupted else "")


class LiveManager:
    """At most one live or talkie session at a time."""

    def __init__(self) -> None:
        self.session: Optional[Any] = None

    def active(self) -> bool:
        return self.session is not None and self.session.state in ("starting", "recording", "stopping")

    async def begin(self, send_json, settings: Dict[str, Any], session_cls=LiveSession):
        if self.active():
            raise engines.EngineError(
                "Another live session is already running. Stop it before starting a "
                "new one.")
        s = session_cls(send_json, settings)
        self.session = s
        try:
            await s.start()
        except Exception:
            self.session = None
            raise
        return s

    async def end(self, session, reason: str = "user") -> None:
        try:
            await session.stop(reason=reason)
        finally:
            if self.session is session:
                self.session = None


live_manager = LiveManager()
