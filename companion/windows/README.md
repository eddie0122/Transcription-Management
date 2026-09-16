# Windows capture and voice companion

Runs natively on the Windows host and provides what the Docker/WSL2 backend
cannot reach from a Linux container:

- **Computer audio** — WASAPI loopback (everything Windows is playing:
  meeting audio, videos, other applications), streamed into the active Real
  Time or Talkie session over `/ws/ingest`.
- **Windows voices** — text-to-speech with the SAPI voices installed on
  Windows, returned as WAV over `/ws/companion` so the browser can speak
  Talkie translations and route each direction to its own output device.

## Install (on Windows, not inside WSL2)

```powershell
cd companion\windows
py -3 -m pip install -r requirements.txt
```

`pywin32` provides SAPI access; without it the companion still captures
audio but refuses voice requests with a clear message.

## Use

1. Open the app UI (`http://localhost:8080`).
2. Run the companion (before or after starting a session):

   ```powershell
   py -3 capture_companion.py --url http://localhost:8080
   ```

   It fetches the per-run session token from `/api/session` (pairing),
   publishes the installed Windows voices, and streams 16 kHz mono PCM to
   `/ws/ingest` through the published proxy.
3. **Real Time**: choose source **Computer Audio** and press **Start**.
   **Talkie**: pick the language pair and press **Start**; select
   *Windows voice (capture companion)* under Voice output.
4. Stop the session from the UI (**Stop**). Ctrl+C stops the companion.

Reconnect behavior: both connections retry every few seconds. If the audio
stream drops, the session stays open and the timeline stays aligned; audio
played while disconnected is not captured and appears as a silent gap. Voice
requests made while the control channel is down fail with "companion not
connected" and the browser marks the row *Not spoken*.

No extra Windows permission prompt is required for loopback capture or SAPI;
microphone capture goes through the browser and its permission prompt.

**Status:** delivered as the required native Windows companion, not yet
verified on Windows hardware — the backend side of both protocols was
exercised with a simulated companion; see
[docs/TEST_REPORT.md](../../docs/TEST_REPORT.md).
