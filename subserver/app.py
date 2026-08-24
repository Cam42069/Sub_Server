"""The HTTPS application: routes, session handling and the JSON API."""

from __future__ import annotations

import functools
import logging
import secrets
from typing import Any, Callable

from flask import (
    Flask, abort, flash, g, jsonify, redirect, render_template,
    request, session, url_for,
)
from werkzeug.exceptions import HTTPException

from . import PROGRAM_NAME, __version__
from .auth import AuthError, UserStore, load_or_create_secret
from .config import Config
from .models import (
    DEFAULT_PALETTE, LAYOUTS, MAX_PLOTS, MAX_SERIES_PER_PLOT, SERIES_KINDS,
    ValidationError, blank_profile, validate_profile,
)
from .paths import UnsafePath
from .profiles import ProfileError, ProfileRef, ProfileStore
from .query import QueryEngine
from .ringstore import RamStore
from .subscriber import Subscriber
from .timeutil import format_epoch, format_span
from .userfuncs import FunctionRegistry

log = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 2 * 1024 * 1024


class Services:
    """The long-lived objects the request handlers share."""

    def __init__(
        self,
        config: Config,
        store: RamStore,
        subscriber: Subscriber | None,
        users: UserStore,
        profiles: ProfileStore,
        functions: FunctionRegistry,
        queries: QueryEngine,
    ):
        self.config = config
        self.store = store
        self.subscriber = subscriber
        self.users = users
        self.profiles = profiles
        self.functions = functions
        self.queries = queries


def login_required(view: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not g.get("user"):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "Not signed in."}), 401
            return redirect(url_for("login", next=request.full_path if request.query_string else request.path))
        return view(*args, **kwargs)

    return wrapper


def api_errors(view: Callable[..., Any]) -> Callable[..., Any]:
    """Turn the expected domain exceptions into clean JSON error responses."""

    @functools.wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return view(*args, **kwargs)
        except (ProfileError, ValidationError, UnsafePath, AuthError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except HTTPException:
            # abort(...) and lazily-raised errors such as 413 carry their own
            # status; let Flask's handlers answer them instead of masking a 500.
            raise
        except Exception:  # noqa: BLE001
            log.exception("unhandled error in %s", view.__name__)
            return jsonify({"ok": False, "error": "The server hit an unexpected error. Check its log."}), 500

    return wrapper


def create_app(services: Services) -> Flask:
    config = services.config
    app = Flask(__name__)
    app.secret_key = load_or_create_secret(config.data_dir / "session.key")
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=True,  # the server only ever speaks HTTPS
        PERMANENT_SESSION_LIFETIME=int(config.get("security", "session_lifetime_hours", 12)) * 3600,
        MAX_CONTENT_LENGTH=MAX_REQUEST_BYTES,
        JSON_SORT_KEYS=False,
    )
    app.extensions["services"] = services

    # -- Request lifecycle ------------------------------------------------------
    @app.before_request
    def load_session_user() -> Any:
        g.services = services
        g.user = None
        username = session.get("user")
        if username:
            record = services.users.get(username)
            if record is None:
                session.clear()  # the account was removed from users.json
            else:
                g.user = record["username"]
                g.display_name = record.get("display_name") or record["username"]
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            if not _csrf_ok():
                if request.path.startswith("/api/"):
                    return jsonify({"ok": False, "error": "Session expired. Reload the page and try again."}), 403
                flash("Your session expired. Please try again.", "error")
                return redirect(request.path)
        return None

    @app.after_request
    def security_headers(response: Any) -> Any:
        # Everything the pages need is served from this origin, so the policy
        # forbids outside script, style and connection sources.  Inline *styles*
        # are allowed because series colours, meter widths and show/hide toggles
        # are set from data at runtime; inline *scripts* stay blocked, which is
        # what actually stops injected markup from executing.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    @app.context_processor
    def template_globals() -> dict[str, Any]:
        return {
            "program_name": PROGRAM_NAME,
            "program_version": __version__,
            "current_user": g.get("user"),
            "display_name": g.get("display_name", ""),
            "csrf_token": _csrf_token(),
        }

    # -- Authentication ---------------------------------------------------------
    @app.route("/login", methods=["GET", "POST"])
    def login() -> Any:
        if g.get("user"):
            return redirect(url_for("home"))
        allow_registration = bool(config.get("security", "allow_registration", True))
        if request.method == "POST":
            try:
                record = services.users.authenticate(
                    request.form.get("username", ""), request.form.get("password", "")
                )
            except AuthError as exc:
                flash(str(exc), "error")
                return render_template("login.html", allow_registration=allow_registration,
                                       username=request.form.get("username", "")), 401
            _start_session(record["username"])
            services.profiles.ensure_user_space(record["username"])
            return redirect(_safe_next(request.args.get("next")) or url_for("home"))
        return render_template("login.html", allow_registration=allow_registration, username="")

    @app.route("/register", methods=["GET", "POST"])
    def register() -> Any:
        if not config.get("security", "allow_registration", True):
            abort(404)
        if g.get("user"):
            return redirect(url_for("home"))
        if request.method == "POST":
            username = request.form.get("username", "")
            display_name = request.form.get("display_name", "")
            password = request.form.get("password", "")
            confirm = request.form.get("confirm", "")
            if password != confirm:
                flash("The two passwords do not match.", "error")
                return render_template("register.html", username=username, display_name=display_name), 400
            try:
                record = services.users.create(username, password, display_name)
            except AuthError as exc:
                flash(str(exc), "error")
                return render_template("register.html", username=username, display_name=display_name), 400
            services.profiles.ensure_user_space(record["username"])
            _start_session(record["username"])
            flash(f"Welcome, {record['display_name']}! Your account is ready.", "success")
            return redirect(url_for("home"))
        return render_template("register.html", username="", display_name="")

    @app.post("/logout")
    def logout() -> Any:
        session.clear()
        flash("You have been signed out.", "success")
        return redirect(url_for("login"))

    # -- Pages ------------------------------------------------------------------
    @app.route("/")
    @login_required
    def home() -> Any:
        visible = services.profiles.list_visible(g.user)
        recent = sorted(visible["private"], key=lambda e: e["modified"], reverse=True)[:6]
        return render_template(
            "home.html",
            recent=recent,
            counts={key: len(value) for key, value in visible.items()},
            status=_status_payload(services),
        )

    @app.route("/about")
    @login_required
    def about() -> Any:
        return render_template("about.html", status=_status_payload(services))

    @app.route("/profiles/new")
    @login_required
    def profile_new() -> Any:
        return render_template(
            "profile_edit.html",
            profile=blank_profile(g.user),
            ref=None,
            mode="new",
            editor_config=_editor_config(services),
        )

    @app.route("/profiles/edit")
    @login_required
    def profile_edit() -> Any:
        ref = _ref_from_args(request.args)
        try:
            profile = services.profiles.load(ref, g.user, palette=_palette(services, g.user))
        except (ProfileError, UnsafePath) as exc:
            flash(str(exc), "error")
            return redirect(url_for("profile_load"))
        # Anything not in the user's private space opens as a copy, because
        # shared and example profiles are read-only.
        editable = services.profiles.can_write(ref, g.user)
        return render_template(
            "profile_edit.html",
            profile=profile,
            ref=ref if editable else None,
            source_ref=ref,
            mode="edit" if editable else "copy",
            editor_config=_editor_config(services),
        )

    @app.route("/profiles/load")
    @login_required
    def profile_load() -> Any:
        visible = services.profiles.list_visible(g.user)
        return render_template(
            "profile_list.html",
            title="Load a profile",
            subtitle="Your saved profiles, plus the shared and example libraries.",
            groups=[
                ("Your profiles", visible["private"], True),
                ("Examples", visible["examples"], False),
            ],
            folders=services.profiles.list_folders("private", g.user),
        )

    @app.route("/profiles/browse")
    @login_required
    def profile_browse() -> Any:
        entries = services.profiles.list_profiles("shared")
        return render_template(
            "profile_list.html",
            title="Browse shared profiles",
            subtitle="Profiles published by everyone on this server. Published profiles are never overwritten.",
            groups=[("Shared", entries, False)],
            folders=[],
        )

    @app.route("/profiles/view")
    @login_required
    def profile_view() -> Any:
        ref = _ref_from_args(request.args)
        try:
            profile = services.profiles.load(ref, g.user, palette=_palette(services, g.user))
        except (ProfileError, UnsafePath) as exc:
            flash(str(exc), "error")
            return redirect(url_for("profile_load"))
        return render_template(
            "profile_view.html",
            profile=profile,
            ref=ref,
            can_edit=services.profiles.can_write(ref, g.user),
            refresh_ms=int(config.get("plots", "live_refresh_ms", 1000)),
        )

    # -- JSON API ---------------------------------------------------------------
    @app.get("/api/status")
    @login_required
    @api_errors
    def api_status() -> Any:
        return jsonify({"ok": True, **_status_payload(services)})

    @app.get("/api/variables")
    @login_required
    @api_errors
    def api_variables() -> Any:
        names = services.store.variables()
        known = services.subscriber.node_variables if services.subscriber else []
        for name in known:
            if name not in names:
                names.append(name)
        return jsonify({"ok": True, "variables": sorted(names)})

    @app.get("/api/functions")
    @login_required
    @api_errors
    def api_functions() -> Any:
        if not services.functions.enabled:
            return jsonify({"ok": True, "functions": [], "enabled": False})
        return jsonify({"ok": True, "functions": services.functions.catalog(g.user), "enabled": True})

    @app.get("/api/profiles")
    @login_required
    @api_errors
    def api_profiles() -> Any:
        return jsonify({"ok": True, "profiles": services.profiles.list_visible(g.user)})

    @app.get("/api/folders")
    @login_required
    @api_errors
    def api_folders() -> Any:
        scope = request.args.get("scope", "private")
        if scope not in ("private", "shared"):
            raise ProfileError("Folders can only be listed for your private or shared space.")
        return jsonify({"ok": True, "folders": services.profiles.list_folders(scope, g.user)})

    @app.post("/api/folders")
    @login_required
    @api_errors
    def api_create_folder() -> Any:
        body = _json_body()
        folder = services.profiles.create_folder(
            str(body.get("scope", "private")), g.user, str(body.get("folder", ""))
        )
        return jsonify({"ok": True, "folder": folder})

    @app.delete("/api/folders")
    @login_required
    @api_errors
    def api_delete_folder() -> Any:
        body = _json_body()
        services.profiles.delete_folder(str(body.get("scope", "private")), g.user, str(body.get("folder", "")))
        return jsonify({"ok": True})

    @app.post("/api/profile/save")
    @login_required
    @api_errors
    def api_save_profile() -> Any:
        body = _json_body()
        profile = validate_profile(body.get("profile"), owner=g.user, palette=_palette(services, g.user))
        name = str(body.get("name") or profile.get("name") or "")
        if not name:
            raise ProfileError("Give the profile a name before saving.")
        folder = str(body.get("folder") or "")
        ref = services.profiles.save_private(g.user, folder, name, profile)
        return jsonify({"ok": True, "ref": ref.as_dict(), "message": f"Saved to {ref.display_path}."})

    @app.post("/api/profile/publish")
    @login_required
    @api_errors
    def api_publish_profile() -> Any:
        body = _json_body()
        profile = validate_profile(body.get("profile"), owner=g.user, palette=_palette(services, g.user))
        name = str(body.get("name") or profile.get("name") or "")
        if not name:
            raise ProfileError("Give the profile a name before publishing.")
        folder = str(body.get("folder") or "")
        ref = services.profiles.publish(g.user, folder, name, profile)
        renamed = ref.name != name
        message = (
            f"Published as {ref.display_path}. A profile named '{name}' was already there, "
            f"so this one was saved as '{ref.name}'."
            if renamed
            else f"Published to {ref.display_path}."
        )
        return jsonify({"ok": True, "ref": ref.as_dict(), "renamed": renamed, "message": message})

    @app.post("/api/profile/delete")
    @login_required
    @api_errors
    def api_delete_profile() -> Any:
        ref = _ref_from_args(_json_body())
        services.profiles.delete(ref, g.user)
        return jsonify({"ok": True, "message": f"Deleted {ref.display_path}."})

    @app.post("/api/profile/validate")
    @login_required
    @api_errors
    def api_validate_profile() -> Any:
        profile = validate_profile(_json_body().get("profile"), owner=g.user, palette=_palette(services, g.user))
        return jsonify({"ok": True, "profile": profile})

    @app.post("/api/data")
    @login_required
    @api_errors
    def api_data() -> Any:
        """Data for one profile: either a saved reference or an unsaved draft."""
        body = _json_body()
        cursors = body.get("cursors") or {}
        if not isinstance(cursors, dict):
            cursors = {}
        clean_cursors: dict[str, float] = {}
        for key, value in cursors.items():
            try:
                clean_cursors[str(key)] = float(value)
            except (TypeError, ValueError):
                continue

        if body.get("ref"):
            ref = _ref_from_args(body["ref"])
            profile = services.profiles.load(ref, g.user, palette=_palette(services, g.user))
            owner = ref.owner or g.user
        else:
            profile = validate_profile(body.get("profile"), owner=g.user, palette=_palette(services, g.user))
            owner = g.user

        only: set[int] | None = None
        requested = body.get("plots")
        if isinstance(requested, list):
            only = set()
            for value in requested:
                try:
                    only.add(int(value))
                except (TypeError, ValueError):
                    continue

        plots = services.queries.profile_payload(
            profile, owner=owner, cursors=clean_cursors, only=only
        )
        return jsonify({
            "ok": True,
            "name": profile.get("name", ""),
            "layout": profile.get("layout", "2x2"),
            "refresh_ms": profile.get("refresh_ms", 1000),
            "plots": plots,
        })

    @app.post("/api/preferences")
    @login_required
    @api_errors
    def api_preferences() -> Any:
        body = _json_body()
        palette = body.get("palette")
        preferences = services.users.preferences(g.user)
        if isinstance(palette, list):
            from .models import normalise_color
            cleaned = [normalise_color(c, "#4fc3f7") for c in palette[:24]]
            preferences["palette"] = cleaned or DEFAULT_PALETTE
        if "default_kind" in body and body["default_kind"] in SERIES_KINDS:
            preferences["default_kind"] = body["default_kind"]
        services.users.set_preferences(g.user, preferences)
        return jsonify({"ok": True, "preferences": preferences})

    @app.get("/favicon.ico")
    def favicon() -> Any:
        return app.send_static_file("favicon.svg")

    # -- Errors -----------------------------------------------------------------
    @app.errorhandler(404)
    def not_found(_exc: Any) -> Any:
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "No such endpoint."}), 404
        return render_template("error.html", code=404, message="That page does not exist."), 404

    @app.errorhandler(413)
    def too_large(_exc: Any) -> Any:
        return jsonify({"ok": False, "error": "That request was too large."}), 413

    @app.errorhandler(500)
    def server_error(_exc: Any) -> Any:
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "Internal server error."}), 500
        return render_template("error.html", code=500, message="The server hit an unexpected error."), 500

    return app


# -- Helpers --------------------------------------------------------------------
def _start_session(username: str) -> None:
    session.clear()  # a fresh session id on login, so a fixated one is discarded
    session["user"] = username
    session["csrf"] = secrets.token_urlsafe(32)
    session.permanent = True


def _csrf_token() -> str:
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token


def _csrf_ok() -> bool:
    expected = session.get("csrf")
    if not expected:
        # No session yet: the login and registration forms carry a token minted
        # for the anonymous session, so compare against that.
        expected = _csrf_token()
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
    if not supplied and request.is_json:
        body = request.get_json(silent=True) or {}
        supplied = str(body.get("csrf_token", "")) if isinstance(body, dict) else ""
    return secrets.compare_digest(str(supplied), str(expected))


def _json_body() -> dict[str, Any]:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ProfileError("Expected a JSON object in the request body.")
    return body


def _ref_from_args(source: Any) -> ProfileRef:
    getter = source.get if hasattr(source, "get") else lambda k, d=None: d
    scope = str(getter("scope", "") or "")
    if scope not in ("private", "shared", "examples"):
        raise ProfileError("Specify which profile area to use.")
    return ProfileRef(
        scope=scope,
        owner=str(getter("owner", "") or ""),
        folder=str(getter("folder", "") or ""),
        name=str(getter("name", "") or ""),
    )


def _safe_next(target: str | None) -> str | None:
    """Only allow same-site relative redirects after login."""
    if not target or not target.startswith("/") or target.startswith("//"):
        return None
    return target


def _palette(services: Services, user: str) -> list[str]:
    preferences = services.users.preferences(user)
    palette = preferences.get("palette")
    if isinstance(palette, list) and palette:
        return [str(c) for c in palette]
    return list(DEFAULT_PALETTE)


def _editor_config(services: Services) -> dict[str, Any]:
    return {
        "max_plots": MAX_PLOTS,
        "max_series": MAX_SERIES_PER_PLOT,
        "layouts": list(LAYOUTS),
        "kinds": list(SERIES_KINDS),
        "palette": DEFAULT_PALETTE,
        "functions_enabled": services.functions.enabled,
    }


def _status_payload(services: Services) -> dict[str, Any]:
    stats = services.store.stats()
    node = services.subscriber.status() if services.subscriber else {"connected": False, "host": "-", "port": 0}
    return {
        "store": {
            **stats,
            "oldest_text": format_epoch(stats["oldest"]) if stats["oldest"] else "",
            "newest_text": format_epoch(stats["newest"]) if stats["newest"] else "",
            "span_text": format_span(stats["oldest"], stats["newest"]),
            "bytes_text": _human_bytes(float(stats["bytes"])),
            "max_bytes_text": _human_bytes(float(stats["max_bytes"])),
        },
        "node": node,
    }


def _human_bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"
