"""Media probing and audio extraction/normalization.

Decoder preference order:
  1. ffmpeg/ffprobe (full format matrix, audio-track selection, streamed decode)
  2. afconvert/afinfo (macOS built-in CoreAudio; default track only)
  3. Pure-Python WAV reader (stdlib; PCM WAV only)

The tested container matrix is published in the deployment guides per
decoder. Input files are never modified; extraction writes a new WAV
normalized to 16 kHz mono s16le as required by both engines.
"""
import json
import re
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
AFCONVERT = shutil.which("afconvert") if config.IS_MACOS else None
AFINFO = shutil.which("afinfo") if config.IS_MACOS else None

SUPPORTED_EXTENSIONS = [
    ".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".mp4", ".mov", ".mkv", ".webm",
]


class MediaError(Exception):
    """User-facing media problem (unreadable file, no audio, unsupported codec)."""


def decoder_info() -> Dict[str, Any]:
    return {
        "ffmpeg": bool(FFMPEG),
        "afconvert": bool(AFCONVERT),
        "wav_fallback": True,
        "active": "ffmpeg" if FFMPEG else ("afconvert" if AFCONVERT else "wav-only"),
    }


def probe(path: Path) -> Dict[str, Any]:
    """Validate actual content (not extension) and return media info:
    {duration_s, container, audio_tracks: [{index, codec, channels, sample_rate,
    language, default}], has_video}."""
    if FFPROBE:
        return _probe_ffprobe(path)
    if AFINFO:
        return _probe_afinfo(path)
    return _probe_wav(path)


def _probe_ffprobe(path: Path) -> Dict[str, Any]:
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        raise MediaError(f"Unreadable or unsupported media file: {r.stderr.strip()[:300]}")
    info = json.loads(r.stdout or "{}")
    fmt = info.get("format", {})
    streams = info.get("streams", [])
    audio, has_video = [], False
    a_index = 0
    for s in streams:
        if s.get("codec_type") == "audio":
            audio.append({
                "index": a_index,
                "codec": s.get("codec_name", "unknown"),
                "channels": s.get("channels", 0),
                "sample_rate": int(s.get("sample_rate") or 0),
                "language": (s.get("tags") or {}).get("language", ""),
                "default": bool((s.get("disposition") or {}).get("default", 0)) or a_index == 0,
            })
            a_index += 1
        elif s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic", 0) == 0:
            has_video = True
    duration = float(fmt.get("duration") or 0) or None
    if not audio:
        kind = "video" if has_video else "file"
        raise MediaError(f"This {kind} contains no audio track.")
    return {
        "duration_s": duration,
        "container": fmt.get("format_name", ""),
        "audio_tracks": audio,
        "has_video": has_video,
        "decoder": "ffmpeg",
    }


def _probe_afinfo(path: Path) -> Dict[str, Any]:
    r = subprocess.run([AFINFO, str(path)], capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or "estimated duration" not in r.stdout:
        raise MediaError(
            "Unreadable or unsupported media file for the CoreAudio decoder. "
            "Install ffmpeg (brew install ffmpeg) for the full format matrix."
        )
    m = re.search(r"estimated duration:\s*([\d.]+)", r.stdout)
    duration = float(m.group(1)) if m else None
    ch = re.search(r"(\d+)\s+ch", r.stdout)
    sr = re.search(r"(\d+)\s+Hz", r.stdout)
    fmt = re.search(r"File type ID:\s*(\S+)", r.stdout)
    return {
        "duration_s": duration,
        "container": (fmt.group(1) if fmt else "").strip(),
        "audio_tracks": [{
            "index": 0, "codec": "coreaudio",
            "channels": int(ch.group(1)) if ch else 0,
            "sample_rate": int(sr.group(1)) if sr else 0,
            "language": "", "default": True,
        }],
        "has_video": False,
        "decoder": "afconvert",
    }


def _probe_wav(path: Path) -> Dict[str, Any]:
    try:
        with wave.open(str(path), "rb") as w:
            frames, rate = w.getnframes(), w.getframerate()
            return {
                "duration_s": frames / float(rate) if rate else None,
                "container": "wav",
                "audio_tracks": [{
                    "index": 0, "codec": "pcm", "channels": w.getnchannels(),
                    "sample_rate": rate, "language": "", "default": True,
                }],
                "has_video": False,
                "decoder": "wav",
            }
    except (wave.Error, EOFError, OSError) as e:
        raise MediaError(
            f"Cannot decode this file ({e}). Only PCM WAV is supported without "
            "ffmpeg; install ffmpeg for MP3/M4A/video formats."
        )


def extract_audio(src: Path, dst_wav: Path, track_index: int = 0) -> None:
    """Extract/normalize audio to 16 kHz mono s16le WAV without modifying the
    source. Uses streamed decoding (ffmpeg/afconvert stream internally)."""
    if FFMPEG:
        r = subprocess.run(
            [FFMPEG, "-nostdin", "-hide_banner", "-y", "-i", str(src),
             "-map", f"0:a:{track_index}", "-vn", "-ac", "1",
             "-ar", str(config.SAMPLE_RATE), "-c:a", "pcm_s16le", str(dst_wav)],
            capture_output=True, text=True, timeout=3600,
        )
        if r.returncode != 0:
            raise MediaError(f"Audio extraction failed: {tail(r.stderr)}")
        return
    if AFCONVERT:
        if track_index != 0:
            raise MediaError(
                "Selecting a non-default audio track requires ffmpeg "
                "(brew install ffmpeg)."
            )
        base_cmd = [AFCONVERT, str(src), str(dst_wav), "-f", "WAVE",
                    "-d", f"LEI16@{config.SAMPLE_RATE}"]
        # --mix downmixes multichannel audio but errors on mono input, so
        # retry without it for mono sources.
        r = subprocess.run(base_cmd + ["-c", "1", "--mix"],
                           capture_output=True, text=True, timeout=3600)
        if r.returncode != 0:
            r = subprocess.run(base_cmd, capture_output=True, text=True, timeout=3600)
        if r.returncode != 0:
            raise MediaError(f"Audio conversion failed (afconvert): {tail(r.stderr)}")
        with wave.open(str(dst_wav), "rb") as w:
            if w.getnchannels() != 1:
                raise MediaError("Could not downmix this file to mono with the "
                                 "CoreAudio decoder; install ffmpeg (brew install ffmpeg).")
        return
    _wav_to_wav(src, dst_wav)


def _wav_to_wav(src: Path, dst: Path) -> None:
    """Stdlib fallback: PCM WAV -> 16 kHz mono s16le, chunked to bound memory."""
    try:
        import audioop  # deprecated but present through Python 3.12
    except ImportError:
        raise MediaError("No audio decoder available; install ffmpeg.")
    try:
        with wave.open(str(src), "rb") as win, wave.open(str(dst), "wb") as wout:
            n_ch, width, rate = win.getnchannels(), win.getsampwidth(), win.getframerate()
            wout.setnchannels(1)
            wout.setsampwidth(2)
            wout.setframerate(config.SAMPLE_RATE)
            state = None
            chunk_frames = rate  # ~1 s per read
            while True:
                data = win.readframes(chunk_frames)
                if not data:
                    break
                if width != 2:
                    data = audioop.lin2lin(data, width, 2)
                if n_ch > 1:
                    data = audioop.tomono(data, 2, 0.5, 0.5)
                if rate != config.SAMPLE_RATE:
                    data, state = audioop.ratecv(data, 2, 1, rate, config.SAMPLE_RATE, state)
                wout.writeframes(data)
    except (wave.Error, EOFError, OSError) as e:
        raise MediaError(f"Cannot decode WAV file: {e}")


def wav_duration_s(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        return w.getnframes() / float(rate) if rate else 0.0


def tail(text: str, n: int = 400) -> str:
    text = (text or "").strip()
    return text[-n:]
