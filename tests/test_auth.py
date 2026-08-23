"""Tests for account storage and password handling."""

import json
import os
import stat

import pytest

from subserver.auth import (
    AuthError, MIN_PASSWORD_LENGTH, UserStore, hash_password, load_or_create_secret, verify_password,
)

GOOD_PASSWORD = "correct horse battery"


@pytest.fixture
def users(tmp_path):
    return UserStore(tmp_path / "users.json")


def test_create_and_authenticate(users):
    users.create("alice", GOOD_PASSWORD, "Alice A")
    record = users.authenticate("alice", GOOD_PASSWORD)
    assert record["display_name"] == "Alice A"
    assert record["last_login"] is not None


def test_usernames_are_case_insensitive(users):
    users.create("Alice", GOOD_PASSWORD)
    assert users.authenticate("ALICE", GOOD_PASSWORD)["username"] == "alice"


def test_wrong_password_is_refused(users):
    users.create("alice", GOOD_PASSWORD)
    with pytest.raises(AuthError):
        users.authenticate("alice", "wrong password")


def test_unknown_user_gives_the_same_message_as_a_wrong_password(users):
    users.create("alice", GOOD_PASSWORD)
    with pytest.raises(AuthError) as unknown:
        users.authenticate("nobody", GOOD_PASSWORD)
    with pytest.raises(AuthError) as wrong:
        users.authenticate("alice", "nope1234")
    assert str(unknown.value) == str(wrong.value)


def test_duplicate_username_is_refused(users):
    users.create("alice", GOOD_PASSWORD)
    with pytest.raises(AuthError):
        users.create("alice", GOOD_PASSWORD)


def test_short_password_is_refused(users):
    with pytest.raises(AuthError):
        users.create("alice", "x" * (MIN_PASSWORD_LENGTH - 1))


def test_password_is_not_stored_in_the_clear(users, tmp_path):
    users.create("alice", GOOD_PASSWORD)
    body = (tmp_path / "users.json").read_text()
    assert GOOD_PASSWORD not in body
    record = json.loads(body)["users"]["alice"]["password"]
    assert record["algorithm"] == "pbkdf2_sha256" and record["iterations"] >= 100_000


def test_same_password_gets_a_different_salt():
    first, second = hash_password(GOOD_PASSWORD), hash_password(GOOD_PASSWORD)
    assert first["salt"] != second["salt"] and first["hash"] != second["hash"]
    assert verify_password(GOOD_PASSWORD, first) and verify_password(GOOD_PASSWORD, second)


def test_verify_password_tolerates_a_malformed_record():
    assert verify_password("x", {}) is False
    assert verify_password("x", {"salt": "zz", "hash": "zz", "iterations": "n"}) is False


def test_credentials_file_is_not_world_readable(users, tmp_path):
    users.create("alice", GOOD_PASSWORD)
    mode = stat.S_IMODE(os.stat(tmp_path / "users.json").st_mode)
    assert mode & 0o077 == 0


def test_change_password(users):
    users.create("alice", GOOD_PASSWORD)
    users.change_password("alice", GOOD_PASSWORD, "a new long password")
    with pytest.raises(AuthError):
        users.authenticate("alice", GOOD_PASSWORD)
    assert users.authenticate("alice", "a new long password")


def test_change_password_needs_the_old_one(users):
    users.create("alice", GOOD_PASSWORD)
    with pytest.raises(AuthError):
        users.change_password("alice", "not it", "a new long password")


def test_preferences_round_trip(users):
    users.create("alice", GOOD_PASSWORD)
    users.set_preferences("alice", {"palette": ["#111111"]})
    assert users.preferences("alice")["palette"] == ["#111111"]


def test_accounts_survive_a_reload(users, tmp_path):
    users.create("alice", GOOD_PASSWORD)
    assert UserStore(tmp_path / "users.json").authenticate("alice", GOOD_PASSWORD)


def test_corrupt_user_file_is_set_aside_rather_than_crashing(tmp_path):
    path = tmp_path / "users.json"
    path.write_text("{ not json")
    store = UserStore(path)
    assert store.count() == 0
    assert list(tmp_path.glob("users.json.corrupt-*"))


def test_session_secret_is_created_once_and_reused(tmp_path):
    path = tmp_path / "session.key"
    first = load_or_create_secret(path)
    assert len(first) >= 32
    assert load_or_create_secret(path) == first
    assert stat.S_IMODE(os.stat(path).st_mode) & 0o077 == 0
