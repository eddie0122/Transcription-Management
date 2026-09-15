"""Computer-audio capture on macOS via the bundled ScreenCaptureKit helper.

The helper (native/macos-capture) writes s16le 16 kHz mono PCM to stdout.
It requires the Screen & System Audio Recording permission; a denial or
missing helper surfaces as a clear unavailable state, never a silent one.
"""
import asyncio
import contextlib
import logging
import os
from pathlib import Path
from typing import Optional

from . import config

log = logging.getLogger("capture")


def helper_status() -> dict:
    """mode: 'native' (backend spawns the macOS helper), 'companion' (a
    Windows-host companion streams into /ws/ingest), or 'unavailable'."""
    path = Path(config.MACOS_CAPTURE_BIN)
    if not config.IS_MACOS:
        return {"available": True, "mode": "companion",
                "reason": "A Linux container cannot capture the Windows speaker "
                          "output directly. Run the native Windows capture "
                          "companion (companion/windows) on the host; it streams "
                          "WASAPI loopback audio into the session."}
    if not (path.is_file() and os.access(path, os.X_OK)):
        return {"available": False, "mode": "unavailable",
                "reason": "The system-audio capture helper is not built. Run "
                          "scripts/macos/setup.sh (it compiles native/macos-capture), "
                          "then grant Screen & System Audio Recording permission when "
                          "prompted."}
    return {"available": True, "mode": "native", "binary": str(path)}


class SystemAudioCapture:
    """Spawns the helper and forwards PCM frames to a callback."""

    def __init__(self, on_pcm, on_error):
        self.on_pcm = on_pcm
        self.on_error = on_error
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        status = helper_status()
        if not status["available"]:
            raise RuntimeError(status["reason"])
        self.proc = await asyncio.create_subprocess_exec(
            status["binary"],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._task = asyncio.get_event_loop().create_task(self._pump())

    async def _pump(self) -> None:
        assert self.proc is not None
        frame_bytes = config.SAMPLE_RATE * 2 // 4  # 250 ms
        try:
            while True:
                chunk = await self.proc.stdout.read(frame_bytes)
                if not chunk:
                    break
                await self.on_pcm(chunk)
        finally:
            rc = await self.proc.wait()
            if rc not in (0, -15):
                err = (await self.proc.stderr.read()).decode(errors="replace")[-400:]
                await self.on_error(
                    "Computer-audio capture stopped unexpectedly "
                    f"(exit {rc}). If this is a permission problem, enable Screen & "
                    f"System Audio Recording for this app in System Settings → "
                    f"Privacy & Security. Details: {err}")

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            if self.proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    self.proc.kill()
        if self._task:
            with contextlib.suppress(asyncio.CancelledError):
                self._task.cancel()
