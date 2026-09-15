"""Windows computer-audio capture companion.

Captures WASAPI loopback audio (what Windows is playing) natively on the
Windows host and streams it into the backend's active live session through
the published frontend proxy, authenticated with the per-run session token.

Usage (on Windows, outside WSL2/Docker):
    py -3 -m pip install -r requirements.txt
    py -3 capture_companion.py --url http://localhost:8080

Workflow:
  1. Start the app UI, choose Real Time → source "Computer Audio", press
     Start. The backend waits for companion audio.
  2. Run this script. It fetches the session token from /api/session and
     streams loopback audio to /ws/ingest until stopped (Ctrl+C) or the
     session ends.

On Ctrl+C the socket closes cleanly; the backend finalizes buffered speech
when you press Stop in the UI. If the connection drops, the recording and
transcript keep their shared timeline — reconnect and streaming resumes into
the same session.

STATUS: delivered as the required native Windows companion implementation,
but not yet verified on Windows hardware (see docs/TEST_REPORT.md).
"""
import argparse
import asyncio
import signal
import sys

import httpx
import numpy as np
import soundcard as sc
import websockets

SAMPLE_RATE = 16000
FRAME_S = 0.25


async def run(base_url: str) -> int:
    try:
        token = httpx.get(base_url.rstrip("/") + "/api/session", timeout=10).json()["token"]
    except Exception as e:
        print(f"Cannot reach the app at {base_url}: {e}", file=sys.stderr)
        return 2
    ws_url = base_url.rstrip("/").replace("http", "ws", 1) + f"/ws/ingest?token={token}"

    speaker = sc.default_speaker()
    if speaker is None:
        print("No default speaker found; cannot open loopback capture.", file=sys.stderr)
        return 2
    loopback = sc.get_microphone(speaker.name, include_loopback=True)
    print(f"Capturing loopback of: {speaker.name}")
    stop = asyncio.Event()
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_event_loop().add_signal_handler(s, stop.set)
        except NotImplementedError:  # Windows event loop
            signal.signal(s, lambda *_: stop.set())

    frames = int(SAMPLE_RATE * FRAME_S)
    async with websockets.connect(ws_url, max_size=None) as ws:
        print("Connected — streaming. Press Ctrl+C to stop the companion; "
              "press Stop in the UI to end the session.")
        with loopback.recorder(samplerate=SAMPLE_RATE, channels=1, blocksize=frames) as rec:
            while not stop.is_set():
                data = await asyncio.get_event_loop().run_in_executor(
                    None, rec.record, frames)
                pcm = np.clip(data[:, 0], -1.0, 1.0)
                await ws.send((pcm * 32767).astype("<i2").tobytes())
    print("Stopped.")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Windows computer-audio capture companion")
    ap.add_argument("--url", default="http://localhost:8080",
                    help="Base URL of the app UI (published frontend proxy)")
    args = ap.parse_args()
    sys.exit(asyncio.get_event_loop().run_until_complete(run(args.url)))


if __name__ == "__main__":
    main()
