from __future__ import annotations

import io
import threading
import time
from dataclasses import dataclass

import httpx
from PIL import Image, ImageDraw, UnidentifiedImageError
import zxingcpp

from .core import (Clock, CsvLog, LatestSlot, Marker, Stamp, latency_fields,
                   stamp_columns, stamp_fields, summary)
from .mjpeg import MultipartJPEG, boundary_from_content_type

TIMING_PREFIXES = ('arrival', 'decode_start', 'decoded', 'qr_start', 'qr_done')
FIELDS = ['event', 'frame_id', 'session', 'sequence', 'marker_ns', 'first_observation',
          'sighting', 'arrival_latency_ms', 'decoded_latency_ms', 'recognition_latency_ms',
          'decode_ms', 'queue_ms', 'recognition_ms', 'frames_received', 'frames_dropped',
          'flags'] + [c for p in TIMING_PREFIXES for c in stamp_columns(p)]


@dataclass(frozen=True)
class Frame:
    number: int
    jpeg: bytes
    arrival: Stamp


class Capture(threading.Thread):
    def __init__(self, args, slot, clock, stop, log):
        super().__init__(name='mjpeg-capture', daemon=True)
        self.args, self.slot, self.clock, self.stop, self.log = args, slot, clock, stop, log
        self.received = 0
        self.reconnects = 0
        self.status = 'connecting'
        self.error = None
        self.rate_start = time.monotonic()

    @property
    def fps(self):
        return self.received / max(.001, time.monotonic() - self.rate_start)

    def run(self):
        try:
            self.consume()
        except Exception as exc:
            self.error = type(exc).__name__
            self.status = f'capture failed: {self.error}'
            self.stop.set()

    def consume(self):
        auth = None
        if self.args.username:
            kind = httpx.DigestAuth if self.args.auth == 'digest' else httpx.BasicAuth
            auth = kind(self.args.username, self.args.password)
        # trust_env=False avoids implicit proxies/netrc credentials; TLS verified by default.
        with httpx.Client(auth=auth, verify=self.args.tls_context, trust_env=False,
                          timeout=httpx.Timeout(self.args.timeout), follow_redirects=False) as client:
            while not self.stop.is_set():
                try:
                    with client.stream('GET', self.args.url,
                                       headers={'Accept-Encoding': 'identity'}) as response:
                        response.raise_for_status()
                        parser = MultipartJPEG(boundary_from_content_type(response.headers.get('content-type', '')))
                        self.status = 'receiving'
                        for chunk in response.iter_raw():
                            if self.stop.is_set():
                                return
                            for jpeg in parser.feed(chunk):
                                # First application point with the complete length-delimited JPEG.
                                stamp = self.clock.sample()
                                self.received += 1
                                replaced = self.slot.put(Frame(self.received, jpeg, stamp))
                                self.log.write(event='acquired', frame_id=self.received,
                                               frames_received=self.received, frames_dropped=self.slot.dropped,
                                               **stamp_fields('arrival', stamp))
                                if replaced:
                                    self.log.write(event='dropped', frame_id=replaced.number,
                                                   frames_received=self.received, frames_dropped=self.slot.dropped,
                                                   flags='replaced_by_newer_frame',
                                                   **stamp_fields('arrival', replaced.arrival))
                        raise ValueError('stream ended')
                except (httpx.HTTPError, ValueError, UnicodeError) as exc:
                    # Never print exception strings: they can contain a URL or credentials.
                    code = f'_{exc.response.status_code}' if isinstance(exc, httpx.HTTPStatusError) else ''
                    self.status = f'connection error: {type(exc).__name__}{code}; retrying'
                    self.reconnects += 1
                    self.log.write(event='connection_error', flags=f'{type(exc).__name__}{code}',
                                   frames_received=self.received, frames_dropped=self.slot.dropped,
                                   **stamp_fields('arrival', self.clock.sample()))
                    self.stop.wait(1)


def recognize(image):
    return zxingcpp.read_barcodes(image, formats=zxingcpp.BarcodeFormat.QRCode)


def decode_frame(frame: Frame, clock: Clock, reader=recognize):
    start = clock.sample()
    with Image.open(io.BytesIO(frame.jpeg)) as raw:
        image = raw.convert('RGB')  # forces JPEG decoding before completion timestamp
    decoded = clock.sample()
    qr_start = clock.sample()
    codes = reader(image)
    qr_done = clock.sample()
    timing = {}
    for prefix, stamp in zip(TIMING_PREFIXES, (frame.arrival, start, decoded, qr_start, qr_done)):
        timing.update(stamp_fields(prefix, stamp))
    markers, invalid = [], 0
    for code in codes:
        try:
            marker = Marker.parse(code.text)
            if marker not in markers:
                markers.append(marker)
            position = code.position
            corners = [(p.x, p.y) for p in (position.top_left, position.top_right,
                                          position.bottom_right, position.bottom_left)]
            ImageDraw.Draw(image).line(corners + [corners[0]], fill='lime', width=3)
        except ValueError:
            invalid += 1
    return image, markers, invalid, timing, (start, decoded, qr_start, qr_done)


class Processor(threading.Thread):
    def __init__(self, slot, display, capture, clock, stop, log):
        super().__init__(name='qr-recognition', daemon=True)
        self.slot, self.display, self.capture = slot, display, capture
        self.clock, self.stop, self.log = clock, stop, log
        self.seen = {}
        self.samples = []
        self.clean_samples = []
        self.error = None

    def run(self):
        try:
            self.process()
        except Exception as exc:
            self.error = type(exc).__name__
            self.stop.set()

    def process(self):
        last_measurement = 'no successful detection yet'
        while not self.stop.is_set():
            frame = self.slot.get()
            if frame is None:
                continue
            base = dict(event='frame', frame_id=frame.number,
                        frames_received=self.capture.received, frames_dropped=self.slot.dropped)
            try:
                image, markers, invalid, timing, stamps = decode_frame(frame, self.clock)
            except (UnidentifiedImageError, OSError, ValueError):
                self.log.write(**base, flags='decode_error', **stamp_fields('arrival', frame.arrival))
                continue
            flags = ['invalid_qr'] if invalid else []
            if any(s.epoch for s in (frame.arrival, *stamps)):
                flags.append('receiver_clock_jump')
            status = 'no marker' if not markers else 'detected'
            label = f'{status}; {last_measurement}'
            if not markers:
                start, decoded, qs, qe = stamps
                self.log.write(**base, **timing, flags=';'.join(flags),
                               decode_ms=(decoded.mono_ns-start.mono_ns)/1e6,
                               queue_ms=(start.mono_ns-frame.arrival.mono_ns)/1e6,
                               recognition_ms=(qe.mono_ns-qs.mono_ns)/1e6)
            for marker in markers:
                key = marker.session, marker.sequence
                previous = self.seen.get(key)
                count = previous[1] + 1 if previous else 1
                self.seen[key] = (previous[0] if previous else marker.timestamp_ns, count)
                row = latency_fields(marker, frame.arrival, *stamps)
                row['flags'] = ';'.join(filter(None, [row['flags'], *flags]))
                if previous and previous[0] != marker.timestamp_ns:
                    row['flags'] += ';marker_timestamp_conflict'
                row.update(first_observation=int(previous is None), sighting=count)
                self.log.write(**base, **timing, **row)
                if previous is None:
                    self.samples.append(row['arrival_latency_ms'])
                    if not row['flags']:
                        self.clean_samples.append(row['arrival_latency_ms'])
                label = (f"{marker.session} seq={marker.sequence} "
                         f"arrival={row['arrival_latency_ms']:.1f} ms "
                         f"QR={row['recognition_ms']:.1f} ms sighting={count}")
                last_measurement = label
            self.display.put((image, label, summary(self.samples), ';'.join(flags)))


def measure(args):
    import pygame
    stop = threading.Event()
    slot, display = LatestSlot(), LatestSlot()
    clock = Clock(int(args.clock_jump_ms * 1e6))
    log = CsvLog(args.output, FIELDS)
    capture = Capture(args, slot, clock, stop, log)
    worker = Processor(slot, display, capture, clock, stop, log)
    window = None
    started = time.monotonic()
    try:
        if not args.headless:
            pygame.display.init()
            pygame.font.init()
            window = pygame.display.set_mode((1100, 820), pygame.RESIZABLE)
            pygame.display.set_caption('Direct MJPEG camera latency — Escape to quit')
            font = pygame.font.Font(None, 24)
        capture.start()
        worker.start()
        latest = None
        next_print = 0
        while not stop.is_set():
            if args.duration and time.monotonic() - started >= args.duration:
                break
            item = display.get(.03)
            if item:
                latest = item
            if window:
                for event in pygame.event.get():
                    if event.type == pygame.QUIT or (event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE):
                        stop.set()
                window.fill('black')
                width, height = window.get_size()
                if latest:
                    image, label, stats, flags = latest
                    surface = pygame.image.frombytes(image.tobytes(), image.size, 'RGB')
                    factor = min(width / image.width, max(1, height - 130) / image.height)
                    surface = pygame.transform.smoothscale(surface, (max(1, int(image.width*factor)), max(1, int(image.height*factor))))
                    window.blit(surface, (0, 0))
                    lines = [label, stats, flags]
                else:
                    lines = ['waiting for first frame', 'n=0', '']
                lines += [f'{capture.status}; delivered {capture.fps:.1f} fps (session average); '
                          f'received={capture.received} dropped={slot.dropped}']
                for i, line in enumerate(lines):
                    window.blit(font.render(line, True, 'white'), (10, height-105+i*25))
                pygame.display.flip()
            if time.monotonic() >= next_print:
                print(f'{capture.status}; {capture.fps:.1f} fps; dropped={slot.dropped}; {summary(worker.samples)}', flush=True)
                next_print = time.monotonic() + 2
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        slot.close()
        if capture.ident:
            capture.join(args.timeout + 2)
        if worker.ident:
            worker.join()  # finish current recognition before closing CSV
        # Capture reads have a finite timeout, including during shutdown.
        if capture.is_alive():
            capture.join()
        pending = slot.get(0)
        if pending:
            slot.dropped += 1
            log.write(event='dropped', frame_id=pending.number, frames_received=capture.received,
                      frames_dropped=slot.dropped, flags='shutdown_pending_frame',
                      **stamp_fields('arrival', pending.arrival))
        log.write(event='shutdown', frames_received=capture.received, frames_dropped=slot.dropped,
                  flags=';'.join(filter(None, [f'processor_error:{worker.error}' if worker.error else '',
                                               f'capture_error:{capture.error}' if capture.error else ''])))
        log.close()
        pygame.quit()
    print('First-observed arrival latency (all): ' + summary(worker.samples))
    print('First-observed arrival latency (unflagged): ' + summary(worker.clean_samples))
    if worker.error:
        raise RuntimeError(f'processing failed: {worker.error}')
    if capture.error:
        raise RuntimeError(f'capture failed: {capture.error}')
