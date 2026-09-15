"""SQLite persistence for jobs, segments, LLM presets, and settings.

A single connection guarded by a lock keeps writes serialized; the
application intentionally runs one backend instance per data directory.
"""
import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from . import config

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,               -- 'file' | 'live'
  display_name TEXT NOT NULL,
  status TEXT NOT NULL,             -- queued|running|completed|completed_with_translation_errors|failed|canceled|interrupted|recording
  stage TEXT NOT NULL DEFAULT '',   -- queued|preparing|transcribing|translating|exporting|done
  progress REAL NOT NULL DEFAULT 0,
  progress_label TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  started_at REAL,
  terminal_at REAL,
  expires_at REAL,
  settings TEXT NOT NULL DEFAULT '{}',
  media TEXT NOT NULL DEFAULT '{}',
  artifacts TEXT NOT NULL DEFAULT '[]',
  storage_bytes INTEGER NOT NULL DEFAULT 0,
  session_started_wall REAL
);
CREATE TABLE IF NOT EXISTS segments (
  job_id TEXT NOT NULL,
  seg_index INTEGER NOT NULL,
  seg_id TEXT NOT NULL,
  start_ms INTEGER NOT NULL,
  end_ms INTEGER NOT NULL,
  text TEXT NOT NULL DEFAULT '',
  translation TEXT,
  translation_status TEXT NOT NULL DEFAULT 'none',  -- none|pending|done|error
  translation_error TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (job_id, seg_index)
);
CREATE TABLE IF NOT EXISTS presets (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  base_url TEXT NOT NULL,
  model_id TEXT NOT NULL,
  timeout_s REAL NOT NULL DEFAULT 60,
  params TEXT NOT NULL DEFAULT '{}',
  key_ref TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_expires ON jobs (expires_at);
"""

TERMINAL_STATUSES = (
    "completed", "completed_with_translation_errors", "failed", "canceled", "interrupted",
)


def init() -> None:
    global _conn
    with _lock:
        _conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.executescript(SCHEMA)
        _conn.commit()


def _c() -> sqlite3.Connection:
    assert _conn is not None, "db.init() not called"
    return _conn


def _row_to_job(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    for k in ("settings", "media"):
        d[k] = json.loads(d.get(k) or "{}")
    d["artifacts"] = json.loads(d.get("artifacts") or "[]")
    return d


# ---------------- jobs ----------------

def create_job(kind: str, display_name: str, settings: Dict[str, Any],
               media: Optional[Dict[str, Any]] = None,
               status: str = "queued", stage: str = "queued") -> Dict[str, Any]:
    job_id = uuid.uuid4().hex
    now = time.time()
    with _lock:
        _c().execute(
            "INSERT INTO jobs (id, kind, display_name, status, stage, created_at, settings, media)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (job_id, kind, display_name, status, stage, now,
             json.dumps(settings), json.dumps(media or {})),
        )
        _c().commit()
    return get_job(job_id)


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        row = _c().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def list_jobs() -> List[Dict[str, Any]]:
    with _lock:
        rows = _c().execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
    return [_row_to_job(r) for r in rows]


def update_job(job_id: str, **fields) -> None:
    if not fields:
        return
    cols, vals = [], []
    for k, v in fields.items():
        if k in ("settings", "media"):
            v = json.dumps(v)
        elif k == "artifacts":
            v = json.dumps(v)
        cols.append(f"{k}=?")
        vals.append(v)
    vals.append(job_id)
    with _lock:
        _c().execute(f"UPDATE jobs SET {', '.join(cols)} WHERE id=?", vals)
        _c().commit()


def mark_terminal(job_id: str, status: str, retention_days: float,
                  error: str = "") -> None:
    """Set terminal state; expiry counts from the terminal timestamp."""
    now = time.time()
    expires = now + retention_days * 86400 if retention_days > 0 else None
    update_job(job_id, status=status, terminal_at=now, expires_at=expires, error=error)


def delete_job_rows(job_id: str) -> None:
    with _lock:
        _c().execute("DELETE FROM segments WHERE job_id=?", (job_id,))
        _c().execute("DELETE FROM jobs WHERE id=?", (job_id,))
        _c().commit()


def expired_jobs(now: Optional[float] = None) -> List[Dict[str, Any]]:
    now = now or time.time()
    with _lock:
        rows = _c().execute(
            "SELECT * FROM jobs WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,)
        ).fetchall()
    return [_row_to_job(r) for r in rows]


# ---------------- segments ----------------

def upsert_segment(job_id: str, seg_index: int, seg_id: str, start_ms: int,
                   end_ms: int, text: str) -> None:
    with _lock:
        _c().execute(
            "INSERT INTO segments (job_id, seg_index, seg_id, start_ms, end_ms, text)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(job_id, seg_index) DO UPDATE SET"
            " seg_id=excluded.seg_id, start_ms=excluded.start_ms,"
            " end_ms=excluded.end_ms, text=excluded.text",
            (job_id, seg_index, seg_id, start_ms, end_ms, text),
        )
        _c().commit()


def set_segment_translation(job_id: str, seg_index: int, translation: Optional[str],
                            status: str, error: str = "") -> None:
    with _lock:
        _c().execute(
            "UPDATE segments SET translation=?, translation_status=?, translation_error=?"
            " WHERE job_id=? AND seg_index=?",
            (translation, status, error, job_id, seg_index),
        )
        _c().commit()


def get_segments(job_id: str) -> List[Dict[str, Any]]:
    with _lock:
        rows = _c().execute(
            "SELECT * FROM segments WHERE job_id=? ORDER BY seg_index", (job_id,)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------- presets ----------------

def create_preset(name: str, base_url: str, model_id: str, timeout_s: float,
                  params: Dict[str, Any], key_ref: str) -> Dict[str, Any]:
    pid = uuid.uuid4().hex
    now = time.time()
    with _lock:
        _c().execute(
            "INSERT INTO presets (id, name, base_url, model_id, timeout_s, params, key_ref,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (pid, name, base_url, model_id, timeout_s, json.dumps(params), key_ref, now, now),
        )
        _c().commit()
    return get_preset(pid)


def get_preset(pid: str) -> Optional[Dict[str, Any]]:
    with _lock:
        row = _c().execute("SELECT * FROM presets WHERE id=?", (pid,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["params"] = json.loads(d.get("params") or "{}")
    return d


def list_presets() -> List[Dict[str, Any]]:
    with _lock:
        rows = _c().execute("SELECT * FROM presets ORDER BY name").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["params"] = json.loads(d.get("params") or "{}")
        out.append(d)
    return out


def update_preset(pid: str, **fields) -> None:
    if "params" in fields:
        fields["params"] = json.dumps(fields["params"])
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    with _lock:
        _c().execute(f"UPDATE presets SET {cols} WHERE id=?", (*fields.values(), pid))
        _c().commit()


def delete_preset(pid: str) -> None:
    with _lock:
        _c().execute("DELETE FROM presets WHERE id=?", (pid,))
        _c().commit()


# ---------------- settings ----------------

def get_setting(key: str, default: Any = None) -> Any:
    with _lock:
        row = _c().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    return json.loads(row["value"])


def set_setting(key: str, value: Any) -> None:
    with _lock:
        _c().execute(
            "INSERT INTO settings (key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        _c().commit()
