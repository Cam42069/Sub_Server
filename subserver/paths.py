"""Path validation for the user-controlled parts of the Profiles tree.

Every user-supplied name -- a username, a folder segment, a profile name --
passes through here before it is joined onto a filesystem path.  The rules are
deliberately strict: a small allow-listed character set plus a final
containment check, so nothing outside the intended root is reachable even if a
new caller forgets to validate first.
"""

from __future__ import annotations

import re
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$")
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,31}$")
RESERVED = {".", "..", "", "con", "prn", "aux", "nul"}


class UnsafePath(ValueError):
    """Raised when a user-supplied path component or path is not acceptable."""


def check_name(name: str, what: str = "name") -> str:
    """Validate a single path segment (folder or profile name)."""
    name = (name or "").strip()
    if not NAME_RE.match(name) or name.lower() in RESERVED or name.endswith("."):
        raise UnsafePath(
            f"Invalid {what}: use 1-64 characters from letters, digits, space, '.', '_' or '-', "
            "starting with a letter or digit."
        )
    return name


def check_username(name: str) -> str:
    """Validate and normalise a username.  Usernames double as directory names."""
    name = (name or "").strip().lower()
    if not USERNAME_RE.match(name) or name in RESERVED:
        raise UnsafePath(
            "Invalid username: 3-32 characters, lowercase letters, digits, '_' or '-', "
            "starting with a letter or digit."
        )
    return name


def split_folder(folder: str | None) -> list[str]:
    """Split a user-supplied relative folder into validated segments."""
    if not folder:
        return []
    raw = str(folder).replace("\\", "/")
    segments = [seg for seg in raw.split("/") if seg not in ("", ".")]
    return [check_name(seg, "folder name") for seg in segments]


def join_relative(root: Path, *parts: str) -> Path:
    """Join validated ``parts`` onto ``root`` and confirm the result stays inside.

    The containment check is the backstop: even a validation bug upstream
    cannot produce a path outside ``root``.
    """
    root = Path(root).resolve()
    candidate = root.joinpath(*parts)
    try:
        resolved = candidate.resolve()
    except OSError as exc:  # e.g. a symlink loop
        raise UnsafePath(f"Cannot resolve path: {exc}") from exc
    if resolved != root and root not in resolved.parents:
        raise UnsafePath("Path escapes its allowed root.")
    return resolved


def relative_folder(path: Path, root: Path) -> str:
    """Render ``path``'s parent directory relative to ``root`` using '/'."""
    try:
        rel = path.parent.resolve().relative_to(Path(root).resolve())
    except ValueError:
        return ""
    return "" if str(rel) == "." else rel.as_posix()
