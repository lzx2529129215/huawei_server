from types import SimpleNamespace
import pytest
from memscenes.runner import _scene4_minimize_prepare


CASE = "s4_abcdef123456_c0000"
BUNDLE = "io.memscene.scene4.workload.a1"


def session_with_rows(rows):
    commands = []
    device = SimpleNamespace(
        launch=lambda bundle, params: commands.append((bundle, params)) or "ok",
        app=lambda bundle: {"uid": 20020188})
    return SimpleNamespace(device=device, journal=lambda bundle, case: rows), commands


def event(name, **extra):
    return dict(event=name, case_id=CASE + "_daily", pid=123, uid=20020188,
                start_ticks=456, monotonic_ns=1000000000, **extra)


def test_only_owned_window_minimize_no_redirty_command():
    session, commands = session_with_rows([
        event("background_prepare_requested"), event("background_prepare_succeeded")])
    result = _scene4_minimize_prepare(session, CASE, [BUNDLE], 1)
    assert result["complete"] and result["file_redirty_performed"] is False
    assert commands == [(BUNDLE, {"op": "minimize_only", "case_id": CASE})]
    assert result["bundles"][BUNDLE]["start_ticks"] == 456


@pytest.mark.parametrize("field,value", [("pid", 124), ("start_ticks", 457), ("uid", 20020189)])
def test_replaced_process_or_wrong_uid_never_claims_background_completion(field, value):
    last = event("background_prepare_succeeded")
    last[field] = value
    session, _ = session_with_rows([event("background_prepare_requested"), last])
    result = _scene4_minimize_prepare(session, CASE, [BUNDLE], 1)
    assert not result["complete"] and BUNDLE in result["errors"]


def test_real_application_cannot_be_minimized_by_test_preparation():
    session, commands = session_with_rows([])
    with pytest.raises(ValueError):
        _scene4_minimize_prepare(session, CASE, ["com.huawei.hmos.filemanager"], 1)
    assert commands == []
