"""In-RAM time-series store with a hard memory ceiling.

Every subscribed variable gets its own pair of growable ``float64`` arrays
(timestamp, value).  Memory use is accounted for globally; once it crosses the
configured ceiling the oldest samples are dropped across *all* variables using a
common cut-off timestamp, so the variables that remain stay time-aligned.

Samples are assumed to arrive in roughly chronological order.  ``merge`` exists
for out-of-order history backfill and pays a sort to keep each buffer ordered.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Iterable

import numpy as np

BYTES_PER_SAMPLE = 16  # one float64 timestamp + one float64 value
_MIN_CAPACITY = 256


def decimate(t: np.ndarray, v: np.ndarray, max_points: int) -> tuple[np.ndarray, np.ndarray]:
    """Reduce a series to at most ``max_points`` samples.

    Uses min/max bucketing: the series is split into buckets and each bucket
    contributes its lowest and highest value, emitted in chronological order.
    Spikes therefore survive the reduction, which plain striding would hide.
    """
    n = t.size
    if max_points <= 0 or n <= max_points:
        return t, v
    buckets = max(1, max_points // 2)
    # Bucket boundaries over the index range.
    edges = np.linspace(0, n, buckets + 1).astype(np.int64)
    keep = np.empty(buckets * 2, dtype=np.int64)
    out = 0
    for i in range(buckets):
        lo, hi = edges[i], edges[i + 1]
        if hi <= lo:
            continue
        window = v[lo:hi]
        lo_i = lo + int(np.argmin(window))
        hi_i = lo + int(np.argmax(window))
        if lo_i <= hi_i:
            keep[out] = lo_i
            keep[out + 1] = hi_i
        else:
            keep[out] = hi_i
            keep[out + 1] = lo_i
        out += 2
    idx = keep[:out]
    if idx.size > 1:
        idx = idx[np.concatenate(([True], np.diff(idx) != 0))]
    return t[idx], v[idx]


class VarBuffer:
    """Growable, chronologically ordered sample buffer for one variable."""

    __slots__ = ("name", "_t", "_v", "_n")

    def __init__(self, name: str, capacity: int = _MIN_CAPACITY):
        self.name = name
        capacity = max(_MIN_CAPACITY, capacity)
        self._t = np.empty(capacity, dtype=np.float64)
        self._v = np.empty(capacity, dtype=np.float64)
        self._n = 0

    def __len__(self) -> int:
        return self._n

    @property
    def capacity(self) -> int:
        return self._t.size

    @property
    def nbytes(self) -> int:
        return self.capacity * BYTES_PER_SAMPLE

    @property
    def first_time(self) -> float | None:
        return float(self._t[0]) if self._n else None

    @property
    def last_time(self) -> float | None:
        return float(self._t[self._n - 1]) if self._n else None

    def _reserve(self, extra: int) -> None:
        need = self._n + extra
        if need <= self.capacity:
            return
        new_cap = max(_MIN_CAPACITY, self.capacity)
        while new_cap < need:
            new_cap *= 2
        t = np.empty(new_cap, dtype=np.float64)
        v = np.empty(new_cap, dtype=np.float64)
        t[: self._n] = self._t[: self._n]
        v[: self._n] = self._v[: self._n]
        self._t, self._v = t, v

    def append(self, ts: float, value: float) -> None:
        """Append one sample.  Out-of-order timestamps are routed to merge()."""
        if self._n and ts < self._t[self._n - 1]:
            self.merge(np.array([ts]), np.array([value]))
            return
        self._reserve(1)
        self._t[self._n] = ts
        self._v[self._n] = value
        self._n += 1

    def extend(self, ts: np.ndarray, values: np.ndarray) -> None:
        """Append a chronologically ordered block of samples."""
        if ts.size == 0:
            return
        if self._n and ts[0] < self._t[self._n - 1]:
            self.merge(ts, values)
            return
        self._reserve(ts.size)
        self._t[self._n : self._n + ts.size] = ts
        self._v[self._n : self._n + ts.size] = values
        self._n += ts.size

    def merge(self, ts: np.ndarray, values: np.ndarray) -> None:
        """Insert samples that may predate or interleave existing ones."""
        if ts.size == 0:
            return
        all_t = np.concatenate((self._t[: self._n], ts))
        all_v = np.concatenate((self._v[: self._n], values))
        order = np.argsort(all_t, kind="stable")
        all_t, all_v = all_t[order], all_v[order]
        if all_t.size > 1:  # drop duplicate timestamps, keeping the newest write
            keep = np.concatenate((np.diff(all_t) != 0, [True]))
            all_t, all_v = all_t[keep], all_v[keep]
        self._t, self._v, self._n = all_t, all_v, all_t.size

    def trim_before(self, cutoff: float) -> int:
        """Drop samples older than ``cutoff``.  Returns the number removed."""
        if not self._n:
            return 0
        idx = int(np.searchsorted(self._t[: self._n], cutoff, side="left"))
        if idx <= 0:
            return 0
        self._compact_from(idx)
        return idx

    def trim_oldest(self, count: int) -> int:
        """Drop the ``count`` oldest samples."""
        count = min(count, self._n)
        if count > 0:
            self._compact_from(count)
        return count

    def _compact_from(self, idx: int) -> None:
        remaining = self._n - idx
        # Reallocate when most of the buffer would sit unused, so that trimming
        # actually returns memory to the process rather than just moving a index.
        if remaining * 2 < self.capacity and self.capacity > _MIN_CAPACITY:
            cap = max(_MIN_CAPACITY, 1 << max(8, int(remaining).bit_length()))
            t = np.empty(cap, dtype=np.float64)
            v = np.empty(cap, dtype=np.float64)
            t[:remaining] = self._t[idx : self._n]
            v[:remaining] = self._v[idx : self._n]
            self._t, self._v = t, v
        else:
            self._t[:remaining] = self._t[idx : self._n]
            self._v[:remaining] = self._v[idx : self._n]
        self._n = remaining

    def slice(self, start: float | None, end: float | None) -> tuple[np.ndarray, np.ndarray]:
        """Return copies of the samples inside ``[start, end]``."""
        lo = 0 if start is None else int(np.searchsorted(self._t[: self._n], start, side="left"))
        hi = self._n if end is None else int(np.searchsorted(self._t[: self._n], end, side="right"))
        if hi <= lo:
            return np.empty(0, dtype=np.float64), np.empty(0, dtype=np.float64)
        return self._t[lo:hi].copy(), self._v[lo:hi].copy()


class RamStore:
    """Thread-safe collection of :class:`VarBuffer` under a global memory cap."""

    def __init__(self, max_bytes: int = 4_000_000_000, max_age_seconds: float = 0.0):
        self.max_bytes = int(max_bytes)
        self.max_age_seconds = float(max_age_seconds or 0.0)
        self._vars: dict[str, VarBuffer] = {}
        self._lock = threading.RLock()
        self._nbytes = 0
        self.samples_ingested = 0
        self.samples_dropped = 0
        self.samples_rejected = 0

    # -- Ingestion --------------------------------------------------------------
    def _buffer(self, name: str) -> VarBuffer:
        buf = self._vars.get(name)
        if buf is None:
            buf = VarBuffer(name)
            self._vars[name] = buf
            self._nbytes += buf.nbytes
        return buf

    def _rebill(self, buf: VarBuffer, before: int) -> None:
        self._nbytes += buf.nbytes - before

    def add_sample(self, name: str, ts: float, value: object) -> bool:
        """Record one sample.  Non-finite or non-numeric values are rejected."""
        try:
            fv = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            with self._lock:
                self.samples_rejected += 1
            return False
        if not math.isfinite(fv) or not math.isfinite(ts):
            with self._lock:
                self.samples_rejected += 1
            return False
        with self._lock:
            buf = self._buffer(name)
            before = buf.nbytes
            buf.append(float(ts), fv)
            self._rebill(buf, before)
            self.samples_ingested += 1
            self._enforce_limits()
        return True

    def add_block(self, name: str, ts: Iterable[float], values: Iterable[float], *, merge: bool = False) -> int:
        """Record a block of samples, dropping any that are not finite."""
        t_arr = np.asarray(list(ts), dtype=np.float64)
        v_arr = np.asarray(list(values), dtype=np.float64)
        if t_arr.size != v_arr.size:
            raise ValueError("timestamp and value counts differ")
        good = np.isfinite(t_arr) & np.isfinite(v_arr)
        rejected = int((~good).sum())
        t_arr, v_arr = t_arr[good], v_arr[good]
        with self._lock:
            self.samples_rejected += rejected
            if t_arr.size:
                buf = self._buffer(name)
                before = buf.nbytes
                if merge:
                    buf.merge(t_arr, v_arr)
                else:
                    buf.extend(t_arr, v_arr)
                self._rebill(buf, before)
                self.samples_ingested += int(t_arr.size)
                self._enforce_limits()
        return int(t_arr.size)

    # -- Eviction ---------------------------------------------------------------
    def _enforce_limits(self) -> None:
        """Drop the oldest data until the store is back inside its limits.

        Called with the lock held.
        """
        if self.max_age_seconds > 0:
            newest = self._newest_locked()
            if newest is not None:
                self._trim_before_locked(newest - self.max_age_seconds)
        if self._nbytes <= self.max_bytes:
            return
        for _ in range(24):  # bounded: each pass removes a fifth of the time span
            if self._nbytes <= self.max_bytes:
                return
            oldest, newest = self._span_locked()
            if oldest is None or newest is None or newest <= oldest:
                break
            cutoff = oldest + (newest - oldest) * 0.2
            if cutoff <= oldest:
                break
            self._trim_before_locked(cutoff)
        # Fallback for data packed into a single instant: trim by sample count.
        while self._nbytes > self.max_bytes:
            biggest = max(self._vars.values(), key=len, default=None)
            if biggest is None or len(biggest) == 0:
                break
            before = biggest.nbytes
            self.samples_dropped += biggest.trim_oldest(max(1, len(biggest) // 5))
            self._rebill(biggest, before)

    def _trim_before_locked(self, cutoff: float) -> None:
        for buf in self._vars.values():
            before = buf.nbytes
            self.samples_dropped += buf.trim_before(cutoff)
            self._rebill(buf, before)

    def _span_locked(self) -> tuple[float | None, float | None]:
        firsts = [b.first_time for b in self._vars.values() if b.first_time is not None]
        lasts = [b.last_time for b in self._vars.values() if b.last_time is not None]
        return (min(firsts) if firsts else None, max(lasts) if lasts else None)

    def _newest_locked(self) -> float | None:
        lasts = [b.last_time for b in self._vars.values() if b.last_time is not None]
        return max(lasts) if lasts else None

    # -- Queries ----------------------------------------------------------------
    def variables(self) -> list[str]:
        with self._lock:
            return sorted(self._vars)

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self._vars

    def earliest(self, name: str | None = None) -> float | None:
        with self._lock:
            if name is None:
                return self._span_locked()[0]
            buf = self._vars.get(name)
            return buf.first_time if buf else None

    def latest(self, name: str | None = None) -> float | None:
        with self._lock:
            if name is None:
                return self._newest_locked()
            buf = self._vars.get(name)
            return buf.last_time if buf else None

    def query(
        self,
        name: str,
        start: float | None = None,
        end: float | None = None,
        max_points: int = 0,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return ``(timestamps, values)`` inside ``[start, end]``, decimated."""
        with self._lock:
            buf = self._vars.get(name)
            if buf is None:
                return np.empty(0, dtype=np.float64), np.empty(0, dtype=np.float64)
            t, v = buf.slice(start, end)
        if max_points:
            t, v = decimate(t, v, max_points)
        return t, v

    def stats(self) -> dict[str, object]:
        with self._lock:
            oldest, newest = self._span_locked()
            return {
                "variables": len(self._vars),
                "samples": sum(len(b) for b in self._vars.values()),
                "bytes": self._nbytes,
                "max_bytes": self.max_bytes,
                "usage_fraction": (self._nbytes / self.max_bytes) if self.max_bytes else 0.0,
                "oldest": oldest,
                "newest": newest,
                "ingested": self.samples_ingested,
                "dropped": self.samples_dropped,
                "rejected": self.samples_rejected,
                "now": time.time(),
            }
