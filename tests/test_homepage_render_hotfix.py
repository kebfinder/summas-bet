from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


class _QueryStub:
    def __init__(self, count_value):
        self._count_value = count_value

    def filter(self, *args, **kwargs):
        return self

    def join(self, *args, **kwargs):
        return self

    def count(self):
        return self._count_value


class _DbStub:
    def query(self, model):
        name = getattr(model, "__name__", "")
        if name == "Fixture":
            return _QueryStub(4)
        if name == "Prediction":
            return _QueryStub(10)
        if name == "Team":
            return _QueryStub(20)
        raise AssertionError(f"Unexpected model queried: {model!r}")


def test_render_homepage_preserves_view_more_js_template(monkeypatch):
    monkeypatch.setattr(
        main,
        "_available_date_summaries",
        lambda db, predicted_only=True: [
            {"date": "2026-04-09", "fixtures": 8, "predictions": 8},
            {"date": "2026-04-08", "fixtures": 7, "predictions": 7},
            {"date": "2026-04-07", "fixtures": 6, "predictions": 6},
            {"date": "2026-04-06", "fixtures": 5, "predictions": 5},
        ],
    )

    html = main._render_homepage(_DbStub())

    assert "`View more (${sortedRows.length - 3} more)`" in html
    assert "Run Selected Date" in html
    assert "Show fewer dates" in html
    assert "if (isKnownDate(queryDate))" in html
    assert "if (isKnownDate(storedDate))" in html


def test_smoke_script_sets_repo_root_path():
    script_text = Path("scripts/smoke_public_web.py").read_text(encoding="utf-8")
    assert "Path(__file__).resolve().parents[1]" in script_text
    assert "sys.path.insert(0" in script_text
