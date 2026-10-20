import csv
import io
import threading
import time
from types import SimpleNamespace

import pytest

from camera_latency.core import Clock, CsvLog, LatestSlot, Marker, Stamp, latency_fields
from camera_latency.emitter import qr_image, slot_at
from camera_latency.mjpeg import MultipartJPEG, boundary_from_content_type
from camera_latency.receiver import Capture, FIELDS, Frame, Processor, decode_frame, recognize
from camera_latency.report import report

MARKER = Marker('0123456789abcdef', 42, 1_700_000_000_000_000_000)


def jpeg(marker=MARKER):
    buffer = io.BytesIO()
    qr_image(marker, 700).save(buffer, format='JPEG', quality=92)
    return buffer.getvalue()


class FakeTime:
    def __init__(self, wall=MARKER.timestamp_ns+100_000_000, mono=0):
        self.wall, self.mono = wall, mono

    def advance(self, ns):
        self.wall += ns
        self.mono += ns

    def clock(self):
        return Clock(wall=lambda: self.wall, mono=lambda: self.mono)


def test_synthetic_qr_and_recognition_delay_does_not_inflate_arrival():
    results = []
    for delay in (0, 250_000_000):
        fake = FakeTime()
        clock = fake.clock()
        arrival = clock.sample()
        fake.advance(5_000_000)  # waiting for consumer
        def slow_reader(image):
            found = recognize(image)
            fake.advance(delay)
            return found
        _, markers, invalid, _, stamps = decode_frame(Frame(1, jpeg(), arrival), clock, slow_reader)
        assert markers == [MARKER] and invalid == 0
        row = latency_fields(markers[0], arrival, *stamps)
        assert row['queue_ms'] == 5
        results.append(row)
    assert [r['arrival_latency_ms'] for r in results] == [100, 100]
    assert results[1]['recognition_latency_ms']-results[0]['recognition_latency_ms'] == 250
    assert results[1]['recognition_ms'] == 250


@pytest.mark.parametrize('offset', [-200_000_000, 80_000_000])
def test_clock_offset_bias_and_negative_values(offset):
    fake = FakeTime(wall=MARKER.timestamp_ns+100_000_000+offset)
    stamp = fake.clock().sample()
    row = latency_fields(MARKER, stamp, stamp, stamp, stamp, stamp)
    assert row['arrival_latency_ms'] == 100 + offset/1e6
    assert ('negative_latency' in row['flags']) == (offset < -100_000_000)


def test_clock_jump_is_detected_and_stays_suspect():
    fake = FakeTime()
    clock = fake.clock()
    clock.sample()
    fake.advance(10_000_000)
    fake.wall -= 100_000_000
    jump = clock.sample()
    assert jump.jump_ns == -100_000_000 and jump.epoch == 1
    fake.advance(10_000_000)
    later = clock.sample()
    assert later.epoch == 1 and later.jump_ns == 0
    assert 'receiver_clock_jump' in latency_fields(MARKER, later, later, later, later, later)['flags']


def test_overload_slot_holds_only_newest_and_counts_drops():
    slot = LatestSlot()
    for i in range(10_000):
        slot.put(i)
    assert slot.dropped == 9_999
    assert slot.get() == 9_999
    assert slot.get(.001) is None
    slot.close()
    assert slot.get() is None


def test_multipart_arbitrary_fragmentation_and_early_completion():
    data = jpeg()
    header = b'--axis\r\nContent-Type: image/jpeg\r\nContent-Length: ' + str(len(data)).encode() + b'\r\n\r\n'
    parser = MultipartJPEG(boundary_from_content_type('multipart/x-mixed-replace; boundary="axis"'))
    payload = header+data  # no next boundary: must yield now
    found = []
    for start in range(0, len(payload), 37):
        found.extend(parser.feed(payload[start:start+37]))
    assert found == [data]
    assert list(parser.feed(b'\r\n'+header+data)) == [data]
    with pytest.raises(ValueError):
        list(MultipartJPEG(b'--x').feed(b'--x\r\nContent-Type: image/jpeg\r\n\r\n'))


def test_repeats_first_observed_once(tmp_path):
    slot, display = LatestSlot(), LatestSlot()
    stop = threading.Event()
    clock = Clock()
    log = CsvLog(tmp_path/'out.csv', FIELDS)
    capture = SimpleNamespace(received=2)
    processor = Processor(slot, display, capture, clock, stop, log)
    processor.start()
    try:
        slot.put(Frame(1, jpeg(), clock.sample()))
        assert display.get(5) is not None
        slot.put(Frame(2, jpeg(), clock.sample()))
        assert display.get(5) is not None
    finally:
        stop.set()
        slot.close()
        processor.join(5)
        log.close()
    assert processor.error is None
    assert len(processor.samples) == 1
    with open(tmp_path/'out.csv') as file:
        rows = list(csv.DictReader(file))
    assert [r['first_observation'] for r in rows] == ['1', '0']
    assert [r['sighting'] for r in rows] == ['1', '2']


def test_offline_join_preserves_raw_and_subtracts_known_submission_offset(tmp_path):
    from camera_latency.emitter import FIELDS as SOURCE_FIELDS
    with open(tmp_path/'source.csv', 'w', newline='') as file:
        writer = csv.DictWriter(file, SOURCE_FIELDS)
        writer.writeheader()
        writer.writerow(dict(event='presented', session=MARKER.session, sequence=42,
                             marker_ns=MARKER.timestamp_ns, submit_wall_ns=MARKER.timestamp_ns+10_000_000,
                             schedule_error_ms=10, flags=''))
    log = CsvLog(tmp_path/'receiver.csv', FIELDS)
    log.write(event='frame', session=MARKER.session, sequence=42, marker_ns=MARKER.timestamp_ns,
              first_observation=1, arrival_wall_ns=MARKER.timestamp_ns+100_000_000, arrival_latency_ms=100)
    log.close()
    report(SimpleNamespace(source_log=tmp_path/'source.csv', measurements=tmp_path/'receiver.csv', output=tmp_path/'join.csv'))
    with open(tmp_path/'join.csv') as file:
        row = next(csv.DictReader(file))
    assert row['arrival_latency_ms'] == '100'
    assert float(row['submit_referenced_arrival_ms']) == 90
    assert row['join_status'] == 'matched'


def test_skip_slots_and_payload_validation():
    assert slot_at(1_050, 0, 200) == 5
    assert Marker.parse(MARKER.encode()) == MARKER
    for text in ['garbage', 'CL2:0123456789abcdef:1:3', 'CL1:0123456789abcdef:1:18446744073709551616']:
        with pytest.raises(ValueError):
            Marker.parse(text)


def test_capture_local_http_preserves_arrivals_and_bounded_overload(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    body = jpeg()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=axis')
            self.end_headers()
            try:
                for _ in range(80):
                    self.wfile.write(b'--axis\r\nContent-Type: image/jpeg\r\nContent-Length: ' +
                                     str(len(body)).encode() + b'\r\n\r\n' + body + b'\r\n')
                    self.wfile.flush()
                    time.sleep(.003)
                time.sleep(.1)
            except (BrokenPipeError, ConnectionResetError):
                pass
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    slot, stop, clock = LatestSlot(), threading.Event(), Clock()
    log = CsvLog(tmp_path/'capture.csv', FIELDS)
    args = SimpleNamespace(username=None, tls_context=True, timeout=.5,
                           url=f'http://127.0.0.1:{server.server_port}/stream')
    capture = Capture(args, slot, clock, stop, log)
    capture.start()
    consumed = []
    try:
        first = slot.get(5)
        assert first is not None
        consumed.append(first)
        # Slow recognition while the independent capture thread keeps draining.
        time.sleep(.15)
        newest = slot.get(1)
        assert newest.number > first.number + 5
        assert newest.arrival.mono_ns > first.arrival.mono_ns
        assert capture.received > 10 and slot.dropped > 5
        consumed.append(newest)
    finally:
        stop.set()
        capture.join(3)
        server.shutdown()
        server.server_close()
        log.close()
    assert not capture.is_alive()
    with open(tmp_path/'capture.csv') as file:
        rows = list(csv.DictReader(file))
    acquired = {int(r['frame_id']): r for r in rows if r['event'] == 'acquired'}
    assert len(acquired) == capture.received
    for frame in consumed:
        assert int(acquired[frame.number]['arrival_wall_ns']) == frame.arrival.wall_ns
    assert any(r['event'] == 'dropped' for r in rows)
