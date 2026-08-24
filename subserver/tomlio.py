"""Reading and writing the TOML documents that back profiles.

Python ships a TOML *reader* (:mod:`tomllib`) but no writer, and profiles are
specified to live on disk as ``.toml``.  Rather than take a dependency for it,
this module emits TOML for the value types a profile can contain: strings,
numbers, booleans, arrays, tables and arrays of tables.
"""

from __future__ import annotations

import math
import re
import tomllib
from pathlib import Path
from typing import Any

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def dump_key(key: str) -> str:
    return key if _BARE_KEY.match(key) else dump_string(key)


def dump_string(value: str) -> str:
    out = ['"']
    for ch in value:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def dump_value(value: Any) -> str:
    """Render a single value as an inline TOML expression."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if isinstance(value, str):
        return dump_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(dump_value(item) for item in value) + "]"
    if isinstance(value, dict):
        body = ", ".join(f"{dump_key(k)} = {dump_value(v)}" for k, v in value.items())
        return "{" + body + "}"
    if value is None:
        # TOML has no null; callers should omit the key instead.  Empty string
        # keeps a hand-edited file loadable rather than raising mid-write.
        return '""'
    raise TypeError(f"cannot serialise {type(value).__name__} to TOML")


def _is_table_array(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(i, dict) for i in value)


def _emit(data: dict[str, Any], prefix: list[str], lines: list[str]) -> None:
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict) and not _is_table_array(v)}
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}
    table_arrays = {k: v for k, v in data.items() if _is_table_array(v)}

    for key, value in scalars.items():
        if value is None:
            continue
        lines.append(f"{dump_key(key)} = {dump_value(value)}")

    for key, value in tables.items():
        path = prefix + [key]
        if lines and lines[-1] != "":
            lines.append("")
        lines.append("[" + ".".join(dump_key(p) for p in path) + "]")
        _emit(value, path, lines)

    for key, entries in table_arrays.items():
        path = prefix + [key]
        header = "[[" + ".".join(dump_key(p) for p in path) + "]]"
        for entry in entries:
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(header)
            _emit(entry, path, lines)


def dumps(data: dict[str, Any]) -> str:
    """Serialise a dictionary to a TOML document."""
    lines: list[str] = []
    _emit(data, [], lines)
    text = "\n".join(lines).strip()
    return text + "\n" if text else ""


def loads(text: str) -> dict[str, Any]:
    return tomllib.loads(text)


def read_file(path: str | Path) -> dict[str, Any]:
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def write_file(path: str | Path, data: dict[str, Any], header: str | None = None) -> None:
    """Write ``data`` to ``path`` atomically, so a crash cannot truncate it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = dumps(data)
    if header:
        body = "".join(f"# {line}\n" for line in header.splitlines()) + body
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)
