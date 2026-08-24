"""Tests for user-authored transform functions."""

import numpy as np
import pytest

from subserver.userfuncs import FunctionError, FunctionRegistry, PlotData

MODULE = '''
import numpy as np

def double(data, factor=2):
    """Multiply everything."""
    return {name: data.v(name) * factor for name in data.names}

def with_times(data):
    return {"shifted": (data.t(data.names[0]) + 10, data.v(data.names[0]))}

def scalars(data):
    return {"mean": float(np.mean(data.v(data.names[0]))), "count": len(data.v(data.names[0]))}

def bare_scalar(data):
    return 42.0

def wrong_length(data):
    return {"bad": np.array([1.0, 2.0])}

def explodes(data):
    raise ValueError("nope")

def returns_none(data):
    return None

def _private(data):
    return 1
'''


@pytest.fixture
def registry(tmp_path):
    (tmp_path / "alice").mkdir()
    (tmp_path / "alice" / "mine.py").write_text(MODULE)
    return FunctionRegistry(tmp_path, enabled=True)


@pytest.fixture
def data():
    times = np.arange(100, dtype=np.float64)
    return PlotData({"a.b": (times, times * 2)}, 0.0, 99.0, 99.0)


def run(registry, data, name, **args):
    return registry.run({"scope": "user", "module": "mine", "name": name, "args": args}, data, "alice")


def test_dict_of_arrays_becomes_series(registry, data):
    out = run(registry, data, "double", factor=3)
    assert [s["label"] for s in out["series"]] == ["a.b"]
    assert out["series"][0]["v"].tolist() == (np.arange(100) * 6).tolist()


def test_explicit_times_are_used(registry, data):
    out = run(registry, data, "with_times")
    assert out["series"][0]["t"][0] == 10.0


def test_scalars_become_reference_lines(registry, data):
    out = run(registry, data, "scalars")
    assert {line["label"] for line in out["lines"]} == {"mean", "count"}
    assert out["series"] == []


def test_a_bare_scalar_is_a_reference_line(registry, data):
    out = run(registry, data, "bare_scalar")
    assert out["lines"] == [{"label": "bare_scalar", "value": 42.0}]


def test_length_mismatch_is_a_clear_error(registry, data):
    with pytest.raises(FunctionError, match="timestamps"):
        run(registry, data, "wrong_length")


def test_an_exception_inside_a_function_is_wrapped(registry, data):
    with pytest.raises(FunctionError, match="nope"):
        run(registry, data, "explodes")


def test_returning_none_is_an_error(registry, data):
    with pytest.raises(FunctionError, match="None"):
        run(registry, data, "returns_none")


def test_bad_arguments_are_reported(registry, data):
    with pytest.raises(FunctionError, match="rejected its arguments"):
        run(registry, data, "double", not_a_parameter=1)


def test_missing_function_and_module_are_reported(registry, data):
    with pytest.raises(FunctionError, match="no function called"):
        run(registry, data, "does_not_exist")
    with pytest.raises(FunctionError, match="No function module"):
        registry.run({"scope": "user", "module": "absent", "name": "f"}, data, "alice")


def test_private_functions_are_not_exposed(registry):
    names = {entry["name"] for entry in registry.catalog("alice")}
    assert "_private" not in names and "double" in names


def test_catalog_reports_signature_and_docstring(registry):
    entry = next(e for e in registry.catalog("alice") if e["name"] == "double")
    assert entry["doc"] == "Multiply everything."
    assert "factor" in entry["signature"]


def test_disabled_registry_refuses_to_load(tmp_path, data):
    (tmp_path / "alice").mkdir()
    (tmp_path / "alice" / "mine.py").write_text(MODULE)
    registry = FunctionRegistry(tmp_path, enabled=False)
    with pytest.raises(FunctionError, match="disabled"):
        registry.run({"scope": "user", "module": "mine", "name": "double"}, data, "alice")


def test_a_module_is_reloaded_after_it_is_edited(registry, data, tmp_path):
    assert run(registry, data, "double")["series"][0]["v"][1] == 4.0
    path = tmp_path / "alice" / "mine.py"
    path.write_text(MODULE.replace("data.v(name) * factor", "data.v(name) * 0"))
    import os, time
    os.utime(path, (time.time() + 1, time.time() + 1))   # ensure a new mtime
    assert run(registry, data, "double")["series"][0]["v"][1] == 0.0


def test_a_users_module_cannot_be_read_from_another_users_directory(registry, data):
    with pytest.raises(FunctionError, match="No function module"):
        registry.run({"scope": "user", "module": "mine", "name": "double"}, data, "bob")


@pytest.mark.parametrize("module", ["../evil", "a b", "os.path"])
def test_module_names_must_be_identifiers(registry, module):
    with pytest.raises(FunctionError):
        registry.module_path("user", "alice", module)


def test_a_broken_module_is_reported_in_the_catalog(tmp_path):
    (tmp_path / "alice").mkdir()
    (tmp_path / "alice" / "broken.py").write_text("this is not python <<<")
    registry = FunctionRegistry(tmp_path, enabled=True)
    entry = next(e for e in registry.catalog("alice") if e["module"] == "broken")
    assert entry["error"] and "failed to import" in entry["doc"]


def test_the_shipped_example_functions_all_run():
    registry = FunctionRegistry("Functions", enabled=True)
    times = np.arange(200, dtype=np.float64)
    data = PlotData({"a": (times, np.sin(times / 10)), "b": (times, np.cos(times / 10))}, 0, 199, 199)
    for name in ("rolling_mean", "difference", "summary", "rate_of_change", "normalise"):
        out = registry.run({"scope": "examples", "module": "statistics", "name": name}, data, "examples")
        assert out["series"] or out["lines"]


def test_a_matplotlib_figure_comes_back_as_a_png():
    pytest.importorskip("matplotlib")
    registry = FunctionRegistry("Functions", enabled=True)
    times = np.arange(200, dtype=np.float64)
    data = PlotData({"a": (times, np.sin(times / 10))}, 0, 199, 199)
    out = registry.run(
        {"scope": "examples", "module": "statistics", "name": "histogram", "args": {"bins": 10}},
        data, "examples",
    )
    import base64
    assert out["image"] and base64.b64decode(out["image"])[:4] == b"\x89PNG"
    assert out["series"] == [] and out["lines"] == []
