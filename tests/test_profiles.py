"""Tests for profile storage, the publish rule, and path safety."""

import pytest

from subserver.models import ValidationError, validate_profile
from subserver.paths import UnsafePath, check_name, check_username, split_folder
from subserver.profiles import ProfileError, ProfileRef, ProfileStore


def make_profile(name="demo", mode="live"):
    plot = {
        "title": "T",
        "y_label": "K",
        "mode": mode,
        "start": "-1h" if mode == "live" else "2026-01-01T00:00:00",
        "series": [{"variable": "a.b"}],
    }
    if mode == "static":
        plot["end"] = "2026-01-01T01:00:00"
    return validate_profile({"name": name, "plots": [plot]}, owner="alice")


@pytest.fixture
def store(tmp_path):
    profiles = ProfileStore(tmp_path)
    profiles.ensure_user_space("alice")
    profiles.ensure_user_space("bob")
    return profiles


def test_save_and_load_round_trip(store):
    ref = store.save_private("alice", "work/aug", "demo", make_profile())
    assert ref.display_path == "private/alice/work/aug/demo"
    loaded = store.load(ref, "alice")
    assert loaded["name"] == "demo"
    assert loaded["plots"][0]["series"][0]["variable"] == "a.b"


def test_publish_never_overwrites_and_suffixes_the_name(store):
    names = [store.publish("alice", "", "bruh", make_profile()).name for _ in range(4)]
    assert names == ["bruh", "bruh_1", "bruh_2", "bruh_3"]
    assert len(store.list_profiles("shared", "alice")) == 4


def test_publish_is_namespaced_per_user(store):
    a = store.publish("alice", "", "bruh", make_profile())
    b = store.publish("bob", "", "bruh", make_profile())
    # Two users publishing the same name do not collide: neither is renamed.
    assert (a.name, a.owner) == ("bruh", "alice")
    assert (b.name, b.owner) == ("bruh", "bob")


def test_shared_profiles_cannot_be_deleted_or_written(store):
    ref = store.publish("alice", "", "bruh", make_profile())
    assert store.can_write(ref, "alice") is False
    with pytest.raises(ProfileError):
        store.delete(ref, "alice")


def test_private_profiles_are_not_readable_by_others(store):
    ref = store.save_private("alice", "", "secret", make_profile())
    assert store.can_read(ref, "alice") is True
    assert store.can_read(ref, "bob") is False
    with pytest.raises(ProfileError):
        store.load(ref, "bob")


def test_shared_and_example_profiles_are_readable_by_everyone(store):
    ref = store.publish("alice", "", "public", make_profile())
    assert store.load(ref, "bob")["name"] == "public"


def test_delete_removes_only_the_owner_s_private_profile(store):
    ref = store.save_private("alice", "", "gone", make_profile())
    with pytest.raises(ProfileError):
        store.delete(ref, "bob")
    store.delete(ref, "alice")
    with pytest.raises(ProfileError):
        store.load(ref, "alice")


def test_folders_can_be_created_and_listed(store):
    store.create_folder("private", "alice", "experiments/august")
    assert "experiments/august" in store.list_folders("private", "alice")
    store.delete_folder("private", "alice", "experiments/august")
    assert "experiments/august" not in store.list_folders("private", "alice")


def test_non_empty_folder_is_not_deleted(store):
    store.save_private("alice", "keep", "demo", make_profile())
    with pytest.raises(ProfileError):
        store.delete_folder("private", "alice", "keep")


@pytest.mark.parametrize("folder", ["..", "../etc", "a/../../b", "a/./../../b", "..%2Fetc"])
def test_folder_traversal_is_rejected(store, folder):
    with pytest.raises((UnsafePath, ProfileError)):
        store.file_path(ProfileRef("private", "alice", folder, "x"))


@pytest.mark.parametrize("folder", ["/etc", "//etc/passwd", "etc/"])
def test_absolute_looking_folders_stay_inside_the_user_root(store, folder):
    """A leading slash is stripped, not honoured: the path stays contained."""
    path = store.file_path(ProfileRef("private", "alice", folder, "x"))
    assert store.owner_root("private", "alice") in path.parents


def test_symlink_out_of_the_tree_is_refused(store, tmp_path):
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    link = store.owner_root("private", "alice") / "escape"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(UnsafePath):
        store.file_path(ProfileRef("private", "alice", "escape", "x"))


@pytest.mark.parametrize("name", ["..", "../evil", "a/b", "", ".hidden", "x" * 80])
def test_profile_name_traversal_is_rejected(name):
    with pytest.raises(UnsafePath):
        check_name(name)


@pytest.mark.parametrize("username", ["ab", "..", "Alice/../root", "a b", "-lead"])
def test_bad_usernames_are_rejected(username):
    with pytest.raises(UnsafePath):
        check_username(username)


def test_username_is_normalised_to_lowercase():
    assert check_username("Alice_1") == "alice_1"


def test_split_folder_rejects_dot_dot():
    assert split_folder("a/b/c") == ["a", "b", "c"]
    with pytest.raises(UnsafePath):
        split_folder("a/../b")


def test_corrupt_profile_file_is_reported_not_raised(store, tmp_path):
    ref = store.save_private("alice", "", "broken", make_profile())
    store.file_path(ref).write_text("this is not = valid toml [[[", encoding="utf-8")
    entry = next(e for e in store.list_profiles("private", "alice") if e["name"] == "broken")
    assert entry["error"]
    with pytest.raises(ProfileError):
        store.load(ref, "alice")


def test_profile_rejects_more_than_four_plots():
    plot = {"mode": "live", "start": "-1h", "series": [{"variable": "a"}]}
    with pytest.raises(ValidationError):
        validate_profile({"name": "x", "plots": [plot] * 5}, owner="alice")
