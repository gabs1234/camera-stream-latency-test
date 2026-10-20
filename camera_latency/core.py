"""Timing, marker format, CSV, and bounded handoff; no GUI dependencies."""
from __future__ import annotations

import csv
import math
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar


@dataclass(frozen=True)
class Marker:
    session: str
    sequence: int
    timestamp_ns: int

    def encode(self) -> str:
        return f"CL1:{self.session}:{self.sequence}:{self.timestamp_ns}"

    @classmethod
    def parse(cls, text: str) -> Marker:
        if len(text) > 100:
            raise ValueError("oversized marker")
        match = re.fullmatch(r"CL1:([0-9a-f]{16}):([0-9]{1,20}):([0-9]{1,20})", text)
        if not match:
            raise ValueError("invalid marker")
        session, seq, stamp = match.groups()
        if max(int(seq), int(stamp)) > 2**64 - 1:
            raise ValueError("marker integer outside uint64")
        return cls(session, int(seq), int(stamp))


@dataclass(frozen=True)
class Stamp:
    wall_ns: int
    mono_ns: int
    epoch: int = 0
    jump_ns: int = 0


class Clock:
    """Every detected step starts an epoch; all subsequent records stay suspect."""
    def __init__(self, threshold_ns: int = 20_000_000, wall=time.time_ns, mono=time.monotonic_ns):
        self.wall, self.mono, self.threshold = wall, mono, threshold_ns
        self.lock = threading.Lock()
        self.previous = None
        self.epoch = 0

    def sample(self) -> Stamp:
        with self.lock:
            m = self.mono()
            w = self.wall()
            jump = 0
            if self.previous:
                pw, pm = self.previous
                delta = (w - pw) - (m - pm)
                if abs(delta) > self.threshold:
                    jump = delta
                    self.epoch += 1
            self.previous = w, m
            return Stamp(w, m, self.epoch, jump)


T = TypeVar("T")


class LatestSlot(Generic[T]):
    """One waiting item, plus the consumer's in-flight item. Never blocks producer."""
    def __init__(self):
        self.condition = threading.Condition()
        self.item = None
        self.dropped = 0
        self.closed = False

    def put(self, item: T):
        with self.condition:
            if self.closed:
                return
            replaced = self.item
            if self.item is not None:
                self.dropped += 1
            self.item = item
            self.condition.notify()
            return replaced

    def get(self, timeout: float = 0.1) -> T | None:
        with self.condition:
            self.condition.wait_for(lambda: self.item is not None or self.closed, timeout)
            item, self.item = self.item, None
            return item

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()


class CsvLog:
    def __init__(self, path: str | Path, fields: list[str]):
        # Refuse accidental overwrite of an earlier experiment.
        self.file = open(path, "x", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.file, fieldnames=fields)
        self.writer.writeheader()
        self.file.flush()
        self.lock = threading.Lock()

    def write(self, **row):
        with self.lock:
            self.writer.writerow(row)
            self.file.flush()

    def close(self):
        self.file.close()


def stamp_fields(prefix: str, stamp: Stamp) -> dict:
    return {f"{prefix}_{key}": value for key, value in zip(
        ("wall_ns", "mono_ns", "clock_epoch", "clock_jump_ns"),
        (stamp.wall_ns, stamp.mono_ns, stamp.epoch, stamp.jump_ns))}


def stamp_columns(prefix: str) -> list[str]:
    return [f"{prefix}_{key}" for key in ("wall_ns", "mono_ns", "clock_epoch", "clock_jump_ns")]


def latency_fields(marker: Marker, arrival: Stamp, decode_start: Stamp,
                   decoded: Stamp, qr_start: Stamp, qr_done: Stamp) -> dict:
    flags = []
    if any(s.epoch for s in (arrival, decode_start, decoded, qr_start, qr_done)):
        flags.append("receiver_clock_jump")
    if arrival.wall_ns < marker.timestamp_ns:
        flags.append("negative_latency")
    return dict(
        session=marker.session, sequence=marker.sequence, marker_ns=marker.timestamp_ns,
        arrival_latency_ms=(arrival.wall_ns - marker.timestamp_ns) / 1e6,
        decoded_latency_ms=(decoded.wall_ns - marker.timestamp_ns) / 1e6,
        recognition_latency_ms=(qr_done.wall_ns - marker.timestamp_ns) / 1e6,
        decode_ms=(decoded.mono_ns - decode_start.mono_ns) / 1e6,
        queue_ms=(decode_start.mono_ns - arrival.mono_ns) / 1e6,
        recognition_ms=(qr_done.mono_ns - qr_start.mono_ns) / 1e6,
        flags=";".join(flags))


def summary(values: list[float]) -> str:
    if not values:
        return "n=0"
    ordered = sorted(values)
    def percentile(p):
        k = (len(ordered) - 1) * p
        a, b = math.floor(k), math.ceil(k)
        return ordered[a] + (ordered[b] - ordered[a]) * (k - a)
    return (f"n={len(values)} median={percentile(.5):.2f} ms "
            f"p95={percentile(.95):.2f} ms min={ordered[0]:.2f} ms max={ordered[-1]:.2f} ms")
