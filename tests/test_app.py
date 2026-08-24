"""End-to-end tests of the web layer, driven through Flask's test client."""

import time

import pytest

from subserver.app import Services, create_app
from subserver.auth import UserStore
from subserver.config import Config
from subserver.profiles import ProfileStore
from subserver.query import QueryEngine
from subserver.ringstore import RamStore
from subserver.userfuncs import FunctionRegistry

PASSWORD = "correct horse battery"


@pytest.fixture
def services(tmp_path):
    config = Config({"security": {"allow_registration": True}}, root=tmp_path)
    config.data_dir.mkdir(parents=True, exist_ok=True)
    store = RamStore(max_bytes=5_000_000)
    now = time.time()
    for i in range(600):
        store.add_sample("a.b", now - 600 + i, float(i))
        store.add_sample("c.d", now - 600 + i, float(i) * 2)
    functions = FunctionRegistry(tmp_path / "Functions", enabled=True)
    return Services(
        config, store, None,
        UserStore(config.data_dir / "users.json"),
        ProfileStore(tmp_path / "Profiles"),
        functions,
        QueryEngine(store, None, functions, max_points=500),
    )


@pytest.fixture
def app(services):
    application = create_app(services)
    application.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    return application


class Client:
    """Test client that carries the CSRF token the way the browser does."""

    def __init__(self, app):
        self.raw = app.test_client()
        self.token = ""

    def _refresh_token(self, path="/register"):
        body = self.raw.get(path).get_data(as_text=True)
        marker = 'name="csrf_token" value="'
        if marker in body:
            self.token = body.split(marker)[1].split('"')[0]

    def register(self, username, password=PASSWORD):
        self._refresh_token()
        response = self.raw.post("/register", data={
            "csrf_token": self.token, "username": username,
            "password": password, "confirm": password,
        }, follow_redirects=False)
        body = self.raw.get("/").get_data(as_text=True)
        if 'data-csrf="' in body:
            self.token = body.split('data-csrf="')[1].split('"')[0]
        return response

    def login(self, username, password=PASSWORD):
        self._refresh_token("/login")
        response = self.raw.post("/login", data={
            "csrf_token": self.token, "username": username, "password": password,
        })
        body = self.raw.get("/").get_data(as_text=True)
        if 'data-csrf="' in body:
            self.token = body.split('data-csrf="')[1].split('"')[0]
        return response

    def api(self, path, payload=None, method="post"):
        fn = getattr(self.raw, method)
        kwargs = {"headers": {"X-CSRF-Token": self.token}}
        if payload is not None:
            kwargs["json"] = payload
        return fn(path, **kwargs)

    def get(self, path):
        return self.raw.get(path)


@pytest.fixture
def client(app):
    return Client(app)


@pytest.fixture
def alice(client):
    client.register("alice")
    return client


def profile_doc(name="demo", mode="live"):
    plot = {
        "title": "Core", "y_label": "K", "mode": mode, "kind": "line",
        "start": "-5m", "series": [{"variable": "a.b", "color": "#4fc3f7"}],
    }
    if mode == "static":
        plot["start"] = "-8m"
        plot["end"] = "-4m"
    return {"name": name, "layout": "1x1", "plots": [plot]}


# -- access control -----------------------------------------------------------
@pytest.mark.parametrize("path", ["/", "/about", "/profiles/new", "/profiles/load", "/profiles/browse"])
def test_pages_require_a_session(client, path):
    response = client.get(path)
    assert response.status_code == 302 and "/login" in response.headers["Location"]


def test_api_returns_401_rather_than_a_redirect(client):
    assert client.api("/api/status", method="get").status_code == 401


def test_login_page_offers_account_creation(client):
    assert "Create a new user" in client.get("/login").get_data(as_text=True)


def test_registration_signs_the_new_user_in(client):
    client.register("alice")
    assert "Welcome back" in client.get("/").get_data(as_text=True)


def test_registration_rejects_mismatched_passwords(client):
    client._refresh_token()
    response = client.raw.post("/register", data={
        "csrf_token": client.token, "username": "alice",
        "password": PASSWORD, "confirm": "something else",
    })
    assert response.status_code == 400
    assert "do not match" in response.get_data(as_text=True)


def test_bad_login_is_rejected(client, alice):
    client.raw.post("/logout", data={"csrf_token": client.token})
    response = client.login("alice", "wrong password")
    assert response.status_code == 401


def test_logout_ends_the_session(alice):
    alice.raw.post("/logout", data={"csrf_token": alice.token})
    assert alice.get("/").status_code == 302


def test_post_without_a_csrf_token_is_refused(alice):
    response = alice.raw.post("/api/profile/save", json={"profile": profile_doc()})
    assert response.status_code == 403


def test_login_redirect_only_follows_relative_targets(client, alice):
    alice.raw.post("/logout", data={"csrf_token": alice.token})
    client._refresh_token("/login")
    response = client.raw.post(
        "/login?next=https://evil.example/x",
        data={"csrf_token": client.token, "username": "alice", "password": PASSWORD},
    )
    assert "evil.example" not in response.headers.get("Location", "")


def test_security_headers_are_set(client):
    headers = client.get("/login").headers
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"


# -- pages --------------------------------------------------------------------
def test_home_greets_the_user_by_name(alice):
    assert "Welcome back alice!" in alice.get("/").get_data(as_text=True)


def test_toolbar_is_present_on_every_page(alice):
    for path in ("/", "/about", "/profiles/new", "/profiles/load", "/profiles/browse"):
        body = alice.get(path).get_data(as_text=True)
        for label in ("Home", "Profiles", "About", "Create New", "Load", "Browse Shared"):
            assert label in body, f"{label!r} missing from {path}"


def test_about_menu_shows_the_name_and_version(alice):
    from subserver import PROGRAM_NAME, __version__
    body = alice.get("/").get_data(as_text=True)
    assert PROGRAM_NAME in body and f"Version {__version__}" in body


def test_unknown_page_is_a_404(alice):
    assert alice.get("/nope").status_code == 404


# -- profiles -----------------------------------------------------------------
def test_save_then_load_a_profile(alice):
    saved = alice.api("/api/profile/save",
                      {"profile": profile_doc(), "name": "demo", "folder": "work"}).get_json()
    assert saved["ok"] and saved["ref"]["folder"] == "work"
    listed = alice.api("/api/profiles", method="get").get_json()["profiles"]["private"]
    assert [entry["name"] for entry in listed] == ["demo"]
    page = alice.get("/profiles/view?scope=private&owner=alice&folder=work&name=demo")
    assert page.status_code == 200 and "demo" in page.get_data(as_text=True)


def test_publishing_the_same_name_adds_a_suffix(alice):
    names = [
        alice.api("/api/profile/publish", {"profile": profile_doc(), "name": "bruh"}).get_json()["ref"]["name"]
        for _ in range(3)
    ]
    assert names == ["bruh", "bruh_1", "bruh_2"]


def test_publish_reports_the_rename_to_the_user(alice):
    alice.api("/api/profile/publish", {"profile": profile_doc(), "name": "bruh"})
    result = alice.api("/api/profile/publish", {"profile": profile_doc(), "name": "bruh"}).get_json()
    assert result["renamed"] is True and "bruh_1" in result["message"]


def test_one_user_cannot_read_another_s_private_profile(app, alice):
    alice.api("/api/profile/save", {"profile": profile_doc(), "name": "secret"})
    bob = Client(app)
    bob.register("bob")
    result = bob.api("/api/data", {"ref": {"scope": "private", "owner": "alice", "folder": "", "name": "secret"}})
    assert result.status_code == 400 and "not have access" in result.get_json()["error"]


def test_shared_profiles_are_readable_by_other_users(app, alice):
    alice.api("/api/profile/publish", {"profile": profile_doc(), "name": "public"})
    bob = Client(app)
    bob.register("bob")
    result = bob.api("/api/data", {"ref": {"scope": "shared", "owner": "alice", "folder": "", "name": "public"}})
    assert result.get_json()["ok"] is True


def test_shared_profiles_cannot_be_deleted(alice):
    alice.api("/api/profile/publish", {"profile": profile_doc(), "name": "public"})
    result = alice.api("/api/profile/delete",
                       {"scope": "shared", "owner": "alice", "folder": "", "name": "public"})
    assert result.status_code == 400


def test_deleting_a_private_profile(alice):
    alice.api("/api/profile/save", {"profile": profile_doc(), "name": "temp"})
    assert alice.api("/api/profile/delete",
                     {"scope": "private", "owner": "alice", "folder": "", "name": "temp"}).get_json()["ok"]
    assert alice.api("/api/profiles", method="get").get_json()["profiles"]["private"] == []


@pytest.mark.parametrize("folder,name", [
    ("../../etc", "x"), ("..", "x"), ("", "../../evil"), ("a/../../b", "x"),
])
def test_traversal_attempts_are_refused(alice, folder, name):
    result = alice.api("/api/profile/save", {"profile": profile_doc(), "name": name, "folder": folder})
    assert result.status_code == 400


def test_invalid_profile_is_refused_with_a_readable_message(alice):
    result = alice.api("/api/profile/save", {"profile": {"name": "x", "plots": []}, "name": "x"})
    assert result.status_code == 400 and "at least one plot" in result.get_json()["error"]


def test_folders_can_be_created_and_removed(alice):
    assert alice.api("/api/folders", {"scope": "private", "folder": "a/b"}).get_json()["folder"] == "a/b"
    assert "a/b" in alice.api("/api/folders?scope=private", method="get").get_json()["folders"]
    assert alice.api("/api/folders", {"scope": "private", "folder": "a/b"}, method="delete").get_json()["ok"]


def test_folders_cannot_be_created_in_the_examples_area(alice):
    assert alice.api("/api/folders", {"scope": "examples", "folder": "x"}).status_code == 400


# -- data ---------------------------------------------------------------------
def test_data_for_an_unsaved_draft(alice):
    result = alice.api("/api/data", {"profile": profile_doc(), "cursors": {}}).get_json()
    assert result["ok"] and len(result["plots"]) == 1
    plot = result["plots"][0]
    assert plot["series"][0]["x"] and plot["incremental"] is False
    assert plot["cursor"] is not None


def test_live_updates_are_incremental(alice, services):
    first = alice.api("/api/data", {"profile": profile_doc(), "cursors": {}}).get_json()["plots"][0]
    cursor = first["cursor"]
    services.store.add_sample("a.b", cursor + 1, 999.0)
    second = alice.api("/api/data", {"profile": profile_doc(), "cursors": {"0": cursor}}).get_json()["plots"][0]
    assert second["incremental"] is True
    assert second["series"][0]["y"] == [999.0]


def test_static_plots_are_never_incremental(alice):
    doc = profile_doc(mode="static")
    first = alice.api("/api/data", {"profile": doc, "cursors": {}}).get_json()["plots"][0]
    second = alice.api("/api/data", {"profile": doc, "cursors": {"0": first["cursor"]}}).get_json()["plots"][0]
    assert second["incremental"] is False
    assert second["series"][0]["x"] == first["series"][0]["x"]


def test_series_are_decimated_to_the_configured_limit(alice, services):
    for i in range(20_000):
        services.store.add_sample("big.var", time.time() - 20_000 + i, float(i))
    doc = profile_doc()
    doc["plots"][0]["start"] = "-6h"
    doc["plots"][0]["series"] = [{"variable": "big.var"}]
    plot = alice.api("/api/data", {"profile": doc, "cursors": {}}).get_json()["plots"][0]
    assert 0 < len(plot["series"][0]["x"]) <= 500


def test_the_plots_filter_restricts_the_work(alice):
    doc = profile_doc()
    doc["plots"].append(dict(doc["plots"][0], title="Second"))
    doc["layout"] = "2x1"
    result = alice.api("/api/data", {"profile": doc, "cursors": {}, "plots": [1]}).get_json()
    assert [plot["index"] for plot in result["plots"]] == [1]


def test_a_missing_variable_yields_an_empty_series_not_an_error(alice):
    doc = profile_doc()
    doc["plots"][0]["series"] = [{"variable": "does.not.exist"}]
    plot = alice.api("/api/data", {"profile": doc, "cursors": {}}).get_json()["plots"][0]
    assert plot["series"][0]["x"] == [] and plot["error"] == ""


def test_variables_endpoint_lists_what_is_in_memory(alice):
    assert alice.api("/api/variables", method="get").get_json()["variables"] == ["a.b", "c.d"]


def test_status_endpoint_reports_the_store(alice):
    status = alice.api("/api/status", method="get").get_json()
    assert status["store"]["variables"] == 2
    assert status["node"]["connected"] is False


def test_oversized_request_is_refused(alice):
    huge = {"profile": profile_doc(), "name": "x" * 3_000_000}
    assert alice.api("/api/profile/save", huge).status_code == 413


def test_preferences_round_trip(alice):
    result = alice.api("/api/preferences", {"palette": ["#abc", "not a colour"]}).get_json()
    assert result["preferences"]["palette"][0] == "#aabbcc"
