"""FastAPI application: settings/jobs APIs, event stream, live websocket,
static frontend serving, startup recovery, and graceful shutdown.

Bind to loopback by default (native) or to the container interface (Docker,
via APP_HOST=0.0.0.0) where only the reverse proxy publishes a port.
"""
import asyncio
import contextlib
import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import (FastAPI, File, Form, HTTPException, UploadFile, WebSocket,
                     WebSocketDisconnect)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, config, db, engines, media, retention, translate
from .capture_macos import SystemAudioCapture, helper_status
from .events import bus, job_event
from .jobs import dir_size, job_dir, manager, retention_days
from .languages import COMMON_TARGETS, WHISPER_LANGUAGES
from .live import live_manager, repair_wav
from .security import safe_child, sanitize_filename, secret_store

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("app")

ACTIVE = ("queued", "running", "recording")


def _recover_interrupted() -> None:
    """After a restart: keep completed results, mark interrupted jobs
    accurately, and repair live recordings' WAV headers."""
    for job in db.list_jobs():
        if job["status"] in ACTIVE:
            wav = job_dir(job["id"]) / "recording.wav"
            if wav.is_file():
                repair_wav(wav)
            db.mark_terminal(
                job["id"], "interrupted", retention_days(),
                error="The application stopped while this job was in progress. "
                      "Completed portions were preserved; you can retry the failed stage.")


def _seed_env_preset() -> None:
    """Docker first-run preset defaults from environment; user edits are
    persisted and never overwritten on restart. TBA values seed nothing."""
    if db.get_setting("env_preset_seeded"):
        return
    if config.is_tba(config.LLM_BASE_URL) or config.is_tba(config.LLM_MODEL):
        return
    key_ref = ""
    if config.LLM_API_KEY_FILE and Path(config.LLM_API_KEY_FILE).is_file():
        content = Path(config.LLM_API_KEY_FILE).read_text().strip()
        if not config.is_tba(content):
            key_ref = "file:" + config.LLM_API_KEY_FILE
    db.create_preset("Environment default", config.LLM_BASE_URL, config.LLM_MODEL,
                     60, {}, key_ref)
    db.set_setting("env_preset_seeded", True)
    log.info("Seeded first-run LLM preset from environment")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    db.init()
    bus.bind_loop(asyncio.get_event_loop())
    _recover_interrupted()
    _seed_env_preset()
    retention.cleanup()
    manager.live_active = live_manager.active
    manager.start()
    retention_task = asyncio.get_event_loop().create_task(retention.retention_loop())
    log.info("AudioTranscription backend ready (data: %s)", config.DATA_DIR)
    try:
        yield
    finally:
        # Graceful shutdown: stop accepting work, flush the live session,
        # and mark unfinished jobs interrupted before exit.
        retention_task.cancel()
        if live_manager.session is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    live_manager.end(live_manager.session, reason="disconnect"),
                    timeout=45)
        for ev in manager.cancel_events.values():
            ev.set()
        for job in db.list_jobs():
            if job["status"] in ACTIVE:
                db.mark_terminal(job["id"], "interrupted", retention_days(),
                                 error="Application shut down during processing.")
        log.info("shutdown complete")


app = FastAPI(title="AudioTranscription", version=__version__, lifespan=lifespan)


def _same_origin_ok(ws: WebSocket) -> bool:
    origin = ws.headers.get("origin")
    if not origin:
        return False
    host = (ws.headers.get("host") or "").split(":")[0]
    return urlparse(origin).hostname == host and host != ""


# ---------------------------------------------------------------- system

@app.get("/api/health")
def health():
    try:
        db.get_setting("retention_days")
        return {"ok": True, "version": __version__}
    except Exception as e:
        return JSONResponse(status_code=503, content={"ok": False, "error": str(e)})


@app.get("/api/system")
def system():
    info = engines.detect(force=True)
    info = dict(info)
    info["decoders"] = media.decoder_info()
    info["capture"] = {
        "computer_audio": helper_status(),
        "companion_ingest": True,
    }
    info["limits"] = {"max_upload_mb": config.MAX_UPLOAD_MB,
                      "max_duration_min": config.MAX_DURATION_MIN,
                      "supported_extensions": media.SUPPORTED_EXTENSIONS}
    info["languages"] = WHISPER_LANGUAGES
    info["common_targets"] = COMMON_TARGETS
    info["models"] = engines.models_catalog()
    info["capabilities"] = {
        "whispercpp": engines.WhisperCppEngine.capabilities,
        "fasterwhisper": engines.FasterWhisperEngine.capabilities,
    }
    info["live_active"] = live_manager.active()
    return info


@app.get("/api/session")
def session_token():
    """Per-run credential for non-browser capture clients (companion)."""
    return {"token": config.SESSION_TOKEN}


# ---------------------------------------------------------------- settings

@app.get("/api/settings")
def get_settings():
    return {
        "retention_days": db.get_setting("retention_days", config.RETENTION_DAYS_DEFAULT),
        "defaults": db.get_setting("defaults", {}),
        "default_preset_id": db.get_setting("default_preset_id"),
    }


@app.put("/api/settings")
async def put_settings(body: Dict[str, Any]):
    if "defaults" in body:
        db.set_setting("defaults", body["defaults"])
    if "default_preset_id" in body:
        db.set_setting("default_preset_id", body["default_preset_id"])
    if "retention_days" in body:
        days = float(body["retention_days"])
        if days <= 0 or days > 3650:
            raise HTTPException(400, "Retention must be between 1 and 3650 days.")
        current = float(db.get_setting("retention_days", config.RETENTION_DAYS_DEFAULT))
        if days < current and not body.get("confirm_retention"):
            affected = retention.preview_retention_change(days)
            if affected:
                return JSONResponse(status_code=409, content={
                    "needs_confirmation": True,
                    "message": f"Shortening retention to {days:g} days will immediately "
                               f"delete {len(affected)} job(s).",
                    "affected": affected})
        await asyncio.to_thread(retention.apply_retention_change, days)
    return get_settings()


# ---------------------------------------------------------------- presets

def _preset_public(p: Dict[str, Any]) -> Dict[str, Any]:
    q = {k: p[k] for k in ("id", "name", "base_url", "model_id", "timeout_s",
                           "params", "created_at", "updated_at")}
    q["has_key"] = bool(p.get("key_ref"))
    q["configured"] = translate.preset_is_configured(p)
    return q


@app.get("/api/presets")
def presets_list():
    return [_preset_public(p) for p in db.list_presets()]


@app.post("/api/presets")
def presets_create(body: Dict[str, Any]):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Preset name is required.")
    if any(p["name"] == name for p in db.list_presets()):
        raise HTTPException(400, f"A preset named '{name}' already exists.")
    key_ref = ""
    if body.get("api_key"):
        key_ref = secret_store.store(body["api_key"])
    p = db.create_preset(name, (body.get("base_url") or "TBA").strip(),
                         (body.get("model_id") or "TBA").strip(),
                         float(body.get("timeout_s") or 60),
                         body.get("params") or {}, key_ref)
    return _preset_public(p)


@app.put("/api/presets/{pid}")
def presets_update(pid: str, body: Dict[str, Any]):
    p = db.get_preset(pid)
    if not p:
        raise HTTPException(404, "Preset not found.")
    fields: Dict[str, Any] = {}
    for k in ("name", "base_url", "model_id"):
        if k in body:
            fields[k] = str(body[k]).strip()
    if "timeout_s" in body:
        fields["timeout_s"] = float(body["timeout_s"])
    if "params" in body:
        fields["params"] = body["params"]
    if body.get("api_key"):
        fields["key_ref"] = secret_store.store(body["api_key"], existing_ref=p["key_ref"]
                                               if not p["key_ref"].startswith("file:") else None)
    if body.get("clear_api_key"):
        secret_store.delete(p["key_ref"])
        fields["key_ref"] = ""
    db.update_preset(pid, **fields)
    return _preset_public(db.get_preset(pid))


@app.delete("/api/presets/{pid}")
def presets_delete(pid: str):
    p = db.get_preset(pid)
    if not p:
        raise HTTPException(404, "Preset not found.")
    secret_store.delete(p["key_ref"])
    db.delete_preset(pid)
    if db.get_setting("default_preset_id") == pid:
        db.set_setting("default_preset_id", None)
    return {"ok": True}


@app.post("/api/presets/{pid}/test")
async def presets_test(pid: str):
    p = db.get_preset(pid)
    if not p:
        raise HTTPException(404, "Preset not found.")
    return await asyncio.to_thread(translate.test_connection, p)


# ---------------------------------------------------------------- models

@app.post("/api/models/download")
async def models_download(body: Dict[str, Any]):
    engine, model = body.get("engine"), body.get("model")

    def run():
        def on_progress(done, total):
            bus.publish_threadsafe({"type": "model.download", "engine": engine,
                                    "model": model, "done": done, "total": total})
        try:
            engines.download_model(engine, model, on_progress)
            bus.publish_threadsafe({"type": "model.download", "engine": engine,
                                    "model": model, "status": "done"})
        except Exception as e:
            bus.publish_threadsafe({"type": "model.download", "engine": engine,
                                    "model": model, "status": "error",
                                    "error": str(e)})

    threading.Thread(target=run, daemon=True).start()
    return {"started": True}


@app.delete("/api/models/{engine}/{model}")
def models_delete(engine: str, model: str):
    engines.delete_model(engine, model)
    return {"ok": True}


# ---------------------------------------------------------------- jobs

@app.post("/api/jobs")
async def jobs_create(files: List[UploadFile] = File(...), settings: str = Form("{}")):
    try:
        job_settings = json.loads(settings)
    except ValueError:
        raise HTTPException(400, "Invalid settings JSON.")
    if job_settings.get("translate"):
        if not job_settings.get("target_language"):
            raise HTTPException(400, "Translation is enabled but no target language "
                                     "is selected.")
        preset = db.get_preset(job_settings.get("llm_preset_id") or "")
        if not preset:
            raise HTTPException(400, "Translation is enabled but no LLM preset is "
                                     "selected.")
    results = []
    for upload in files:
        results.append(await _create_one_job(upload, job_settings))
    return results


async def _create_one_job(upload: UploadFile, job_settings: Dict[str, Any]) -> Dict[str, Any]:
    display = sanitize_filename(upload.filename or "upload")
    job = db.create_job("file", display, dict(job_settings))
    d = job_dir(job["id"])
    (d / "input").mkdir(parents=True, exist_ok=True)
    dest = safe_child(d / "input", display)
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    size = 0
    try:
        with open(dest, "wb") as f:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise media.MediaError(
                        f"File exceeds the {config.MAX_UPLOAD_MB} MB upload limit.")
                f.write(chunk)
        info = await asyncio.to_thread(media.probe, dest)
        if info.get("duration_s") and info["duration_s"] > config.MAX_DURATION_MIN * 60:
            raise media.MediaError(
                f"Duration {info['duration_s'] / 60:.0f} min exceeds the "
                f"{config.MAX_DURATION_MIN} min limit.")
        db.update_job(job["id"], media=info, storage_bytes=size)
        await manager.enqueue(job["id"])
        job = db.get_job(job["id"])
        return {"ok": True, "job": job}
    except media.MediaError as e:
        db.mark_terminal(job["id"], "failed", retention_days(), error=str(e))
        db.update_job(job["id"], storage_bytes=dir_size(d))
        bus.publish_threadsafe(job_event(db.get_job(job["id"])))
        return {"ok": False, "job": db.get_job(job["id"]), "error": str(e)}


@app.get("/api/jobs")
def jobs_list():
    return db.list_jobs()


@app.get("/api/jobs/{job_id}")
def jobs_get(job_id: str):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    return job


@app.get("/api/jobs/{job_id}/segments")
def jobs_segments(job_id: str):
    if not db.get_job(job_id):
        raise HTTPException(404, "Job not found.")
    return db.get_segments(job_id)


@app.post("/api/jobs/{job_id}/cancel")
def jobs_cancel(job_id: str):
    if not manager.cancel(job_id):
        raise HTTPException(409, "Job is not cancelable in its current state.")
    return {"ok": True}


@app.post("/api/jobs/{job_id}/retry")
async def jobs_retry(job_id: str):
    if not await manager.retry(job_id):
        raise HTTPException(409, "Job cannot be retried (missing source media or "
                                 "not in a retryable state).")
    return {"ok": True}


@app.post("/api/jobs/{job_id}/retry-translation")
async def jobs_retry_translation(job_id: str):
    if not await manager.retry_translation(job_id):
        raise HTTPException(409, "Translation retry is only available for completed jobs.")
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
async def jobs_delete(job_id: str):
    ok = await asyncio.to_thread(manager.delete, job_id)
    if not ok:
        raise HTTPException(404, "Job not found.")
    return {"ok": True}


@app.get("/api/jobs/{job_id}/download/{kind:path}")
def jobs_download(job_id: str, kind: str):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    for a in job.get("artifacts", []):
        if a["kind"] == kind or a["file"] == kind:
            path = safe_child(job_dir(job_id), *a["file"].split("/"))
            if not path.is_file():
                raise HTTPException(410, "This artifact is no longer available.")
            return FileResponse(str(path), filename=a["label"])
    raise HTTPException(404, "No such artifact for this job.")


# ---------------------------------------------------------------- websockets

@app.websocket("/ws/events")
async def ws_events(ws: WebSocket):
    if not _same_origin_ok(ws):
        await ws.close(code=4403)
        return
    await ws.accept()
    q = bus.subscribe()
    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=20)
                await ws.send_json(event)
            except asyncio.TimeoutError:
                await ws.send_json({"type": "ping"})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        bus.unsubscribe(q)


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    token_ok = ws.query_params.get("token") == config.SESSION_TOKEN
    if not (_same_origin_ok(ws) or token_ok):
        await ws.close(code=4403)
        return
    await ws.accept()
    session = None
    capture: Optional[SystemAudioCapture] = None
    overload_notified = 0.0
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect()
            if msg.get("bytes") is not None:
                if session is not None:
                    info = session.feed(msg["bytes"])
                    now = time.monotonic()
                    if info.get("overload") and now - overload_notified > 3:
                        overload_notified = now
                        await ws.send_json({
                            "type": "error", "recoverable": True, "code": "overload",
                            "message": "Processing backlog exceeded the buffer limit. "
                                       "Capture is paused; audio already received is "
                                       "preserved. Resume once the backlog clears."})
                continue
            if msg.get("text") is None:
                continue
            try:
                data = json.loads(msg["text"])
            except ValueError:
                continue
            mtype = data.get("type")
            if mtype == "start" and session is None:
                try:
                    session = await live_manager.begin(ws.send_json,
                                                       data.get("settings") or {})
                except engines.EngineError as e:
                    await ws.send_json({"type": "error", "recoverable": True,
                                        "message": str(e)})
                    continue
                if (data.get("settings") or {}).get("source") == "system":
                    status = helper_status()
                    if status["mode"] == "native":
                        async def on_pcm(chunk, s=session):
                            s.feed(chunk)

                        async def on_error(message):
                            await ws.send_json({"type": "error", "recoverable": True,
                                                "code": "device_lost",
                                                "message": message})
                        capture = SystemAudioCapture(on_pcm, on_error)
                        try:
                            await capture.start()
                        except RuntimeError as e:
                            await ws.send_json({"type": "error", "recoverable": True,
                                                "message": str(e)})
                            await live_manager.end(session)
                            session, capture = None, None
                    elif status["mode"] == "companion":
                        await ws.send_json({
                            "type": "status", "state": "recording",
                            "detail": "waiting_companion",
                            "message": "Waiting for the Windows capture companion. "
                                       "Run companion/windows/capture_companion.py "
                                       "on the Windows host to stream computer "
                                       "audio into this session."})
                    else:
                        await ws.send_json({"type": "error", "recoverable": True,
                                            "message": status["reason"]})
                        await live_manager.end(session)
                        session = None
            elif mtype == "device_lost" and session is not None:
                await ws.send_json({"type": "error", "recoverable": True,
                                    "code": "device_lost",
                                    "message": data.get("message") or
                                    "The audio input device was disconnected."})
            elif mtype == "stop" and session is not None:
                if capture:
                    await capture.stop()
                    capture = None
                await live_manager.end(session)
                session = None
    except WebSocketDisconnect:
        pass
    finally:
        if capture:
            await capture.stop()
        if session is not None:
            await live_manager.end(session, reason="disconnect")


@app.websocket("/ws/ingest")
async def ws_ingest(ws: WebSocket):
    """External capture companion (e.g. Windows WASAPI loopback) streams PCM
    into the active live session, authenticated by the per-run token."""
    if ws.query_params.get("token") != config.SESSION_TOKEN:
        await ws.close(code=4403)
        return
    await ws.accept()
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data and live_manager.session is not None:
                live_manager.session.feed(data)
    except WebSocketDisconnect:
        pass


# ---------------------------------------------------------------- frontend

_dist = Path(config.FRONTEND_DIST)
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="frontend")


def run() -> None:
    import uvicorn
    uvicorn.run("app.main:app", host=config.APP_HOST, port=config.APP_PORT,
                log_level="info", ws_max_size=4 * 1024 * 1024)


if __name__ == "__main__":
    run()
