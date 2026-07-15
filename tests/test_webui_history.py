from pathlib import Path


ROOT = Path(__file__).parent.parent


def test_history_and_report_views_are_wired():
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "webui" / "js" / "app.js").read_text(encoding="utf-8")

    assert 'data-screen="history"' in html
    assert 'data-view="history"' in html
    assert 'data-view="report"' in html
    assert "get_history_summary" in js
    assert "get_match_history" in js
    assert "get_match_report" in js
    assert "case 'postgame_ready'" in js
