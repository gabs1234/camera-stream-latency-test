# Camera stream latency

**Disclaimer: AI-generated code.**

Measure direct MJPEG stream latency by pointing the camera at timestamped QR codes
on another computer's screen.

## Install on both computers

Requires Python 3.11+ and a Linux desktop.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
```

## Configure the receiver

Copy `.env.example` to `.env` if needed, then edit:

```dotenv
CAMERA_URL='https://CAMERA_HOST/stream.mjpg'
CAMERA_USERNAME='viewer'
CAMERA_PASSWORD='your-password'
```

Use your camera's multipart MJPEG URL; each frame must include `Content-Length`.

`.env` is ignored by Git. Run from its directory. CLI options override configuration.
For HTTP Basic auth use `--auth basic`; for a private CA use `--ca-file camera-ca.pem`.

## Run

**First synchronize both computers to the same NTP server.** With chrony, check
`chronyc sources -v` and `chronyc tracking` on both: the intended source should be
selected and clock offsets small compared with the latency you want to measure.

In the camera room, point the camera at the source screen:

```bash
python -m camera_latency emit --interval-ms 200 --visible-ms 150
```

In the receiving room:

```bash
python -m camera_latency measure --output measurements.csv
```

Escape exits. The receiver shows video, latency, fps and drops; on exit it reports
count, median, p95, minimum and maximum. Use fresh output filenames for each run.
Add `--headless --duration 60` for a timed run without a window.

## Read the results

`arrival_latency_ms` measures scheduled QR time to complete JPEG availability in
the receiver, before decoding and recognition. Only the first successful detection
of each marker enters the statistics. Slow processing drops waiting frames.

The source writes `source-timing.csv`. To examine scheduling offsets, copy it to
the receiver and join the logs:

```bash
python -m camera_latency report --measurements measurements.csv \
  --source-log source-timing.csv --output joined.csv
```

Clock offset directly biases latency. Screen refresh, pixel response, camera
exposure and failed detections limit accuracy; nanosecond timestamps do not mean
nanosecond accuracy. Short flashes may be missed; increase visibility if needed.
Delivered fps can differ from requested fps. The browser player may buffer further.
Clock jumps are flagged and negative values preserved. Source logs record rendering
submission, not physical pixel visibility; joining logs cannot remove that uncertainty.

Tests: `python -m pytest -q`. More options: `python -m camera_latency measure --help`.
