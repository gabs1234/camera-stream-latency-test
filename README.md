# Direct-stream camera latency

A small Linux Python application with a fullscreen timestamped QR source and an
Axis HTTP/HTTPS multipart MJPEG receiver. Run the same project on both computers.
It measures a software receive path, with explicit source and receiver timing
logs. It does **not** claim nanosecond measurement accuracy.

## Install

Python 3.11 or newer and a working desktop display are required for the GUI.
On each computer, from this directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m camera_latency --help
```

Alternatively, with uv installed: `uv sync --extra test`, then activate `.venv`.
The checked-in `uv.lock` records the tested dependency set. On minimal Linux
systems, SDL may need the distribution's X11/Wayland runtime libraries.

The dependencies are pygame (SDL window and events), qrcode + Pillow (sharp QR
images and JPEG decoding), zxing-cpp (native QR recognition with Python wheels),
and HTTPX (streaming HTTP, TLS, Basic/Digest authentication). No OpenCV video
capture, browser player, FFmpeg process, or hidden decoder frame queue is used.
The multipart framing code is small because the Axis endpoint supplies
`Content-Length` for every JPEG; streams omitting it are rejected.
See [Axis's stream format](https://developer.axis.com/vapix/network-video/video-streaming/),
[HTTPX streaming](https://www.python-httpx.org/quickstart/#streaming-responses),
[authentication](https://www.python-httpx.org/advanced/authentication/),
[zxing-cpp](https://github.com/zxing-cpp/zxing-cpp/tree/master/wrappers/python),
[qrcode](https://pypi.org/project/qrcode/), and
[pygame display](https://www.pygame.org/docs/ref/display.html).

## Synchronize first

Configure the two computers' existing time synchronization services to use the
same reliable NTP server, preferably a local server. The application never adjusts
clocks. With chrony, check **both computers**, before and after the experiment:

```bash
chronyc sources -v
chronyc tracking
chronyc waitsync 60 0.001
```

Look for the intended selected source (`*`), recent successful updates, normal
leap status, and small system correction, last/RMS offset, root delay and root
dispersion compared with the accuracy you need. The `waitsync` example waits for
up to 60 attempts and less than 1 ms remaining correction; it does not prove
1 ms absolute accuracy. Keep copies of the tracking outputs with your logs.
An active NTP service or matching displayed seconds alone is insufficient.
See [chrony's command reference](https://chrony.gitlab.io/doc/4.9/chronyc.html).

A receiver clock 10 ms ahead of the source adds 10 ms to every absolute latency.
A common NTP server reduces clock disagreement but does not remove network
asymmetry or uncertainty. For stronger claims use independently validated clock
synchronization, potentially PTP/hardware timing. Do any deliberate clock steps
before starting, then allow synchronization to settle.

## Run in the camera room

Point the camera at the source screen. Make the QR large and sharply focused in
the camera's field of view; avoid glare and clipping of the white margin.

```bash
python -m camera_latency emit --interval-ms 200 --visible-ms 150 \
  --source-log source-timing.csv
```

Escape exits the fullscreen application. The screen starts black, then shows a QR
on white with a four-module quiet zone, readable scheduled UTC time, session ID
and sequence number. It returns to black between presentations. Each run has a
random 64-bit session ID. The ASCII payload is:

```text
CL1:0123456789abcdef:42:1792497600000000000
```

`CL1` versions the format; the other fields are session, increasing decimal
sequence and unsigned 64-bit UTC Unix nanoseconds. Nanoseconds are a representation,
not an accuracy claim. Sequence gaps are expected when slots are skipped.

At 25–60 fps, 150 ms visible spans roughly 4–9 frame periods. The default 200 ms
interval gives a 50 ms black gap. These are practical starting values, not capture
guarantees. Visibility below 100 ms or a black gap below 20 ms triggers a warning.
Durations shorter than a camera frame period (40 ms at 25 fps, 16.7 ms at 60 fps),
exposure, or source refresh period can be missed or blended. Lengthen visibility
and interval if recognition is intermittent. Very short intervals also exceed QR
generation or display capacity; values below 1 ms are rejected. Actual delivered
fps is shown by the receiver and can differ from the camera's requested fps.

### Source timing semantics

The timestamp represents the **scheduled presentation time**, not generation
start. At startup, a wall/monotonic clock pair defines the schedule. The first slot
is 500 ms later; subsequent slots use integer monotonic deadlines, avoiding sleep
drift. The scheduled UTC value is extrapolated from that initial pair for the
whole session. It is not remapped after wall-clock adjustments.

A single worker prepares one QR ahead of presentation. The main thread prepares
its display surface and text in advance. If work is late, the source skips missed
slots; it never rapidly replays expired markers. A late marker still within its
visible window is shown for the remaining window, with lateness logged. Black
and presentation deadlines are monotonic; a busy OS can still delay them.

Source CSV events are `generated`, `presented`, `hidden`, and `skipped`.
`generated` records generation start/end (wall and monotonic) and duration.
`presented` repeats that information and adds scheduled monotonic time, `submit`
(immediately before `pygame.display.flip()` after drawing), `flip_done` (after the
call returns), and monotonic `schedule_error_ms`. `hidden` records black submission
and the actual interval between visible/black software submissions. Logs are keyed
by session and sequence. Unshown generated QRs may have no presentation event.

Neither submission nor flip return measures physical light onset. The compositor,
scanout, screen refresh and pixel response remain unknown. Even a blocking flip
does not establish when a particular pixel was visible.

## Run in the receiving room

```bash
python -m camera_latency measure \
  --url 'https://CAMERA_HOST/axis-cgi/mjpg/video.cgi' \
  --username viewer --output measurements.csv
```

The password is prompted without echo. Digest is the default; use `--auth basic`
for HTTP Basic authentication. Prefer HTTPS, especially with Basic. For unattended
use, `--password-env CAMERA_PASSWORD` reads that variable; the value is not printed.
Do not put credentials in the URL (embedded user/password are rejected).
URLs, passwords and exception bodies are never written to logs. A private CA can
be supplied with `--ca-file camera-ca.pem`; certificate verification stays enabled.
Automatic proxy/environment credential discovery and redirects are disabled.

You can request camera parameters in the URL, for example
`'?fps=30&resolution=1280x720'`, subject to camera support and permissions. This is
a request, not evidence of actual fps. The displayed rate is complete JPEGs
received divided by session elapsed time, including connection outages.

The live view outlines recognized markers, shows the latest result, statistics,
frame rate, drops and detection/connection status. Escape/window close or Ctrl-C
stops it and prints first-observed count, median, p95, minimum and maximum. For
headless use, add `--headless`; `--duration 60` stops after about 60 seconds.
Connection errors, including authentication errors, produce sanitized diagnostic
CSV events and retry after one second. `--timeout 5` limits network inactivity;
shutdown waits for the active read and current recognition to finish.
Outputs are created exclusively: use new filenames for each run.

### Receiver timing semantics

One thread continuously drains HTTPX `iter_raw()` without requesting a fixed-size
chunk. The multipart parser recognizes complete `Content-Length` JPEG payloads
without waiting for the next boundary. `arrival_wall_ns` / `arrival_mono_ns` are
sampled immediately after the complete JPEG is extracted by the parser, before
queue handoff, JPEG decoding, recognition or display. This is **application-level
complete-JPEG availability**, not exposure time, kernel packet arrival or first
byte arrival. TLS/HTTP/OS buffering, parser extraction/copy time and thread
scheduling before this point are included. A chunk may already contain several
frames; each gets a timestamp when extracted, not an invented network timestamp.

The handoff holds at most one waiting compressed frame, plus one in-flight frame
being processed. A new JPEG replaces any waiting JPEG. The processing thread
records `decode_start`, forces Pillow JPEG decoding/RGB conversion, records
`decoded`, and brackets native QR recognition with `qr_start` / `qr_done`.
Original arrival timestamps stay attached to their frame. The GUI has a separate
one-item latest-result slot. CSV writes are serialized and flushed per event;
slow storage or CPU starvation can still delay acquisition, so use a local disk
and inspect drops and processing times. There is no claim that buffering upstream
of this application is bounded by its one-frame slot.

For a valid QR, the CSV contains:

| Column | Meaning |
| --- | --- |
| `arrival_latency_ms` | arrival wall time minus encoded scheduled UTC; **main result** |
| `decoded_latency_ms` | decoded-image wall time minus marker UTC |
| `recognition_latency_ms` | recognition-completion wall time minus marker UTC |
| `queue_ms` | monotonic decode start minus original arrival |
| `decode_ms` | monotonic decoded-image completion minus decode start |
| `recognition_ms` | monotonic QR completion minus QR start |

All five timing points have raw wall/monotonic nanoseconds, clock epoch and jump
size columns. Duration calculations use monotonic time; cross-computer absolute
latency uses wall time. No assumed average computation time is subtracted.

CSV `acquired` events retain the arrival timestamp of **every** received JPEG.
`dropped` events identify overwritten waiting frames or the remaining waiting
frame discarded at shutdown; they have no decode or QR
timestamps because that work did not happen. `frame` events record processed
frames, including no-marker frames and decode errors. Multiple valid markers in
one frame produce multiple `frame` rows. Counts and `frames_dropped` are cumulative;
CSV order can interleave threads, so use `frame_id` to relate events.

`first_observation=1` occurs once per unique `(session, sequence)`, with
`sighting=1`; later sightings are retained as frame age and excluded from latency
statistics. Timestamp conflicts for the same identity are flagged. First-observed
means first successfully recognized **processed** frame, not necessarily the first
camera frame containing the marker. Sampling phase, queue drops, blur, partial
exposure and recognition failures can delay detection or remove a marker entirely.
This selection can bias results. Compare detection coverage against presented
source markers and review drop/error counts rather than just percentiles.
Statistics include negative and flagged values; a separate unflagged summary is
also printed at receiver exit. Percentiles use linear interpolation. The frame
queues are bounded; seen-marker bookkeeping and summary samples grow with the
number of unique markers, so split exceptionally long experiments into runs.

## Join source and receiver logs

Copy the source CSV to the receiving computer after the run:

```bash
python -m camera_latency report --measurements measurements.csv
python -m camera_latency report --measurements measurements.csv \
  --source-log source-timing.csv --output joined.csv
```

The joined file preserves all receiver columns and adds source submission time,
source schedule error/flags, join status and `submit_referenced_arrival_ms`.
Matching requires session, sequence and identical encoded timestamp. Missing
matches or timestamp conflicts stay explicit. The extra latency is calculated as
`arrival_wall_ns - source_submit_wall_ns`; equivalently it removes the known
source software scheduling offset from the raw result. Both raw and submission
referenced first-observation summaries are printed. Flagged rows remain included
and visible. Examine `source_flags` as well as receiver `flags` before drawing
conclusions. This correction does **not** remove physical display uncertainty,
camera sampling delay or clock offset.

## Limits and diagnostics

- Screen refresh/scanout, compositor buffering, pixel response, camera exposure
  and rolling shutter limit accuracy. Defaults favor repeatable detection rather
  than single-frame timing. For physical timing validation use independent optical
  or hardware measurements.
- This is the direct MJPEG receive path, including camera encoding, transport and
  software arrival handling. The camera's browser player can buffer further and
  therefore show different latency. No browser-player latency is measured here.
- The clock monitor compares successive wall and monotonic progress. A difference
  exceeding `--clock-jump-ms` (default 20 ms) increments an epoch and records the
  signed step. All later measurements in that run remain flagged conservatively;
  source presentations are flagged too. Restart after restoring synchronization.
  Gradual clock slews, initial offsets, steps below threshold, or steps that cancel
  between samples can escape detection. Sampling the two clocks is not atomic;
  severe scheduling delays can also trigger a flag.
- Negative latencies are retained and flagged, never clipped. They often indicate
  clock disagreement or source schedule trouble. Flags are diagnostics, not a
  calibration method. Nanosecond integer precision does not establish clock,
  display, camera or network accuracy.

## Validation

```bash
python -m pytest -q
```

Tests use real generated QR JPEGs, controlled monotonic/wall clocks and recognition
delays. They verify that recognition runtime does not change arrival latency,
clock offset changes it with the expected sign, negative values survive, clock
steps stay flagged, repeated markers count once, multipart fragments complete
without a following boundary, overload replaces rather than queues, and source
submission joins preserve the raw measurement. A loopback HTTP multipart server
also verifies capture during a slow consumer and per-frame arrival/drop logs.
The integration test needs permission to bind a loopback socket.

These checks validate software semantics. They cannot establish the AXIS camera's
actual latency or a real monitor's photon timing; perform the two-room experiment
with verified clock synchronization before trusting numerical results.
