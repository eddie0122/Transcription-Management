"""Text-to-speech synthesis engines for Talkie mode.

Engines return WAV bytes so the browser can play them on a chosen output
device (per direction). The browser's own speechSynthesis is a third engine
handled entirely client-side and is not represented here.

  - macos_say:  the `say` command on native macOS (system voices).
  - companion:  the Windows capture companion, which synthesizes with the
                Windows SAPI voices and streams the WAV back over /ws/companion
                (the Linux container has no access to Windows audio).

Text is passed to `say` on stdin, never through a shell or as an argument.
"""
import asyncio
import logging
import os
import re
import secrets
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

from . import config

log = logging.getLogger("tts")

MAX_TEXT_CHARS = 1000
SAY_TIMEOUT_S = 30
COMPANION_TIMEOUT_S = 20
SAY_BIN = "/usr/bin/say"

# Novelty/effect voices are listed last so a language's default is a normal one.
_NOVELTY = {"Bad News", "Bahh", "Bells", "Boing", "Bubbles", "Cellos", "Good News",
            "Jester", "Organ", "Superstar", "Trinoids", "Whisper", "Wobble", "Zarvox",
            "Albert", "Fred", "Junior", "Kathy", "Ralph"}


class TTSError(Exception):
    """category: unavailable | input | synthesis"""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


def _lang_of(locale_code: str) -> str:
    return re.split(r"[_-]", locale_code)[0].lower()


def validate_text(text: Any) -> str:
    if not isinstance(text, str) or not text.strip():
        raise TTSError("input", "Nothing to speak: the text is empty.")
    if len(text) > MAX_TEXT_CHARS:
        raise TTSError("input", f"Text exceeds the {MAX_TEXT_CHARS}-character limit for one "
                                "utterance.")
    return text.strip()


# ---------------------------------------------------------------- macOS say

_say_cache: Dict[str, Any] = {}
_say_lock = threading.Lock()


def say_available() -> bool:
    return config.IS_MACOS and os.access(SAY_BIN, os.X_OK)


def say_voices(force: bool = False) -> List[Dict[str, str]]:
    """Installed macOS voices from `say -v ?` as {id, label, locale, language}."""
    if not say_available():
        return []
    with _say_lock:
        if _say_cache.get("voices") is not None and not force:
            return _say_cache["voices"]
        try:
            out = subprocess.run([SAY_BIN, "-v", "?"], capture_output=True, text=True,
                                 timeout=15).stdout
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("say -v ? failed: %s", e)
            return []
        voices = []
        for line in out.splitlines():
            # "<name possibly with spaces/parentheses>  <ll_RR>    # sample"
            m = re.match(r"^(.*?)\s{2,}([a-zA-Z]{2,3}[_-][A-Za-z0-9_]+)\s+#", line)
            if not m:
                continue
            name, loc = m.group(1).strip(), m.group(2)
            voices.append({"id": name, "label": f"{name} ({loc})", "locale": loc,
                           "language": _lang_of(loc)})
        voices.sort(key=lambda v: (v["language"], v["id"].split(" (")[0] in _NOVELTY,
                                   v["id"]))
        _say_cache["voices"] = voices
        return voices


def say_synthesize(text: str, voice: Optional[str], rate: Optional[float]) -> bytes:
    if not say_available():
        raise TTSError("unavailable", "The macOS system voice is only available in the "
                                      "native macOS deployment.")
    voices = say_voices()
    if voice and not any(v["id"] == voice for v in voices):
        raise TTSError("input", f"Unknown macOS voice '{voice}'.")
    tmp = config.TMP_DIR / f"tts-{time.monotonic_ns()}-{secrets.token_hex(4)}.wav"
    cmd = [SAY_BIN, "-o", str(tmp), "--data-format=LEI16@22050", "-f", "-"]
    if voice:
        cmd += ["-v", voice]
    if rate:
        # `rate` is a multiplier (1.0 = normal); say expects words per minute.
        cmd += ["-r", str(int(max(90, min(400, 175 * float(rate)))))]
    try:
        proc = subprocess.run(cmd, input=text, capture_output=True, text=True,
                              timeout=SAY_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        raise TTSError("synthesis", f"Speech synthesis timed out after {SAY_TIMEOUT_S}s.")
    except OSError as e:
        raise TTSError("synthesis", f"Could not run the say command: {e}")
    try:
        if proc.returncode != 0 or not tmp.is_file():
            raise TTSError("synthesis", "Speech synthesis failed: "
                                        f"{(proc.stderr or '').strip()[-300:] or 'no output'}")
        return tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------- companion

class CompanionRegistry:
    """The single connected Windows companion that can synthesize speech.

    The websocket handler registers itself with a `send` coroutine and the
    voices announced in the companion's hello message; synthesize() awaits
    the binary reply matched by request id."""

    def __init__(self) -> None:
        self.send = None
        self.voices: List[Dict[str, str]] = []
        self.platform = ""
        self.connected_at: Optional[float] = None
        self._pending: Dict[str, "asyncio.Future[bytes]"] = {}
        self._token = 0

    def register(self, send, voices: List[Dict[str, Any]], platform: str) -> int:
        self._fail_all("The capture companion reconnected.")
        self.send = send
        self.platform = platform
        self.voices = [
            {"id": str(v.get("id") or v.get("name") or ""),
             "label": str(v.get("label") or v.get("name") or v.get("id") or ""),
             "locale": str(v.get("locale") or ""),
             "language": str(v.get("language") or _lang_of(str(v.get("locale") or "")))}
            for v in voices if isinstance(v, dict) and (v.get("id") or v.get("name"))]
        self.connected_at = time.time()
        self._token += 1
        return self._token

    def unregister(self, token: int) -> None:
        if token != self._token:
            return  # a newer companion replaced this one
        self.send = None
        self.voices = []
        self.platform = ""
        self.connected_at = None
        self._fail_all("The capture companion disconnected.")

    def available(self) -> bool:
        return self.send is not None

    def _fail_all(self, message: str) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(TTSError("unavailable", message))
        self._pending.clear()

    def resolve(self, req_id: str, wav: bytes) -> None:
        fut = self._pending.pop(req_id, None)
        if fut and not fut.done():
            fut.set_result(wav)

    def reject(self, req_id: str, message: str) -> None:
        fut = self._pending.pop(req_id, None)
        if fut and not fut.done():
            fut.set_exception(TTSError("synthesis", message))

    async def synthesize(self, text: str, voice: Optional[str], rate: Optional[float]) -> bytes:
        if self.send is None:
            raise TTSError("unavailable",
                           "The Windows capture companion is not connected. Run "
                           "companion/windows/capture_companion.py on the Windows host; it "
                           "provides the Windows voices.")
        if voice and self.voices and not any(v["id"] == voice for v in self.voices):
            raise TTSError("input", f"Unknown companion voice '{voice}'.")
        req_id = secrets.token_hex(16)
        fut: "asyncio.Future[bytes]" = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        try:
            await self.send({"type": "tts", "id": req_id, "text": text,
                             "voice": voice or "", "rate": rate})
            return await asyncio.wait_for(fut, timeout=COMPANION_TIMEOUT_S)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise TTSError("synthesis", "The companion did not return speech within "
                                        f"{COMPANION_TIMEOUT_S}s.")
        except Exception as e:
            self._pending.pop(req_id, None)
            if isinstance(e, TTSError):
                raise
            raise TTSError("unavailable", f"Companion request failed: {e}")


companion = CompanionRegistry()


# ---------------------------------------------------------------- API surface

def engines_info() -> Dict[str, Any]:
    return {
        "browser": {"available": True,
                    "detail": "Voices installed in this browser/OS; plays on the default "
                              "output device (no per-direction routing)."},
        "macos_say": {"available": say_available(),
                      "detail": "macOS system voices; returns audio the browser can route "
                                "to a chosen output device." if say_available() else
                                "Only in the native macOS deployment.",
                      "voices": say_voices()},
        "companion": {"available": companion.available(),
                      "detail": "Windows voices via the capture companion; routable per "
                                "direction." if companion.available() else
                                "Start companion/windows/capture_companion.py on the "
                                "Windows host to use Windows voices.",
                      "voices": companion.voices},
    }


async def synthesize(engine: str, text: Any, voice: Optional[str],
                     rate: Optional[float]) -> bytes:
    text = validate_text(text)
    if engine == "macos_say":
        return await asyncio.to_thread(say_synthesize, text, voice, rate)
    if engine == "companion":
        return await companion.synthesize(text, voice, rate)
    raise TTSError("input", f"Unknown TTS engine '{engine}'. Use macos_say or companion "
                            "(the browser voice is synthesized client-side).")
