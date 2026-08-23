"""User-authored transform functions.

A function lives in ``Functions/{username}/{module}.py`` (or
``Functions/examples/{module}.py``) and is called with the data a plot selected:

    def smooth(data, window=10):
        '''Rolling mean of every selected variable.'''
        out = {}
        for name in data.names:
            out[f"{name} (avg)"] = rolling_mean(data.v(name), window)
        return out

The return value may be:

* a ``dict`` of ``label -> values``, ``label -> (times, values)``, or
  ``label -> scalar`` (drawn as a horizontal reference line);
* a bare array of values, paired with the first input series' timestamps;
* a scalar, or a tuple/list of scalars, drawn as reference lines;
* a matplotlib ``Figure``, rendered server-side to a PNG and shown in place of
  the plot.

SECURITY: these modules are ordinary Python executed inside the server process,
with the server's privileges.  Writing a function is equivalent to running code
on the host.  Only give accounts to people you would trust with a shell, or set
``allow_user_functions = false`` in ``config.toml``.
"""

from __future__ import annotations

import base64
import importlib.util
import inspect
import io
import logging
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .paths import join_relative, check_username

log = logging.getLogger(__name__)

MAX_OUTPUT_POINTS = 200_000
MAX_OUTPUT_SERIES = 24


class FunctionError(Exception):
    """A user function could not be loaded or failed while running."""


class PlotData:
    """The read-only view of selected data handed to a user function."""

    def __init__(self, series: dict[str, tuple[np.ndarray, np.ndarray]], start: float, end: float, now: float):
        self._series = series
        self.start = start
        self.end = end
        self.now = now

    @property
    def names(self) -> list[str]:
        """Variable names, in the order the profile lists them."""
        return list(self._series)

    def t(self, name: str) -> np.ndarray:
        """Timestamps (epoch seconds) for one variable."""
        return self._series[name][0]

    def v(self, name: str) -> np.ndarray:
        """Values for one variable."""
        return self._series[name][1]

    def series(self, name: str) -> tuple[np.ndarray, np.ndarray]:
        return self._series[name]

    def items(self):
        return self._series.items()

    def __len__(self) -> int:
        return len(self._series)

    def __contains__(self, name: object) -> bool:
        return name in self._series

    def __getitem__(self, name: str) -> tuple[np.ndarray, np.ndarray]:
        return self._series[name]


class FunctionRegistry:
    """Loads and caches user function modules, reloading them when edited."""

    def __init__(self, root: str | Path, enabled: bool = True):
        self.root = Path(root)
        self.enabled = enabled
        self._cache: dict[Path, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "examples").mkdir(parents=True, exist_ok=True)

    # -- Discovery --------------------------------------------------------------
    def owner_dir(self, scope: str, owner: str) -> Path:
        if scope == "examples":
            return join_relative(self.root, "examples")
        return join_relative(self.root, check_username(owner))

    def module_path(self, scope: str, owner: str, module: str) -> Path:
        if not module.isidentifier():
            raise FunctionError(f"{module!r} is not a valid module name.")
        return join_relative(self.owner_dir(scope, owner), f"{module}.py")

    def list_modules(self, scope: str, owner: str) -> list[str]:
        directory = self.owner_dir(scope, owner)
        if not directory.is_dir():
            return []
        return sorted(p.stem for p in directory.glob("*.py") if p.is_file() and p.stem.isidentifier())

    def catalog(self, user: str) -> list[dict[str, Any]]:
        """Every function available to ``user``, with its signature and docstring."""
        entries: list[dict[str, Any]] = []
        for scope, owner in (("user", user), ("examples", "examples")):
            for module in self.list_modules(scope if scope == "examples" else "user", owner):
                try:
                    loaded = self._load(scope, owner, module)
                except FunctionError as exc:
                    entries.append({"scope": scope, "owner": owner, "module": module,
                                    "name": "", "doc": str(exc), "signature": "", "error": True})
                    continue
                for name, fn in self._public_functions(loaded).items():
                    entries.append({
                        "scope": scope,
                        "owner": owner if scope == "user" else "examples",
                        "module": module,
                        "name": name,
                        "doc": (inspect.getdoc(fn) or "").strip(),
                        "signature": self._signature(fn),
                        "error": False,
                    })
        return entries

    @staticmethod
    def _signature(fn: Callable[..., Any]) -> str:
        try:
            params = list(inspect.signature(fn).parameters.values())[1:]  # skip `data`
            return ", ".join(str(p) for p in params)
        except (TypeError, ValueError):
            return ""

    @staticmethod
    def _public_functions(module: Any) -> dict[str, Callable[..., Any]]:
        exported = getattr(module, "__all__", None)
        result = {}
        for name, obj in vars(module).items():
            if name.startswith("_") or not callable(obj):
                continue
            if exported is not None and name not in exported:
                continue
            if inspect.isfunction(obj) and obj.__module__ != module.__name__:
                continue  # an import, not something defined here
            if inspect.isclass(obj):
                continue
            result[name] = obj
        return result

    # -- Loading ----------------------------------------------------------------
    def _load(self, scope: str, owner: str, module: str) -> Any:
        if not self.enabled:
            raise FunctionError("User functions are disabled in config.toml.")
        path = self.module_path(scope, owner, module)
        if not path.is_file():
            raise FunctionError(f"No function module named {module!r} for {owner!r}.")
        mtime = path.stat().st_mtime
        with self._lock:
            cached = self._cache.get(path)
            if cached and cached[0] == mtime:
                return cached[1]
        # Namespaced so two users may both have a module called "helpers".
        qualified = f"subserver_userfunc_{scope}_{owner}_{module}".replace("-", "_")
        spec = importlib.util.spec_from_file_location(qualified, path)
        if spec is None or spec.loader is None:
            raise FunctionError(f"Could not load {path.name}.")
        loaded = importlib.util.module_from_spec(spec)
        sys.modules[qualified] = loaded
        try:
            spec.loader.exec_module(loaded)
        except Exception as exc:  # noqa: BLE001 - report the author's error to them
            sys.modules.pop(qualified, None)
            raise FunctionError(f"{module}.py failed to import: {type(exc).__name__}: {exc}") from exc
        with self._lock:
            self._cache[path] = (mtime, loaded)
        return loaded

    def resolve(self, spec: dict[str, Any], default_owner: str) -> Callable[..., Any]:
        scope = spec.get("scope") or "user"
        owner = spec.get("owner") or default_owner
        module = str(spec.get("module") or "")
        name = str(spec.get("name") or "")
        loaded = self._load(scope, owner if scope == "user" else "examples", module)
        fn = getattr(loaded, name, None)
        if fn is None or not callable(fn) or name.startswith("_"):
            raise FunctionError(f"{module}.py has no function called {name!r}.")
        return fn

    # -- Execution --------------------------------------------------------------
    def run(self, spec: dict[str, Any], data: PlotData, default_owner: str) -> dict[str, Any]:
        """Run a user function and normalise whatever it returned.

        Returns ``{"series": [...], "lines": [...], "image": "<base64 png>|None"}``.
        Any exception raised by the author's code is turned into a message the
        UI shows on the plot rather than a 500.
        """
        fn = self.resolve(spec, default_owner)
        args = dict(spec.get("args") or {})
        try:
            result = fn(data, **args)
        except TypeError as exc:
            raise FunctionError(f"{spec.get('name')}() rejected its arguments: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            detail = traceback.format_exc(limit=3).strip().splitlines()[-1]
            raise FunctionError(f"{spec.get('name')}() raised {detail}") from exc
        return self._normalise(result, data, spec)

    def _normalise(self, result: Any, data: PlotData, spec: dict[str, Any]) -> dict[str, Any]:
        image = _render_figure(result)
        if image is not None:
            return {"series": [], "lines": [], "image": image}

        reference_t = data.t(data.names[0]) if data.names else np.empty(0, dtype=np.float64)
        series: list[dict[str, Any]] = []
        lines: list[dict[str, Any]] = []

        def add(label: str, value: Any) -> None:
            if len(series) + len(lines) >= MAX_OUTPUT_SERIES:
                raise FunctionError(f"A function may return at most {MAX_OUTPUT_SERIES} results.")
            if isinstance(value, (int, float, np.number)) and not isinstance(value, bool):
                lines.append({"label": str(label), "value": float(value)})
                return
            if isinstance(value, tuple) and len(value) == 2:
                t_arr = _as_array(value[0], f"{label} timestamps")
                v_arr = _as_array(value[1], f"{label} values")
            else:
                v_arr = _as_array(value, f"{label} values")
                t_arr = reference_t
            if t_arr.size != v_arr.size:
                raise FunctionError(
                    f"{label!r}: got {v_arr.size} values for {t_arr.size} timestamps. "
                    "Return (times, values) when the lengths differ from the input."
                )
            if v_arr.size > MAX_OUTPUT_POINTS:
                raise FunctionError(f"{label!r}: {v_arr.size} points exceeds the {MAX_OUTPUT_POINTS} limit.")
            series.append({"label": str(label), "t": t_arr, "v": v_arr})

        if isinstance(result, dict):
            for label, value in result.items():
                add(str(label), value)
        elif isinstance(result, (int, float, np.number)) and not isinstance(result, bool):
            add(str(spec.get("name") or "result"), result)
        elif isinstance(result, tuple) and len(result) == 2 and _looks_like_series_pair(result):
            add(str(spec.get("name") or "result"), result)
        elif isinstance(result, (list, tuple)):
            if all(isinstance(v, (int, float, np.number)) and not isinstance(v, bool) for v in result):
                for i, value in enumerate(result):
                    add(f"{spec.get('name') or 'result'} {i + 1}", value)
            else:
                for i, value in enumerate(result):
                    add(f"{spec.get('name') or 'result'} {i + 1}", value)
        elif result is None:
            raise FunctionError(f"{spec.get('name')}() returned None; it must return data or a figure.")
        else:
            add(str(spec.get("name") or "result"), result)
        return {"series": series, "lines": lines, "image": None}


def _looks_like_series_pair(value: tuple[Any, Any]) -> bool:
    return all(hasattr(item, "__len__") for item in value)


def _as_array(value: Any, what: str) -> np.ndarray:
    try:
        arr = np.asarray(value, dtype=np.float64).ravel()
    except (TypeError, ValueError) as exc:
        raise FunctionError(f"{what} must be numbers, not {type(value).__name__}.") from exc
    return arr


def _render_figure(result: Any) -> str | None:
    """Return a base64 PNG if ``result`` is a matplotlib figure, else None."""
    module = type(result).__module__ or ""
    if not module.startswith("matplotlib"):
        return None
    try:
        import matplotlib
        matplotlib.use("Agg", force=False)
        from matplotlib.figure import Figure
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise FunctionError(
            "This function returned a matplotlib figure, but matplotlib is not installed on the server."
        ) from exc
    if not isinstance(result, Figure):
        if hasattr(result, "figure") and isinstance(getattr(result, "figure"), Figure):
            result = result.figure
        else:
            return None
    buffer = io.BytesIO()
    try:
        result.patch.set_facecolor("#1e1e1e")
        result.savefig(buffer, format="png", dpi=110, bbox_inches="tight", facecolor=result.get_facecolor())
    finally:
        try:
            import matplotlib.pyplot as plt
            plt.close(result)  # otherwise every refresh leaks a figure
        except Exception:  # noqa: BLE001
            pass
    return base64.b64encode(buffer.getvalue()).decode("ascii")
