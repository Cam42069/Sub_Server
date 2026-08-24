"""Profile storage.

On-disk layout, exactly as specified::

    Profiles/shared/{user}/.../{profile_name}.toml     published, append-only
    Profiles/private/{user}/.../{profile_name}.toml    the user's own work
    Profiles/examples/.../{profile_name}.toml          shipped read-only samples

Users create their own folder trees underneath their username directory in both
the private and shared spaces.

The shared space is append-only.  A publish never replaces an existing file: if
``bruh.toml`` is already there, the new document is written as ``bruh_1.toml``,
then ``bruh_2.toml``, and so on.  That rule is enforced with an exclusive
create, so two simultaneous publishes cannot both win the same name.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterator

from . import tomlio
from .models import ValidationError, validate_profile
from .paths import UnsafePath, check_name, check_username, join_relative, split_folder

SCOPES = ("private", "shared", "examples")
MAX_PROFILE_BYTES = 512 * 1024
MAX_PUBLISH_ATTEMPTS = 1000


@dataclass(frozen=True)
class ProfileRef:
    """Where a profile lives, in terms the URL layer can round-trip."""

    scope: str
    owner: str
    folder: str
    name: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)

    @property
    def display_path(self) -> str:
        parts = [self.scope]
        if self.owner:
            parts.append(self.owner)
        if self.folder:
            parts.append(self.folder)
        parts.append(self.name)
        return "/".join(parts)


class ProfileError(Exception):
    """A profile operation was refused; the message is safe to show a user."""


class ProfileStore:
    """Filesystem-backed profile repository."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        for scope in SCOPES:
            (self.root / scope).mkdir(parents=True, exist_ok=True)

    # -- Path resolution --------------------------------------------------------
    def scope_root(self, scope: str) -> Path:
        if scope not in SCOPES:
            raise ProfileError(f"Unknown profile area {scope!r}.")
        return (self.root / scope).resolve()

    def owner_root(self, scope: str, owner: str | None) -> Path:
        base = self.scope_root(scope)
        if scope == "examples":
            return base
        if not owner:
            raise ProfileError("A user is required for that profile area.")
        return join_relative(base, check_username(owner))

    def directory(self, scope: str, owner: str | None, folder: str | None) -> Path:
        return join_relative(self.owner_root(scope, owner), *split_folder(folder))

    def file_path(self, ref: ProfileRef) -> Path:
        name = check_name(ref.name, "profile name")
        return join_relative(self.directory(ref.scope, ref.owner, ref.folder), f"{name}.toml")

    # -- Permissions ------------------------------------------------------------
    def can_read(self, ref: ProfileRef, user: str) -> bool:
        if ref.scope in ("shared", "examples"):
            return True
        return ref.owner == user

    def can_write(self, ref: ProfileRef, user: str) -> bool:
        """Only the private space is writable; shared is append-only via publish."""
        return ref.scope == "private" and ref.owner == user

    def _require_read(self, ref: ProfileRef, user: str) -> None:
        if not self.can_read(ref, user):
            raise ProfileError("You do not have access to that profile.")

    # -- Listing ----------------------------------------------------------------
    def _iter_files(self, base: Path) -> Iterator[Path]:
        if not base.is_dir():
            return
        for path in sorted(base.rglob("*.toml")):
            if path.is_file():
                yield path

    def _ref_for(self, scope: str, owner: str, path: Path) -> ProfileRef:
        owner_root = self.owner_root(scope, owner or None)
        rel = path.parent.resolve().relative_to(owner_root)
        folder = "" if str(rel) == "." else rel.as_posix()
        return ProfileRef(scope=scope, owner=owner, folder=folder, name=path.stem)

    def _summarise(self, scope: str, owner: str, path: Path) -> dict[str, Any]:
        ref = self._ref_for(scope, owner, path)
        entry: dict[str, Any] = {
            **ref.as_dict(),
            "display_path": ref.display_path,
            "modified": path.stat().st_mtime,
            "title": ref.name,
            "description": "",
            "plot_count": 0,
            "created_by": owner,
            "error": "",
        }
        try:
            data = tomlio.read_file(path)
            entry["title"] = str(data.get("name") or ref.name)[:64]
            entry["description"] = str(data.get("description", ""))[:200]
            plots = data.get("plots")
            entry["plot_count"] = len(plots) if isinstance(plots, list) else 0
            entry["created_by"] = str(data.get("created_by") or owner)[:32]
        except Exception as exc:  # noqa: BLE001 - a broken file must not hide the rest
            entry["error"] = f"Could not read this file: {exc}"
        return entry

    def list_profiles(self, scope: str, owner: str | None = None) -> list[dict[str, Any]]:
        """List profiles in one area.  ``owner=None`` in shared lists everyone."""
        entries: list[dict[str, Any]] = []
        if scope == "examples":
            for path in self._iter_files(self.scope_root("examples")):
                entries.append(self._summarise("examples", "", path))
        elif owner:
            base = self.owner_root(scope, owner)
            for path in self._iter_files(base):
                entries.append(self._summarise(scope, check_username(owner), path))
        else:
            if scope != "shared":
                raise ProfileError("Only the shared area can be listed across users.")
            base = self.scope_root("shared")
            for user_dir in sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []:
                try:
                    user = check_username(user_dir.name)
                except UnsafePath:
                    continue  # a stray directory that no account could have made
                for path in self._iter_files(user_dir):
                    entries.append(self._summarise("shared", user, path))
        entries.sort(key=lambda e: (e["owner"], e["folder"], e["name"].lower()))
        return entries

    def list_visible(self, user: str) -> dict[str, list[dict[str, Any]]]:
        return {
            "private": self.list_profiles("private", user),
            "shared": self.list_profiles("shared"),
            "examples": self.list_profiles("examples"),
        }

    def list_folders(self, scope: str, owner: str | None) -> list[str]:
        """Relative folder paths beneath a user's (or the examples) root."""
        base = self.owner_root(scope, owner)
        if not base.is_dir():
            return [""]
        folders = {""}
        for path in base.rglob("*"):
            if path.is_dir():
                folders.add(path.relative_to(base).as_posix())
        return sorted(folders)

    def create_folder(self, scope: str, owner: str, folder: str) -> str:
        if scope not in ("private", "shared"):
            raise ProfileError("Folders can only be created in your private or shared space.")
        segments = split_folder(folder)
        if not segments:
            raise ProfileError("Enter a folder name.")
        target = join_relative(self.owner_root(scope, owner), *segments)
        target.mkdir(parents=True, exist_ok=True)
        return "/".join(segments)

    def delete_folder(self, scope: str, owner: str, folder: str) -> None:
        """Remove one of the user's own folders, only when it is empty."""
        if scope not in ("private", "shared"):
            raise ProfileError("You can only remove folders in your private or shared space.")
        segments = split_folder(folder)
        if not segments:
            raise ProfileError("Choose a folder to remove.")
        target = join_relative(self.owner_root(scope, owner), *segments)
        if not target.is_dir():
            raise ProfileError("That folder does not exist.")
        if any(target.iterdir()):
            raise ProfileError("That folder is not empty. Remove what is inside it first.")
        target.rmdir()

    # -- Reading ----------------------------------------------------------------
    def load(self, ref: ProfileRef, user: str, *, palette: list[str] | None = None) -> dict[str, Any]:
        self._require_read(ref, user)
        path = self.file_path(ref)
        if not path.is_file():
            raise ProfileError(f"Profile {ref.display_path!r} was not found.")
        if path.stat().st_size > MAX_PROFILE_BYTES:
            raise ProfileError("That profile file is too large to load.")
        try:
            raw = tomlio.read_file(path)
        except Exception as exc:  # noqa: BLE001
            raise ProfileError(f"Could not parse that profile: {exc}") from exc
        try:
            profile = validate_profile(raw, owner=ref.owner, palette=palette)
        except ValidationError as exc:
            raise ProfileError(f"That profile is not valid: {exc}") from exc
        profile["name"] = profile["name"] or ref.name
        profile["_ref"] = ref.as_dict()
        return profile

    # -- Writing ----------------------------------------------------------------
    def save_private(self, user: str, folder: str, name: str, profile: dict[str, Any]) -> ProfileRef:
        """Create or replace a profile in the user's private space."""
        name = check_name(name, "profile name")
        ref = ProfileRef(scope="private", owner=check_username(user), folder="/".join(split_folder(folder)), name=name)
        if not self.can_write(ref, user):
            raise ProfileError("You can only save into your own private space.")
        document = dict(profile)
        document.pop("_ref", None)
        document["name"] = name
        document.setdefault("created_by", user)
        path = self.file_path(ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        tomlio.write_file(path, document, header=f"Sub_Server profile - saved by {user}")
        return ref

    def publish(self, user: str, folder: str, name: str, profile: dict[str, Any]) -> ProfileRef:
        """Copy a profile into the shared space without ever replacing a file.

        Returns the reference actually written, whose name may carry a
        ``_1``/``_2``/... suffix if the requested one was taken.
        """
        user = check_username(user)
        name = check_name(name, "profile name")
        folder_path = "/".join(split_folder(folder))
        directory = self.directory("shared", user, folder_path)
        directory.mkdir(parents=True, exist_ok=True)

        document = dict(profile)
        document.pop("_ref", None)
        document.setdefault("created_by", user)
        document["published_by"] = user
        document["published_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")

        for attempt in range(MAX_PUBLISH_ATTEMPTS):
            candidate = name if attempt == 0 else f"{name}_{attempt}"
            target = join_relative(directory, f"{candidate}.toml")
            document["name"] = candidate
            body = tomlio.dumps(document)
            header = f"# Sub_Server profile - published by {user}\n"
            try:
                # O_EXCL makes "does it exist?" and "create it" one atomic step,
                # so concurrent publishes cannot collide on the same name.
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(header + body)
            return ProfileRef(scope="shared", owner=user, folder=folder_path, name=candidate)
        raise ProfileError(
            f"Could not publish: {MAX_PUBLISH_ATTEMPTS} profiles named like {name!r} already exist there."
        )

    def delete(self, ref: ProfileRef, user: str) -> None:
        """Delete a private profile.  Shared and example profiles are immutable."""
        if ref.scope != "private":
            raise ProfileError("Only profiles in your private space can be deleted.")
        if ref.owner != user:
            raise ProfileError("You can only delete your own profiles.")
        path = self.file_path(ref)
        if not path.is_file():
            raise ProfileError("That profile does not exist.")
        path.unlink()

    def ensure_user_space(self, user: str) -> None:
        """Create the per-user directories a new account needs."""
        for scope in ("private", "shared"):
            self.owner_root(scope, user).mkdir(parents=True, exist_ok=True)
