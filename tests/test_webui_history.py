from pathlib import Path


ROOT = Path(__file__).parent.parent


def test_history_and_report_views_are_wired():
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "webui" / "js" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "webui" / "css" / "app.css").read_text(encoding="utf-8")

    assert 'data-screen="history"' in html
    assert 'data-view="history"' in html
    assert 'data-view="report"' in html
    assert 'id="historyFeed"' in html
    assert 'id="historyBestRank"' in html
    assert 'id="reportRankLabel"' in html
    assert "get_history_summary" in js
    assert "get_match_history" in js
    assert "get_match_report" in js
    assert "rankLabel" in js
    assert "loadHistory(false)" in js
    assert "closest('.history-card')" in js
    assert "case 'postgame_ready'" in js
    assert "overflow-y:scroll" in css
    assert ".history-card" in css
    assert ".report-rankbox" in css
