"""Turning validated plot specifications into data payloads for the browser.

Live plots are served incrementally: the first request returns the whole window
from the plot's start time up to now, and later requests pass the cursor from
the previous reply so only the new samples travel.  Static plots are served
whole, and plots driven by a user function are always recomputed over the full
window because the function may depend on every point in it.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import numpy as np

from .models import DEFAULT_PALETTE, palette_color
from .ringstore import RamStore, decimate
from .subscriber import Subscriber
from .timeutil import parse_datetime, TimeError
from .userfuncs import FunctionError, FunctionRegistry, PlotData

log = logging.getLogger(__name__)

HISTORY_RETRY_SECONDS = 60.0
# Beyond this, a "live" plot's initial window is clamped: a start date from last
# year would otherwise ask the store for everything it holds on every reload.
MAX_LIVE_WINDOW_SECONDS = 90 * 24 * 3600


def _palette_excluding(used: set[str]) -> list[str]:
    """The default palette with `used` colours removed, never returning empty."""
    remaining = [color for color in DEFAULT_PALETTE if color not in used]
    return remaining or list(DEFAULT_PALETTE)


def _round_times(values: np.ndarray) -> list[float]:
    return np.round(values, 3).tolist()


def _round_values(values: np.ndarray) -> list[float]:
    return np.round(values, 6).tolist()


class QueryEngine:
    """Answers plot data requests from the RAM store, backfilling when it can."""

    def __init__(
        self,
        store: RamStore,
        subscriber: Subscriber | None,
        functions: FunctionRegistry,
        max_points: int = 4000,
    ):
        self.store = store
        self.subscriber = subscriber
        self.functions = functions
        self.max_points = int(max_points)
        self._history_attempts: dict[tuple[str, int, int], float] = {}
        self._lock = threading.Lock()

    # -- History backfill -------------------------------------------------------
    def _should_try_history(self, variable: str, start: float, end: float) -> bool:
        if self.subscriber is None or not self.subscriber.allow_history_requests:
            return False
        if self.subscriber.history_supported is False:
            return False
        key = (variable, int(start), int(end))
        now = time.monotonic()
        with self._lock:
            last = self._history_attempts.get(key)
            if last is not None and now - last < HISTORY_RETRY_SECONDS:
                return False
            self._history_attempts[key] = now
            if len(self._history_attempts) > 4096:  # keep the throttle map bounded
                self._history_attempts.clear()
        return True

    def _backfill(self, variables: list[str], start: float, end: float) -> str:
        """Ask the node for data older than the store holds.  Returns a notice."""
        missing = []
        for variable in variables:
            earliest = self.store.earliest(variable)
            if earliest is None or earliest > start + 1.0:
                missing.append(variable)
        if not missing:
            return ""
        if not self._should_try_history(",".join(sorted(missing)), start, end):
            return self._coverage_notice(missing, start)
        try:
            fetched = self.subscriber.fetch_history(missing, start, end)  # type: ignore[union-attr]
            if fetched:
                log.info("backfilled %d samples for %d variables", fetched, len(missing))
        except Exception as exc:  # noqa: BLE001 - degrade to whatever RAM holds
            log.info("history request failed: %s", exc)
        return self._coverage_notice(missing, start)

    def _coverage_notice(self, variables: list[str], start: float) -> str:
        still_missing = []
        for variable in variables:
            earliest = self.store.earliest(variable)
            if earliest is None or earliest > start + 1.0:
                still_missing.append(variable)
        if not still_missing:
            return ""
        earliest_times = [self.store.earliest(v) for v in still_missing]
        known = [t for t in earliest_times if t is not None]
        if not known:
            return "No data has arrived for these variables yet."
        from .timeutil import format_epoch
        return f"Data before {format_epoch(min(known))} is not held in memory."

    # -- Series retrieval -------------------------------------------------------
    def _fetch(self, variable: str, start: float, end: float, max_points: int) -> tuple[np.ndarray, np.ndarray]:
        return self.store.query(variable, start, end, max_points=max_points)

    def plot_payload(
        self,
        plot: dict[str, Any],
        index: int,
        *,
        owner: str,
        since: float | None = None,
        max_points: int | None = None,
    ) -> dict[str, Any]:
        """Build the JSON payload for a single plot."""
        limit = int(max_points or self.max_points)
        now = time.time()
        payload: dict[str, Any] = {
            "index": index,
            "title": plot.get("title", f"Plot {index + 1}"),
            "y_label": plot.get("y_label", ""),
            "mode": plot.get("mode", "live"),
            "show_legend": plot.get("show_legend", True),
            "show_grid": plot.get("show_grid", True),
            "series": [],
            "lines": [],
            "image": None,
            "incremental": False,
            "cursor": None,
            "notice": "",
            "error": "",
            "server_time": now,
        }
        for bound in ("y_min", "y_max"):
            if bound in plot:
                payload[bound] = plot[bound]

        try:
            start = parse_datetime(plot.get("start"))
            end = parse_datetime(plot.get("end")) if plot.get("mode") == "static" else None
        except TimeError as exc:
            payload["error"] = str(exc)
            return payload

        if plot.get("mode") == "static":
            if start is None or end is None:
                payload["error"] = "This static plot is missing its start or end time."
                return payload
        else:
            end = now
            if start is None:
                start = now - 3600
            if end - start > MAX_LIVE_WINDOW_SECONDS:
                start = end - MAX_LIVE_WINDOW_SECONDS
                payload["notice"] = "Showing the most recent 90 days; the start date is further back than that."
        payload["start_ts"] = start
        payload["end_ts"] = end

        spec_series = plot.get("series") or []
        variables = [s["variable"] for s in spec_series]
        function_spec = plot.get("function")

        # Incremental updates only apply to live plots with no function attached:
        # a function may aggregate over the whole window, so it is recomputed.
        incremental = (
            since is not None
            and plot.get("mode") == "live"
            and not function_spec
            and since >= start
        )
        window_start = since if incremental else start
        payload["incremental"] = bool(incremental)

        if not incremental and variables:
            # A clamped window and a coverage gap are separate facts; show both
            # rather than letting the later one silently replace the earlier.
            coverage = self._backfill(variables, start, end)
            payload["notice"] = "  ".join(n for n in (payload["notice"], coverage) if n)

        cursor = since if incremental else None
        if function_spec:
            payload.update(self._function_payload(function_spec, spec_series, start, end, now, owner, limit))
            payload["cursor"] = end
            return payload

        for i, spec in enumerate(spec_series):
            times, values = self._fetch(spec["variable"], window_start, end, 0 if incremental else limit)
            if incremental and times.size and window_start is not None:
                keep = times > window_start
                times, values = times[keep], values[keep]
                if times.size > limit:
                    times, values = decimate(times, values, limit)
            if times.size:
                last = float(times[-1])
                cursor = last if cursor is None else max(cursor, last)
            payload["series"].append({
                "label": spec.get("label") or spec["variable"],
                "variable": spec["variable"],
                "color": spec.get("color") or palette_color(i),
                "kind": spec.get("kind", "line"),
                "width": spec.get("width", 1.8),
                "point_size": spec.get("point_size", 2.5),
                "y_axis": spec.get("y_axis", "left"),
                "x": _round_times(times),
                "y": _round_values(values),
            })
        payload["cursor"] = cursor if cursor is not None else (since if since is not None else start)
        return payload

    def _function_payload(
        self,
        function_spec: dict[str, Any],
        spec_series: list[dict[str, Any]],
        start: float,
        end: float,
        now: float,
        owner: str,
        limit: int,
    ) -> dict[str, Any]:
        """Run a plot's user function and turn its output into drawable series."""
        # Functions see decimated data too, so a multi-day window cannot hand a
        # user function tens of millions of points.
        inputs: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        for spec in spec_series:
            inputs[spec["variable"]] = self._fetch(spec["variable"], start, end, limit)

        result: dict[str, Any] = {"series": [], "lines": [], "image": None, "error": ""}
        try:
            output = self.functions.run(
                function_spec, PlotData(inputs, start, end, now), function_spec.get("owner") or owner
            )
        except FunctionError as exc:
            result["error"] = str(exc)
            return result

        # When the raw inputs stay on the plot, the function's own traces must
        # not be handed a colour one of them already uses.
        reserved = set()
        if not function_spec.get("replace_series", True):
            reserved = {spec.get("color") for spec in spec_series if spec.get("color")}
        palette = _palette_excluding(reserved)

        result["image"] = output["image"]
        for i, item in enumerate(output["series"]):
            times, values = item["t"], item["v"]
            if times.size > limit:
                times, values = decimate(times, values, limit)
            result["series"].append({
                "label": item["label"],
                "variable": item["label"],
                "color": palette_color(i, palette),
                "kind": function_spec.get("kind", "line"),
                "width": 1.8,
                "point_size": 2.5,
                "y_axis": "left",
                "x": _round_times(times),
                "y": _round_values(values),
            })
        for i, line in enumerate(output["lines"]):
            result["lines"].append({
                "label": line["label"],
                "value": line["value"],
                "color": palette_color(len(result["series"]) + i, palette),
            })

        # Keep the raw inputs visible alongside a function's output unless the
        # profile asked for the function to replace them.
        if not function_spec.get("replace_series", True):
            offset = len(result["series"])
            for i, spec in enumerate(spec_series):
                times, values = inputs[spec["variable"]]
                result["series"].append({
                    "label": spec.get("label") or spec["variable"],
                    "variable": spec["variable"],
                    "color": spec.get("color") or palette_color(offset + i),
                    "kind": spec.get("kind", "line"),
                    "width": spec.get("width", 1.8),
                    "point_size": spec.get("point_size", 2.5),
                    "y_axis": spec.get("y_axis", "left"),
                    "x": _round_times(times),
                    "y": _round_values(values),
                })
        return result

    def profile_payload(
        self,
        profile: dict[str, Any],
        *,
        owner: str,
        cursors: dict[str, float] | None = None,
        only: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Build payloads for a profile's plots.

        ``only`` restricts the work to certain plot indices, which is how the
        live refresh loop avoids recomputing static plots every second.
        """
        cursors = cursors or {}
        payloads = []
        for index, plot in enumerate(profile.get("plots", [])):
            if only is not None and index not in only:
                continue
            since = cursors.get(str(index))
            payloads.append(self.plot_payload(plot, index, owner=owner, since=since))
        return payloads
