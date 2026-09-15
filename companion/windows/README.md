# Windows computer-audio capture companion

Captures **WASAPI loopback** audio (everything Windows is playing — meeting
audio, videos, other applications) natively on the Windows host and streams
it into the Real Time session of the Docker/WSL2 deployment. A Linux
container cannot capture Windows audio directly; this companion is the
supported computer-audio path for that deployment.

## Install (on Windows, not inside WSL2)

```powershell
cd companion\windows
py -3 -m pip install -r requirements.txt
```

## Use

1. Open the app UI (`http://localhost:8080`), switch to **Real Time**,
   choose source **Computer Audio**, press **Start**. The session shows
   *Waiting for the Windows capture companion*.
2. Run the companion:

   ```powershell
   py -3 capture_companion.py --url http://localhost:8080
   ```

   It fetches the per-run session token from `/api/session` (pairing) and
   streams 16 kHz mono PCM to `/ws/ingest` through the published proxy.
3. Stop the session from the UI (**Stop**) — the backend flushes buffered
   speech and finalizes the recording. Ctrl+C stops the companion itself.

Reconnect behavior: if the companion disconnects, the session stays open and
the timeline stays aligned; re-run the companion to resume streaming. Audio
played while disconnected is not captured and appears as a silent gap.

No extra Windows permission prompt is required for loopback capture;
microphone capture (the other source) goes through the browser and its
permission prompt instead.

**Status:** delivered as the required native Windows companion, not yet
verified on Windows hardware — see [docs/TEST_REPORT.md](../../docs/TEST_REPORT.md).
