"""Profile schema: defaults, validation and normalisation.

Everything a browser posts passes through :func:`validate_profile` before it is
written to disk, and everything read off disk passes through it again before it
is used, so a hand-edited file cannot put the renderer into a state the UI
never produces.
"""

from __future__ import annotations

import re
from typing import Any

from .timeutil import TimeError, parse_datetime, utc_now_iso

FORMAT_VERSION = 1
MAX_PLOTS = 4
MAX_SERIES_PER_PLOT = 24

LAYOUTS = ("1x1", "2x1", "1x2", "2x2")
PLOT_MODES = ("live", "static")
SERIES_KINDS = ("line", "scatter", "step", "area", "bar")

# Chosen for legibility against the dark grey canvas and separated in hue so
# adjacent traces stay distinguishable.
DEFAULT_PALETTE = [
    "#4fc3f7", "#ffb74d", "#81c784", "#e57373",
    "#ba68c8", "#4db6ac", "#fff176", "#f06292",
    "#7986cb", "#a1887f", "#90a4ae", "#aed581",
]

_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class ValidationError(ValueError):
    """A profile document was rejected; the message is safe to show a user."""


def _text(value: Any, limit: int, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, (str, int, float)):
        raise ValidationError(f"{field} must be text.")
    text = str(value).strip()
    if len(text) > limit:
        raise ValidationError(f"{field} must be {limit} characters or fewer.")
    return text


def _choice(value: Any, options: tuple[str, ...], default: str, field: str) -> str:
    if value in (None, ""):
        return default
    text = str(value).strip().lower()
    if text not in options:
        raise ValidationError(f"{field} must be one of: {', '.join(options)}.")
    return text


def _number(value: Any, field: str) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field} must be a number.") from None


def normalise_color(value: Any, fallback: str) -> str:
    if isinstance(value, str) and _COLOR_RE.match(value.strip()):
        color = value.strip().lower()
        if len(color) == 4:  # expand #abc to #aabbcc so the client never has to
            color = "#" + "".join(ch * 2 for ch in color[1:])
        return color
    return fallback


def palette_color(index: int, palette: list[str] | None = None) -> str:
    colors = palette or DEFAULT_PALETTE
    return colors[index % len(colors)]


def validate_series(raw: Any, index: int, default_kind: str, palette: list[str] | None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValidationError("Each series must be a table.")
    variable = _text(raw.get("variable") or raw.get("name"), 200, "Series variable")
    if not variable:
        raise ValidationError("Every series needs a variable name.")
    return {
        "variable": variable,
        "label": _text(raw.get("label"), 120, "Series label") or variable,
        "color": normalise_color(raw.get("color"), palette_color(index, palette)),
        "kind": _choice(raw.get("kind"), SERIES_KINDS, default_kind, "Series plot type"),
        "width": max(0.5, min(8.0, _number(raw.get("width"), "Series width") or 1.8)),
        "point_size": max(0.0, min(20.0, _number(raw.get("point_size"), "Series point size") or 2.5)),
        "y_axis": _choice(raw.get("y_axis"), ("left", "right"), "left", "Series axis"),
    }


def validate_function(raw: Any) -> dict[str, Any] | None:
    """Validate the optional user-function block attached to a plot."""
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise ValidationError("A plot's function must be a table.")
    module = _text(raw.get("module"), 64, "Function module")
    name = _text(raw.get("name"), 64, "Function name")
    if not module or not name:
        return None
    if not _MODULE_RE.match(module) or not _MODULE_RE.match(name):
        raise ValidationError("Function module and name must be plain Python identifiers.")
    args = raw.get("args") or {}
    if not isinstance(args, dict):
        raise ValidationError("Function args must be a table of name = value pairs.")
    clean_args: dict[str, Any] = {}
    for key, value in args.items():
        key = _text(key, 64, "Function argument name")
        if not _MODULE_RE.match(key):
            raise ValidationError(f"Function argument {key!r} is not a valid identifier.")
        if isinstance(value, (str, int, float, bool)):
            clean_args[key] = value
        elif isinstance(value, list) and all(isinstance(v, (str, int, float, bool)) for v in value):
            clean_args[key] = list(value)
        else:
            raise ValidationError(f"Function argument {key!r} must be a string, number, boolean or list.")
    return {
        "module": module,
        "name": name,
        "args": clean_args,
        "scope": _choice(raw.get("scope"), ("user", "examples"), "user", "Function scope"),
        "owner": _text(raw.get("owner"), 32, "Function owner"),
        "replace_series": bool(raw.get("replace_series", True)),
    }


def validate_plot(raw: Any, index: int, palette: list[str] | None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValidationError("Each plot must be a table.")
    mode = _choice(raw.get("mode"), PLOT_MODES, "live", "Plot mode")
    default_kind = _choice(raw.get("kind"), SERIES_KINDS, "line", "Plot type")

    raw_series = raw.get("series") or []
    if not isinstance(raw_series, list):
        raise ValidationError("A plot's series must be a list.")
    if len(raw_series) > MAX_SERIES_PER_PLOT:
        raise ValidationError(f"A plot may show at most {MAX_SERIES_PER_PLOT} variables.")
    series = [validate_series(s, i, default_kind, palette) for i, s in enumerate(raw_series)]

    function = validate_function(raw.get("function"))
    if not series and not function:
        raise ValidationError(f"Plot {index + 1} needs at least one variable or a function.")

    try:
        start_ts = parse_datetime(raw.get("start"))
        end_ts = parse_datetime(raw.get("end"))
    except TimeError as exc:
        raise ValidationError(str(exc)) from exc

    if mode == "static":
        if start_ts is None or end_ts is None:
            raise ValidationError(f"Plot {index + 1} is static, so it needs both a start and an end time.")
        if end_ts <= start_ts:
            raise ValidationError(f"Plot {index + 1}: the end time must be after the start time.")
    elif start_ts is None:
        raise ValidationError(f"Plot {index + 1} is live, so it needs a start time.")

    y_min = _number(raw.get("y_min"), "Y-axis minimum")
    y_max = _number(raw.get("y_max"), "Y-axis maximum")
    if y_min is not None and y_max is not None and y_max <= y_min:
        raise ValidationError(f"Plot {index + 1}: the Y-axis maximum must be above the minimum.")

    plot = {
        "title": _text(raw.get("title"), 120, "Plot title") or f"Plot {index + 1}",
        "y_label": _text(raw.get("y_label") or raw.get("y_axis_name"), 120, "Y-axis name"),
        "mode": mode,
        "kind": default_kind,
        "start": _text(raw.get("start"), 40, "Start time"),
        "end": _text(raw.get("end"), 40, "End time") if mode == "static" else "",
        "series": series,
        "show_legend": bool(raw.get("show_legend", True)),
        "show_grid": bool(raw.get("show_grid", True)),
    }
    if y_min is not None:
        plot["y_min"] = y_min
    if y_max is not None:
        plot["y_max"] = y_max
    if function:
        plot["function"] = function
    return plot


def validate_profile(raw: Any, *, owner: str | None = None, palette: list[str] | None = None) -> dict[str, Any]:
    """Validate and normalise a whole profile document."""
    if not isinstance(raw, dict):
        raise ValidationError("A profile must be a table.")

    raw_plots = raw.get("plots") or []
    if not isinstance(raw_plots, list):
        raise ValidationError("A profile's plots must be a list.")
    if not raw_plots:
        raise ValidationError("A profile needs at least one plot.")
    if len(raw_plots) > MAX_PLOTS:
        raise ValidationError(f"A profile can show at most {MAX_PLOTS} plots.")

    plots = [validate_plot(p, i, palette) for i, p in enumerate(raw_plots)]
    layout = _choice(raw.get("layout"), LAYOUTS, default_layout(len(plots)), "Layout")

    profile = {
        "format_version": FORMAT_VERSION,
        "name": _text(raw.get("name"), 64, "Profile name"),
        "description": _text(raw.get("description"), 500, "Description"),
        "layout": layout,
        "created_by": _text(raw.get("created_by") or owner, 32, "Owner"),
        "created_at": _text(raw.get("created_at"), 40, "Created at") or utc_now_iso(),
        "updated_at": utc_now_iso(),
        "refresh_ms": int(max(250, min(60_000, _number(raw.get("refresh_ms"), "Refresh interval") or 1000))),
        "plots": plots,
    }
    for optional in ("published_from", "published_at", "published_by"):
        value = _text(raw.get(optional), 200, optional)
        if value:
            profile[optional] = value
    return profile


def default_layout(plot_count: int) -> str:
    return {1: "1x1", 2: "2x1", 3: "2x2", 4: "2x2"}.get(plot_count, "2x2")


def blank_profile(owner: str) -> dict[str, Any]:
    """A one-plot starting point for the "Create New" page."""
    return {
        "format_version": FORMAT_VERSION,
        "name": "",
        "description": "",
        "layout": "1x1",
        "created_by": owner,
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "refresh_ms": 1000,
        "plots": [
            {
                "title": "Plot 1",
                "y_label": "",
                "mode": "live",
                "kind": "line",
                "start": "",
                "end": "",
                "series": [],
                "show_legend": True,
                "show_grid": True,
            }
        ],
    }
