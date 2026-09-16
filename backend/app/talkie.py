"""Talkie mode: a two-way live interpreter for calls.

Two concurrent speech channels share one engine:
  - `mic`    (speaker "me")   — the user's microphone, spoken in the source
                                language, translated to the target language.
  - `system` (speaker "them") — computer audio (the call's playback), spoken
                                in the target language, translated back.

Each finalized utterance is translated immediately (no batching window) and
reported to the browser with `speak: true`; the browser owns the playback
queue and reports when it is speaking so both channels can be gated
(half-duplex), which keeps the product from transcribing its own voice via
the computer-audio capture.
"""
import asyncio
import contextlib
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import db, engines, exports, translate
from .events import bus, job_event
from .jobs import job_dir
from .live import SpeechChannel, finish_session_job, resolve_engine_for

log = logging.getLogger("talkie")

GATE_RELEASE_S = 0.3   # keep listening paused briefly after playback ends

SPEAKER_CHANNEL = {"me": "mic", "them": "system"}


class TalkieSession:
    """One interpreter session bound to a websocket."""

    def __init__(self, send_json, settings: Dict[str, Any]):
        self.send_json = send_json
        self.settings = settings
        self.state = "starting"
        self.job: Optional[Dict[str, Any]] = None
        self.cancel = threading.Event()
        self.engine = None
        self.effective = ""
        self.source = str(settings.get("source_language") or "").strip()
        self.target = str(settings.get("target_language") or "").strip()
        self.gate_enabled = bool(settings.get("gate_while_speaking", True))
        self.preset: Optional[Dict[str, Any]] = None
        self.channels: Dict[str, SpeechChannel] = {}
        self.next_index = 0
        self.infer_lock = threading.Lock()
        self.tasks: List[asyncio.Task] = []
        self.translation_q: "asyncio.Queue[Optional[Dict[str, Any]]]" = asyncio.Queue()
        self.translation_errors = False
        self.speaking = False
        self._gate_release: Optional[asyncio.TimerHandle] = None
        self.stopped_event = asyncio.Event()
        self._loop = asyncio.get_event_loop()

    def _alloc_index(self) -> int:
        idx = self.next_index
        self.next_index += 1
        return idx

    # ------------- lifecycle -------------

    async def start(self) -> None:
        if not self.source or not self.target:
            raise engines.EngineError("Talkie needs both languages: yours and the other "
                                      "person's.")
        if self.source == self.target:
            raise engines.EngineError("Choose two different languages for Talkie.")
        self.preset = db.get_preset(self.settings.get("llm_preset_id") or "")
        if not self.preset or not translate.preset_is_configured(self.preset):
            # Unlike Real Time, translation is the point of this mode: refuse
            # to start rather than run a session that can never interpret.
            raise engines.EngineError(
                "Talkie needs a configured LLM preset for translation. Create or "
                "complete one under Settings → LLM presets (TBA presets cannot be used).")
        self.engine, self.effective = resolve_engine_for(self.settings)
        self.settings["effective_engine"] = self.effective
        name = time.strftime("Talkie session %Y-%m-%d %H.%M.%S")
        self.job = db.create_job("talkie", name, self.settings, status="recording",
                                 stage="transcribing")
        db.update_job(self.job["id"], session_started_wall=time.time())
        d = job_dir(self.job["id"])
        d.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        common = {k: v for k, v in self.settings.items() if k != "language"}
        self.channels["mic"] = SpeechChannel(
            "mic", "me", dict(common, language=self.source), self.engine, self.job["id"],
            d / "recording.me.wav", self._alloc_index, self.send_json, self._on_final,
            self.cancel, infer_lock=self.infer_lock, session_t0=t0)
        self.channels["system"] = SpeechChannel(
            "system", "them", dict(common, language=self.target), self.engine, self.job["id"],
            d / "recording.them.wav", self._alloc_index, self.send_json, self._on_final,
            self.cancel, infer_lock=self.infer_lock, session_t0=t0)
        self.state = "recording"
        loop = asyncio.get_event_loop()
        self.tasks = [loop.create_task(c.process_loop()) for c in self.channels.values()]
        self.tasks.append(loop.create_task(self._translation_loop()))
        bus.publish_threadsafe(job_event(db.get_job(self.job["id"])))
        await self.send_json({"type": "status", "state": "recording",
                              "job_id": self.job["id"], "engine": self.effective,
                              "source_language": self.source, "target_language": self.target})

    def _feed(self, name: str, pcm: bytes) -> Dict[str, Any]:
        if self.state != "recording":
            return {"accepted": False}
        return self.channels[name].feed(pcm)

    def feed_mic(self, pcm: bytes) -> Dict[str, Any]:
        return self._feed("mic", pcm)

    def feed_system(self, pcm: bytes) -> Dict[str, Any]:
        return self._feed("system", pcm)

    feed_ingest = feed_system  # the Windows companion streams computer audio

    def set_speaking(self, speaking: bool) -> None:
        """Browser playback state. With gating enabled, both channels stop
        detecting speech while the product talks and resume shortly after."""
        self.speaking = speaking
        if not self.gate_enabled or self.state != "recording":
            return
        if self._gate_release is not None:
            self._gate_release.cancel()
            self._gate_release = None
        if speaking:
            for c in self.channels.values():
                c.set_gate(True)
        else:
            self._gate_release = self._loop.call_later(GATE_RELEASE_S, self._release_gate)

    def _release_gate(self) -> None:
        self._gate_release = None
        if not self.speaking:
            for c in self.channels.values():
                c.set_gate(False)

    async def stop(self, reason: str = "user") -> None:
        if self.state in ("stopping", "stopped"):
            await self.stopped_event.wait()
            return
        self.state = "stopping"
        await self.send_json({"type": "status", "state": "stopping",
                              "detail": "Flushing buffered speech"})
        if self._gate_release is not None:
            self._gate_release.cancel()
            self._gate_release = None
        for c in self.channels.values():
            c.set_gate(False)
            await c.flush()
        await self.translation_q.put(None)
        for t in self.tasks:
            if t is not asyncio.current_task():
                with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(t, timeout=90)
        for s in db.get_segments(self.job["id"]):
            if s["translation_status"] == "pending":
                self.translation_errors = True
                db.set_segment_translation(self.job["id"], s["seg_index"], None, "error",
                                           "Translation did not complete before the session "
                                           "ended. Use Retry translation.")
        for c in self.channels.values():
            c.close()
        self._finish_job(interrupted=(reason == "disconnect"))
        self.state = "stopped"
        self.stopped_event.set()
        job = db.get_job(self.job["id"])
        bus.publish_threadsafe(job_event(job))
        with contextlib.suppress(Exception):
            await self.send_json({"type": "stopped", "job_id": self.job["id"],
                                  "status": job["status"], "artifacts": job["artifacts"]})

    async def abort(self) -> None:
        await self.stop(reason="disconnect")

    def _finish_job(self, interrupted: bool) -> None:
        job = db.get_job(self.job["id"])
        segments = db.get_segments(self.job["id"])
        base = Path(job["display_name"]).stem
        artifacts = exports.export_all(job, segments, job_dir(job["id"]), base,
                                       f"{self.source}-{self.target}",
                                       partial=interrupted or self.translation_errors)
        for speaker, fname in (("me", "recording.me.wav"), ("them", "recording.them.wav")):
            rec = job_dir(job["id"]) / fname
            if rec.is_file():
                artifacts.append({"kind": f"recording_{speaker}",
                                  "label": f"{base}.recording.{speaker}.wav",
                                  "file": rec.name, "bytes": rec.stat().st_size})
        finish_session_job(job["id"], artifacts, interrupted, self.translation_errors)

    # ------------- segments / translation -------------

    async def _on_final(self, seg: Dict[str, Any]) -> None:
        idx, seg_id, text, speaker = seg["index"], seg["id"], seg["text"], seg["speaker"]
        db.upsert_segment(self.job["id"], idx, seg_id, seg["start_ms"], seg["end_ms"],
                          text, speaker=speaker)
        db.set_segment_translation(self.job["id"], idx, None, "pending")
        out = dict(seg, translation_status="pending")
        await self.send_json({"type": "final", "segment": out})
        await self.translation_q.put({"seg_index": idx, "seg_id": seg_id, "text": text,
                                      "speaker": speaker})

    def _direction(self, speaker: str):
        """(source language spoken, language to translate into)."""
        return (self.source, self.target) if speaker == "me" else (self.target, self.source)

    async def _translation_loop(self) -> None:
        """One utterance at a time, in the order they were finalized; latency
        matters more than batching here."""
        while True:
            item = await self.translation_q.get()
            if item is None:
                break
            await asyncio.to_thread(self._translate_blocking, item)

    def _translate_blocking(self, item: Dict[str, Any]) -> None:
        spoken, into = self._direction(item["speaker"])

        def on_batch(segs, results, error):
            for s in segs:
                if results is not None:
                    text = results.get(s["seg_index"], "")
                    db.set_segment_translation(self.job["id"], s["seg_index"], text, "done")
                    payload = {"type": "translation", "seg_index": s["seg_index"],
                               "seg_id": s["seg_id"], "speaker": s["speaker"],
                               "status": "done", "text": text, "language": into,
                               "speak": bool(text.strip())}
                else:
                    self.translation_errors = True
                    db.set_segment_translation(self.job["id"], s["seg_index"], None,
                                               "error", error or "Translation failed.")
                    payload = {"type": "translation", "seg_index": s["seg_index"],
                               "seg_id": s["seg_id"], "speaker": s["speaker"],
                               "status": "error", "error": error, "language": into}
                asyncio.run_coroutine_threadsafe(self._send_safe(payload), self._loop)

        translate.translate_segments(self.preset, [item], into, spoken, on_batch,
                                     cancel=self.cancel)

    async def _send_safe(self, payload: Dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            await self.send_json(payload)
