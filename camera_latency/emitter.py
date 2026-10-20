from __future__ import annotations

import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import qrcode

from .core import Clock, CsvLog, Marker, stamp_columns, stamp_fields

PREFIXES = ('generation_start', 'generation_done', 'submit', 'flip_done', 'black_submit')
FIELDS = ['event', 'session', 'sequence', 'marker_ns', 'scheduled_mono_ns',
          'generation_ms', 'schedule_error_ms', 'actual_visible_submission_ms', 'flags'] + [
              c for p in PREFIXES for c in stamp_columns(p)]


def qr_image(marker: Marker, size: int):
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=1, border=4)
    qr.add_data(marker.encode())
    qr.make(fit=True)
    image = qr.make_image(fill_color='black', back_color='white').convert('RGB')
    # Integer scaling keeps edges sharp and a complete four-module quiet zone.
    scale = size // image.width
    if scale < 2:
        raise ValueError('display too small for a readable QR code')
    return image.resize((image.width*scale, image.height*scale), resample=0)


def slot_at(now: int, origin: int, interval: int) -> int:
    return (now - origin) // interval


def emit(args):
    import pygame
    pygame.display.init()
    pygame.font.init()
    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    pygame.display.set_caption('Timestamp QR source — Escape to quit')
    width, height = screen.get_size()
    font = pygame.font.Font(None, max(22, min(38, width//40)))
    session = secrets.token_hex(8)
    clock = Clock(int(args.clock_jump_ms*1e6))
    initial = clock.sample()
    interval, visible = int(args.interval_ms*1e6), int(args.visible_ms*1e6)
    origin = initial.mono_ns + 500_000_000
    wall_origin = initial.wall_ns + (origin - initial.mono_ns)
    log = CsvLog(args.source_log, FIELDS)
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='qr-generation')
    print(f'session={session}; source log={args.source_log}', flush=True)

    def prepare(sequence):
        marker = Marker(session, sequence, wall_origin + sequence*interval)
        start = clock.sample()
        image = qr_image(marker, min(width-40, height-150))
        end = clock.sample()
        info = dict(session=session, sequence=sequence, marker_ns=marker.timestamp_ns,
                    scheduled_mono_ns=origin + sequence*interval,
                    generation_ms=(end.mono_ns-start.mono_ns)/1e6,
                    **stamp_fields('generation_start', start), **stamp_fields('generation_done', end))
        log.write(event='generated', **info, flags='source_clock_jump' if end.epoch else '')
        return marker, image, info

    pending_seq = 0
    future = pool.submit(prepare, 0)
    prepared = None
    next_slot = 0
    active = None
    running = True
    try:
        screen.fill('black')
        pygame.display.flip()
        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT or (event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE):
                    running = False
            now = clock.sample()  # also checks steps during black/idle periods
            if active and now.mono_ns >= active['scheduled_mono_ns'] + visible:
                screen.fill('black')
                black = clock.sample()
                pygame.display.flip()
                log.write(event='hidden', session=session, sequence=active['sequence'],
                          marker_ns=active['marker_ns'],
                          actual_visible_submission_ms=(black.mono_ns-active['submit_mono_ns'])/1e6,
                          **stamp_fields('black_submit', black),
                          flags='source_clock_jump' if black.epoch else '')
                active = None
            if future and future.done():
                marker, image, info = future.result()
                # Complete pygame surface and text preparation before the deadline.
                canvas = pygame.Surface((width, height))
                canvas.fill('black')
                surface = pygame.image.frombytes(image.tobytes(), image.size, 'RGB')
                canvas.blit(surface, ((width-image.width)//2, 20))
                utc = datetime.fromtimestamp(marker.timestamp_ns/1e9, timezone.utc).isoformat(timespec='milliseconds')
                for index, line in enumerate((f'UTC scheduled {utc}', f'{session}  sequence {marker.sequence}',
                                               'Escape exits • timestamp units: ns, accuracy is not ns')):
                    text = font.render(line, True, 'white')
                    canvas.blit(text, (max(0, (width-text.get_width())//2), height-120+index*36))
                prepared = (pending_seq, canvas, info)
                future = None
            current = slot_at(now.mono_ns, origin, interval)
            if current >= next_slot:
                # One log record per skipped slot; never replay expired presentations.
                for seq in range(next_slot, current):
                    log.write(event='skipped', session=session, sequence=seq,
                              marker_ns=wall_origin+seq*interval, scheduled_mono_ns=origin+seq*interval,
                              flags='missed_slot')
                deadline = origin + current*interval
                if prepared and prepared[0] == current and now.mono_ns < deadline + visible:
                    _, canvas, info = prepared
                    screen.blit(canvas, (0, 0))
                    submit = clock.sample()
                    # If copying the canvas consumed the entire visible window, skip it.
                    if submit.mono_ns < deadline + visible:
                        pygame.display.flip()
                        done = clock.sample()
                        active = dict(info, **stamp_fields('submit', submit))
                        log.write(event='presented', **info, **stamp_fields('submit', submit),
                                  **stamp_fields('flip_done', done),
                                  schedule_error_ms=(submit.mono_ns-deadline)/1e6,
                                  flags='source_clock_jump' if done.epoch else '')
                    else:
                        log.write(event='skipped', **info, flags='visible_window_expired')
                else:
                    log.write(event='skipped', session=session, sequence=current,
                              marker_ns=wall_origin+current*interval, scheduled_mono_ns=deadline,
                              flags='generation_not_ready_or_expired')
                next_slot = current + 1
                prepared = None
            # Only one generation job exists; when late it is discarded, not replayed.
            if future is None and (prepared is None or prepared[0] != next_slot):
                pending_seq = max(next_slot, slot_at(time.monotonic_ns(), origin, interval)+1)
                future = pool.submit(prepare, pending_seq)
            time.sleep(.001)
    except KeyboardInterrupt:
        pass
    finally:
        if active:
            screen.fill('black')
            black = clock.sample()
            pygame.display.flip()
            log.write(event='hidden', session=session, sequence=active['sequence'],
                      marker_ns=active['marker_ns'], **stamp_fields('black_submit', black),
                      actual_visible_submission_ms=(black.mono_ns-active['submit_mono_ns'])/1e6,
                      flags='exit')
        pool.shutdown(wait=True, cancel_futures=True)
        log.close()
        pygame.quit()
