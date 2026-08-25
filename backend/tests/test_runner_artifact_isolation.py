"""Focused guards for isolated pre-fill artifacts."""
import inspect

from backend.applier import runner


def test_prefill_artifact_options_are_backward_compatible():
    params = inspect.signature(runner.prefill_application).parameters

    assert params["artifact_dir"].default is None
    assert params["copy_to_downloads"].default is True
    assert "submit" not in params


def test_default_artifact_directory_remains_main_prefill_tree(monkeypatch, tmp_path):
    legacy_root = tmp_path / "uploads" / "prefill"
    monkeypatch.setattr(runner, "OUT_ROOT", legacy_root)

    assert runner._artifact_directory("candidate", "company-role") == (
        legacy_root / "candidate" / "company-role"
    )


def test_explicit_artifact_directory_is_used_verbatim(tmp_path):
    isolated = tmp_path / "company-applier" / "run-42"

    assert runner._artifact_directory("candidate", "company-role", isolated) == isolated
    assert runner._artifact_directory("candidate", "company-role", str(isolated)) == isolated


def test_download_copy_can_be_bypassed(monkeypatch, tmp_path):
    """The new flag gates the only Downloads-copy call in prefill_application."""
    source = inspect.getsource(runner.prefill_application)

    assert "if copy_to_downloads:" in source
    assert source.count("_save_to_downloads(") == 1


def test_runner_has_no_submit_action():
    source = inspect.getsource(runner.prefill_application)

    # Human-review prose may mention Submit; the runner must not invoke a submit API,
    # click a submit selector, or expose a submit switch.
    compact = "".join(source.lower().split())
    assert ".submit(" not in compact
    assert "click(\"submit" not in compact
    assert "click('submit" not in compact
    assert "submit" not in inspect.signature(runner.prefill_application).parameters
