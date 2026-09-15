"""Export writers: UTF-8 TXT (original/translated), SRT, VTT, and JSON.

Translated exports inherit the source segment timings; translation creates no
new audio alignment. Subtitle cues are validated for order and duration.
Partial exports are clearly marked in the accompanying job metadata.
"""
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def _ts_srt(ms: int) -> str:
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def _ts_vtt(ms: int) -> str:
    return _ts_srt(ms).replace(",", ".")


def _validated_cues(segments: List[Dict[str, Any]], field: str) -> List[Dict[str, Any]]:
    """Monotonic, non-empty cues with a minimum visible duration."""
    cues = []
    last_end = 0
    for seg in segments:
        text = (seg.get(field) or "").strip()
        if not text:
            continue
        start = max(int(seg["start_ms"]), last_end)
        end = max(int(seg["end_ms"]), start + 200)
        cues.append({"start_ms": start, "end_ms": end, "text": text})
        last_end = end
    return cues


def write_txt(segments: List[Dict[str, Any]], path: Path, field: str) -> Optional[Path]:
    lines = [(s.get(field) or "").strip() for s in segments]
    lines = [ln for ln in lines if ln]
    if not lines:
        return None
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_srt(segments: List[Dict[str, Any]], path: Path, field: str) -> Optional[Path]:
    cues = _validated_cues(segments, field)
    if not cues:
        return None
    out = []
    for i, c in enumerate(cues, 1):
        out.append(f"{i}\n{_ts_srt(c['start_ms'])} --> {_ts_srt(c['end_ms'])}\n{c['text']}\n")
    path.write_text("\n".join(out), encoding="utf-8")
    return path


def write_vtt(segments: List[Dict[str, Any]], path: Path, field: str) -> Optional[Path]:
    cues = _validated_cues(segments, field)
    if not cues:
        return None
    out = ["WEBVTT", ""]
    for c in cues:
        out.append(f"{_ts_vtt(c['start_ms'])} --> {_ts_vtt(c['end_ms'])}\n{c['text']}\n")
    path.write_text("\n".join(out), encoding="utf-8")
    return path


def write_json(job: Dict[str, Any], segments: List[Dict[str, Any]], path: Path,
               partial: bool = False) -> Path:
    doc = {
        "job_id": job["id"],
        "kind": job["kind"],
        "display_name": job["display_name"],
        "created_at": job["created_at"],
        "exported_at": time.time(),
        "partial": partial,
        "settings": job.get("settings", {}),
        "media": job.get("media", {}),
        "segments": [
            {
                "id": s["seg_id"],
                "index": s["seg_index"],
                "start_ms": s["start_ms"],
                "end_ms": s["end_ms"],
                "text": s["text"],
                "translation": s.get("translation"),
                "translation_status": s.get("translation_status", "none"),
                "translation_error": s.get("translation_error", ""),
            }
            for s in segments
        ],
    }
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def export_all(job: Dict[str, Any], segments: List[Dict[str, Any]], job_dir: Path,
               base_name: str, target_lang: Optional[str],
               partial: bool = False) -> List[Dict[str, Any]]:
    """Write all applicable exports; returns artifact descriptors
    [{kind, label, file}] with collision-safe internal filenames."""
    artifacts: List[Dict[str, Any]] = []

    def add(kind: str, label: str, path: Optional[Path]):
        if path is not None and path.is_file():
            artifacts.append({"kind": kind, "label": label, "file": path.name,
                              "bytes": path.stat().st_size})

    add("original_txt", f"{base_name}.original.txt",
        write_txt(segments, job_dir / "original.txt", "text"))
    add("original_srt", f"{base_name}.original.srt",
        write_srt(segments, job_dir / "original.srt", "text"))
    add("original_vtt", f"{base_name}.original.vtt",
        write_vtt(segments, job_dir / "original.vtt", "text"))
    if target_lang:
        add("translated_txt", f"{base_name}.{target_lang}.txt",
            write_txt(segments, job_dir / "translated.txt", "translation"))
        add("translated_srt", f"{base_name}.{target_lang}.srt",
            write_srt(segments, job_dir / "translated.srt", "translation"))
        add("translated_vtt", f"{base_name}.{target_lang}.vtt",
            write_vtt(segments, job_dir / "translated.vtt", "translation"))
    add("segments_json", f"{base_name}.segments.json",
        write_json(job, segments, job_dir / "segments.json", partial=partial))
    return artifacts
