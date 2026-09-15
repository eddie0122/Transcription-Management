"""File-job pipeline: Queued → Preparing Audio → Transcribing → Translating
(if enabled) → Exporting → Completed (or Completed with translation errors /
Failed / Canceled).

Jobs run one at a time off a bounded queue in a worker thread so inference
never blocks the event loop. Live sessions take priority: while a live
session is active the worker delays starting new file jobs and runs
inference at reduced OS priority.
"""
import asyncio
import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import config, db, engines, exports, translate
from .events import bus, job_event
from .media import MediaError, extract_audio, wav_duration_s
from .security import safe_child

log = logging.getLogger("jobs")

STAGE_LABELS = {
    "queued": "Queued",
    "preparing": "Preparing Audio",
    "transcribing": "Transcribing",
    "translating": "Translating",
    "exporting": "Exporting",
    "done": "Completed",
}


def job_dir(job_id: str) -> Path:
    return safe_child(config.JOBS_DIR, job_id)


def dir_size(path: Path) -> int:
    total = 0
    if path.is_dir():
        for p in path.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
    return total


def retention_days() -> float:
    return float(db.get_setting("retention_days", config.RETENTION_DAYS_DEFAULT))


class JobManager:
    def __init__(self) -> None:
        # The queue is created in start() so it binds to the running event
        # loop (Python 3.9 binds asyncio primitives at construction time).
        self.queue: Optional["asyncio.Queue[str]"] = None
        self.cancel_events: Dict[str, threading.Event] = {}
        self.current_job_id: Optional[str] = None
        self.live_active: Callable[[], bool] = lambda: False
        self._worker_task: Optional[asyncio.Task] = None

    # ---------------- lifecycle ----------------

    def start(self) -> None:
        self.queue = asyncio.Queue(maxsize=config.MAX_QUEUED_JOBS)
        self._worker_task = asyncio.get_event_loop().create_task(self._worker())

    async def _worker(self) -> None:
        while True:
            job_id = await self.queue.get()
            job = db.get_job(job_id)
            if not job or job["status"] != "queued":
                continue  # canceled or deleted while queued
            # Prioritize live transcription over queued file processing.
            while self.live_active():
                await asyncio.sleep(1.0)
                job = db.get_job(job_id)
                if not job or job["status"] != "queued":
                    break
            job = db.get_job(job_id)
            if not job or job["status"] != "queued":
                continue
            self.current_job_id = job_id
            try:
                await asyncio.to_thread(self._run_pipeline, job_id)
            except Exception:
                log.exception("job %s crashed", job_id)
                db.mark_terminal(job_id, "failed", retention_days(),
                                 error="Internal error while processing this job; see logs.")
                self._emit(job_id)
            finally:
                self.current_job_id = None
                self.cancel_events.pop(job_id, None)

    # ---------------- public operations ----------------

    async def enqueue(self, job_id: str) -> None:
        self.cancel_events[job_id] = threading.Event()
        await self.queue.put(job_id)
        self._emit(job_id)

    def cancel(self, job_id: str) -> bool:
        job = db.get_job(job_id)
        if not job or job["status"] not in ("queued", "running"):
            return False
        ev = self.cancel_events.get(job_id)
        if ev:
            ev.set()
        if job["status"] == "queued":
            db.mark_terminal(job_id, "canceled", retention_days())
            self._emit(job_id)
        return True

    async def retry(self, job_id: str) -> bool:
        """Restart a failed/canceled/interrupted job's failed stage (from the
        beginning of processing — acceptable for the first release)."""
        job = db.get_job(job_id)
        if not job or job["status"] not in ("failed", "canceled", "interrupted"):
            return False
        input_dir = job_dir(job_id) / "input"
        if not (input_dir.is_dir() and any(input_dir.glob("*"))):
            return False  # source media no longer retained (e.g. live session)
        db.update_job(job_id, status="queued", stage="queued", progress=0,
                      progress_label="", error="", terminal_at=None, expires_at=None)
        await self.enqueue(job_id)
        return True

    async def retry_translation(self, job_id: str) -> bool:
        """Re-run translation only, without retranscribing."""
        job = db.get_job(job_id)
        if not job or job["status"] not in ("completed", "completed_with_translation_errors"):
            return False
        db.update_job(job_id, status="queued", stage="queued", progress=0,
                      progress_label="Waiting to retry translation",
                      terminal_at=None, expires_at=None)
        self.cancel_events[job_id] = threading.Event()
        self._emit(job_id)

        async def _run():
            await asyncio.to_thread(self._run_translation_only, job_id)
        asyncio.get_event_loop().create_task(_run())
        return True

    def delete(self, job_id: str) -> bool:
        """Cancel if active, then remove all managed artifacts and rows."""
        job = db.get_job(job_id)
        if not job:
            return False
        ev = self.cancel_events.get(job_id)
        if ev:
            ev.set()
        deadline = time.time() + 30
        while self.current_job_id == job_id and time.time() < deadline:
            time.sleep(0.2)
        d = job_dir(job_id)
        # Delete rows first so a racing worker cannot recreate results.
        db.delete_job_rows(job_id)
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
        bus.publish_threadsafe({"type": "job.deleted", "job_id": job_id})
        return True

    # ---------------- pipeline (worker thread) ----------------

    def _emit(self, job_id: str) -> None:
        job = db.get_job(job_id)
        if job:
            bus.publish_threadsafe(job_event(job))

    def _set(self, job_id: str, **fields) -> None:
        db.update_job(job_id, **fields)
        self._emit(job_id)

    def _canceled(self, job_id: str) -> bool:
        ev = self.cancel_events.get(job_id)
        return bool(ev and ev.is_set())

    def _run_pipeline(self, job_id: str) -> None:
        job = db.get_job(job_id)
        if not job:
            return
        settings = job["settings"]
        cancel = self.cancel_events.setdefault(job_id, threading.Event())
        d = job_dir(job_id)
        try:
            self._set(job_id, status="running", stage="preparing",
                      started_at=time.time(), progress=0,
                      progress_label="Extracting and normalizing audio")
            inputs = sorted((d / "input").glob("*")) if (d / "input").is_dir() else []
            if not inputs:
                raise MediaError("The uploaded file is no longer available.")
            src = inputs[0]
            wav = d / "audio.wav"
            track = int(settings.get("audio_track", 0))
            extract_audio(src, wav, track_index=track)
            duration = wav_duration_s(wav)
            media = dict(job["media"])
            media["normalized_duration_s"] = duration
            db.update_job(job_id, media=media)
            if cancel.is_set():
                raise Canceled()

            # Transcribe — resolve engine per recorded job settings; never
            # silently change an explicit selection.
            engine, effective = engines.resolve(settings.get("device", "auto"))
            settings["effective_engine"] = effective
            db.update_job(job_id, settings=settings)
            self._set(job_id, stage="transcribing", progress=0,
                      progress_label=f"Transcribing with {effective}")

            last = {"t": 0.0}

            def on_progress(frac: float) -> None:
                now = time.time()
                if now - last["t"] >= 0.5:
                    last["t"] = now
                    self._set(job_id, progress=frac,
                              progress_label=f"Transcribing — {frac * 100:.0f}% of audio processed (estimate)")

            result = engine.transcribe(wav, self._engine_opts(settings),
                                       on_progress=on_progress, cancel=cancel,
                                       nice=self.live_active())
            for i, seg in enumerate(result["segments"]):
                db.upsert_segment(job_id, i, f"{job_id[:8]}-{i}",
                                  seg["start_ms"], seg["end_ms"], seg["text"])
            settings["detected_language"] = result.get("language")
            db.update_job(job_id, settings=settings)
            if cancel.is_set():
                raise Canceled()

            translation_failed = self._run_translation(job_id, settings, cancel)

            self._export_and_finish(job_id, translation_failed)
        except Canceled:
            self._finish_canceled(job_id)
        except (MediaError, engines.EngineError) as e:
            if self._canceled(job_id):
                self._finish_canceled(job_id)
                return
            msg = str(e)
            fb = getattr(e, "fallback", None)
            if fb:
                msg += f" A compatible fallback is available: set hardware to '{fb}' and retry."
            db.mark_terminal(job_id, "failed", retention_days(), error=msg)
            self._set(job_id, stage="done", progress_label="")
        finally:
            db.update_job(job_id, storage_bytes=dir_size(d))
            self._emit(job_id)

    def _finish_canceled(self, job_id: str) -> None:
        db.mark_terminal(job_id, "canceled", retention_days())
        self._set(job_id, progress_label="")

    @staticmethod
    def _engine_opts(settings: Dict[str, Any]) -> Dict[str, Any]:
        preset = engines.PRESETS.get(settings.get("quality_preset", "balanced"),
                                     engines.PRESETS["balanced"])
        adv = settings.get("advanced") or {}
        return {
            "model": settings.get("model", "base"),
            "language": settings.get("language") or None,  # None = auto-detect
            "beam_size": adv.get("beam_size", preset["beam_size"]),
            "temperature": adv.get("temperature", preset["temperature"]),
            "initial_prompt": adv.get("initial_prompt"),
            "word_timestamps": bool(adv.get("word_timestamps", False)),
            "vad": adv.get("vad", True),
            "compute_type": adv.get("compute_type", "auto"),
        }

    def _run_translation(self, job_id: str, settings: Dict[str, Any],
                         cancel: threading.Event) -> bool:
        """Returns True if any translation errors occurred. When translation
        is off, no transcript content is sent anywhere."""
        if not settings.get("translate"):
            return False
        segments = db.get_segments(job_id)
        if not segments:
            return False
        target = settings.get("target_language")
        preset = db.get_preset(settings.get("llm_preset_id") or "")
        for seg in segments:
            db.set_segment_translation(job_id, seg["seg_index"], None, "pending")
        if not preset or not translate.preset_is_configured(preset):
            for seg in segments:
                db.set_segment_translation(
                    job_id, seg["seg_index"], None, "error",
                    "Translation awaiting configuration: the selected LLM preset is "
                    "missing or still contains TBA values. No transcript content was "
                    "sent. Configure the preset in Settings, then use Retry translation.")
            return True
        self._set(job_id, stage="translating", progress=0,
                  progress_label="Translating finalized segments")
        had_error = {"v": False}

        def on_batch(batch, results, error):
            for seg in batch:
                if results is not None:
                    db.set_segment_translation(job_id, seg["seg_index"],
                                               results.get(seg["seg_index"], ""), "done")
                else:
                    had_error["v"] = True
                    db.set_segment_translation(job_id, seg["seg_index"], None,
                                               "error", error or "Translation failed.")
            bus.publish_threadsafe({"type": "job.segments", "job_id": job_id})

        def on_progress(done, total):
            self._set(job_id, progress=done / max(total, 1),
                      progress_label=f"Translating — {done}/{total} units completed")

        translate.translate_segments(
            preset, segments, target, settings.get("detected_language"),
            on_batch, cancel=cancel, on_progress=on_progress)
        return had_error["v"]

    def _export_and_finish(self, job_id: str, translation_failed: bool) -> None:
        job = db.get_job(job_id)
        if not job:
            return
        self._set(job_id, stage="exporting", progress_label="Writing exports")
        segments = db.get_segments(job_id)
        base = Path(job["display_name"]).stem or "transcript"
        target = job["settings"].get("target_language") if job["settings"].get("translate") else None
        artifacts = exports.export_all(job, segments, job_dir(job_id), base, target,
                                       partial=translation_failed)
        recording = job_dir(job_id) / "recording.wav"
        if recording.is_file():
            artifacts.append({"kind": "recording", "label": f"{base}.recording.wav",
                              "file": recording.name, "bytes": recording.stat().st_size})
        inputs = sorted((job_dir(job_id) / "input").glob("*")) if (job_dir(job_id) / "input").is_dir() else []
        for p in inputs:
            artifacts.append({"kind": "input", "label": p.name, "file": f"input/{p.name}",
                              "bytes": p.stat().st_size})
        db.update_job(job_id, artifacts=artifacts)
        status = "completed_with_translation_errors" if translation_failed else "completed"
        db.mark_terminal(job_id, status, retention_days())
        self._set(job_id, stage="done", progress=1.0, progress_label="")

    def _run_translation_only(self, job_id: str) -> None:
        job = db.get_job(job_id)
        if not job:
            return
        cancel = self.cancel_events.setdefault(job_id, threading.Event())
        try:
            self._set(job_id, status="running", stage="translating", progress=0,
                      progress_label="Retrying translation")
            failed = self._run_translation(job_id, job["settings"], cancel)
            self._export_and_finish(job_id, failed)
        except Exception:
            log.exception("translation retry failed for %s", job_id)
            db.mark_terminal(job_id, "completed_with_translation_errors",
                             retention_days(),
                             error="Translation retry failed; original transcription preserved.")
            self._emit(job_id)
        finally:
            self.cancel_events.pop(job_id, None)
            db.update_job(job_id, storage_bytes=dir_size(job_dir(job_id)))


class Canceled(Exception):
    pass


manager = JobManager()
