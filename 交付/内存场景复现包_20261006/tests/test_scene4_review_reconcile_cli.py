import importlib.util
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))
SCRIPT = PACKAGE_ROOT / "tools" / "reconcile_scene4_frame_reviews.py"
spec = importlib.util.spec_from_file_location("scene4_review_reconcile_cli", SCRIPT)
cli = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(cli)


def test_cli_delegates_original_result_to_canonical_reconciler(tmp_path, monkeypatch, capsys):
    import memscenes.runner

    source = tmp_path / "scene4_result.json"
    source.write_text("{}", encoding="utf-8")
    expected = tmp_path / "scene4_result_reviewed.json"
    calls = []

    def reconcile(path):
        calls.append(Path(path).resolve())
        return expected

    monkeypatch.setattr(memscenes.runner, "reconcile_scene4_frame_reviews", reconcile)
    assert cli.main([str(source)]) == 0
    assert calls == [source.resolve()]
    assert capsys.readouterr().out.strip() == str(expected)
    assert source.read_text(encoding="utf-8") == "{}"


def test_cli_reports_reconciliation_failure_without_output_file(tmp_path, monkeypatch, capsys):
    import memscenes.runner

    def fail(_path):
        raise ValueError("missing frame review")

    monkeypatch.setattr(memscenes.runner, "reconcile_scene4_frame_reviews", fail)
    assert cli.main([str(tmp_path / "absent.json")]) == 2
    assert "missing frame review" in capsys.readouterr().err
