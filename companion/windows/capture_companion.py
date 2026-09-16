"""Windows companion: computer-audio capture and Windows voices.

Runs natively on the Windows host (outside WSL2/Docker) and gives the
containerized backend what a Linux container cannot reach:

  1. Computer audio — WASAPI loopback (what Windows is playing, e.g. the
     call) streamed into the backend's active live/talkie session through
     the published frontend proxy (/ws/ingest).
  2. Windows voices — text-to-speech with the SAPI voices installed on
     Windows, returned as WAV over the control channel (/ws/companion) so
     the browser can play each Talkie direction on its own output device.

Both connections authenticate with the per-run session token from
/api/session.

Usage (on Windows, outside WSL2/Docker):
    py -3 -m pip install -r requirements.txt
    py -3 capture_companion.py --url http://localhost:8080

Workflow:
  1. Start the app UI. Optionally start this script first so the Windows
     voices appear under Talkie → Voice output.
  2. Choose Real Time → "Computer Audio" or the Talkie tab, press Start.
  3. Loopback audio streams until stopped (Ctrl+C) or the app closes; the
     control channel reconnects automatically.

STATUS: delivered as the required native Windows companion implementation,
but not yet verified on Windows hardware (see docs/TEST_REPORT.md).
"""
import argparse
import asyncio
import json
import locale
import os
import signal
import sys
import tempfile

import httpx
import numpy as np
import soundcard as sc
import websockets

SAMPLE_RATE = 16000
FRAME_S = 0.25
RECONNECT_S = 3.0


# ----------------------------------------------------------------- SAPI TTS

def _lcid_to_locale(value: str) -> str:
    """SAPI reports Language as hex LCID(s), e.g. '412' (ko-KR) or '409;809'."""
    first = (value or "").split(";")[0].strip()
    try:
        return locale.windows_locale.get(int(first, 16), "")
    except ValueError:
        return ""


def sapi_voices():
    """Installed SAPI voices as {id, label, locale, language}."""
    try:
        import win32com.client  # type: ignore
        voice = win32com.client.Dispatch("SAPI.SpVoice")
    except Exception as e:  # pywin32 missing or no SAPI
        print(f"Windows voices unavailable ({e}); TTS requests will be refused.",
              file=sys.stderr)
        return []
    out = []
    for token in voice.GetVoices():
        name = token.GetDescription()
        try:
            loc = _lcid_to_locale(token.GetAttribute("Language"))
        except Exception:
            loc = ""
        out.append({"id": name, "label": f"{name} ({loc or 'unknown locale'})",
                    "locale": loc, "language": loc.split("_")[0].lower() if loc else ""})
    return out


def sapi_synthesize(text: str, voice_name: str, rate) -> bytes:
    """Synthesize to a 22 kHz 16-bit mono WAV file and return its bytes."""
    import win32com.client  # type: ignore
    voice = win32com.client.Dispatch("SAPI.SpVoice")
    if voice_name:
        for token in voice.GetVoices():
            if token.GetDescription() == voice_name:
                voice.Voice = token
                break
        else:
            raise ValueError(f"Unknown Windows voice '{voice_name}'.")
    if rate:
        # SAPI rate is -10..10 around normal; `rate` is a multiplier (1.0 = normal).
        voice.Rate = max(-10, min(10, int(round((float(rate) - 1.0) * 10))))
    stream = win32com.client.Dispatch("SAPI.SpFileStream")
    fmt = win32com.client.Dispatch("SAPI.SpAudioFormat")
    fmt.Type = 22  # SAFT22kHz16BitMono
    stream.Format = fmt
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        stream.Open(path, 3, False)  # SSFMCreateForWrite
        voice.AudioOutputStream = stream
        voice.Speak(text, 0)
        stream.Close()
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ----------------------------------------------------------------- channels

async def control_channel(url_for, stop: asyncio.Event) -> None:
    """Announce voices and answer TTS requests; reconnects until stopped."""
    voices = sapi_voices()
    print(f"Windows voices available: {len(voices)}")
    loop = asyncio.get_event_loop()
    while not stop.is_set():
        try:
            async with websockets.connect(url_for("/ws/companion"), max_size=None) as ws:
                await ws.send(json.dumps({"type": "hello", "platform": "windows",
                                          "voices": voices}))
                print("Control channel connected (Windows voices published).")
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    if isinstance(raw, bytes):
                        continue
                    msg = json.loads(raw)
                    if msg.get("type") != "tts":
                        continue
                    req_id = str(msg.get("id") or "")
                    try:
                        wav = await loop.run_in_executor(
                            None, sapi_synthesize, str(msg.get("text") or ""),
                            str(msg.get("voice") or ""), msg.get("rate"))
                        await ws.send(req_id.encode("ascii") + wav)
                    except Exception as e:
                        await ws.send(json.dumps({"type": "tts_error", "id": req_id,
                                                  "message": str(e)}))
        except (OSError, websockets.WebSocketException) as e:
            print(f"Control channel disconnected ({e}); retrying in {RECONNECT_S:.0f}s.")
        if not stop.is_set():
            await asyncio.sleep(RECONNECT_S)


async def capture_channel(url_for, stop: asyncio.Event) -> int:
    """Stream loopback audio into the active session; reconnects until stopped."""
    speaker = sc.default_speaker()
    if speaker is None:
        print("No default speaker found; cannot open loopback capture.", file=sys.stderr)
        return 2
    loopback = sc.get_microphone(speaker.name, include_loopback=True)
    print(f"Capturing loopback of: {speaker.name}")
    frames = int(SAMPLE_RATE * FRAME_S)
    loop = asyncio.get_event_loop()
    while not stop.is_set():
        try:
            async with websockets.connect(url_for("/ws/ingest"), max_size=None) as ws:
                print("Streaming computer audio. Press Ctrl+C to stop the companion; "
                      "press Stop in the UI to end the session.")
                with loopback.recorder(samplerate=SAMPLE_RATE, channels=1, blocksize=frames) as rec:
                    while not stop.is_set():
                        data = await loop.run_in_executor(None, rec.record, frames)
                        pcm = np.clip(data[:, 0], -1.0, 1.0)
                        await ws.send((pcm * 32767).astype("<i2").tobytes())
        except (OSError, websockets.WebSocketException) as e:
            print(f"Audio stream disconnected ({e}); retrying in {RECONNECT_S:.0f}s.")
        if not stop.is_set():
            await asyncio.sleep(RECONNECT_S)
    return 0


async def run(base_url: str) -> int:
    try:
        token = httpx.get(base_url.rstrip("/") + "/api/session", timeout=10).json()["token"]
    except Exception as e:
        print(f"Cannot reach the app at {base_url}: {e}", file=sys.stderr)
        return 2
    ws_base = base_url.rstrip("/").replace("http", "ws", 1)
    stop = asyncio.Event()
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_event_loop().add_signal_handler(s, stop.set)
        except NotImplementedError:  # Windows event loop
            signal.signal(s, lambda *_: stop.set())

    def url_for(path: str) -> str:
        return f"{ws_base}{path}?token={token}"

    results = await asyncio.gather(control_channel(url_for, stop),
                                   capture_channel(url_for, stop))
    print("Stopped.")
    return results[1] or 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Windows computer-audio capture and voice companion")
    ap.add_argument("--url", default="http://localhost:8080",
                    help="Base URL of the app UI (published frontend proxy)")
    args = ap.parse_args()
    sys.exit(asyncio.get_event_loop().run_until_complete(run(args.url)))


if __name__ == "__main__":
    main()
