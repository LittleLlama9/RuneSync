from pathlib import Path
import re


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
    assert 'class="history-overview"' in html
    assert '<details class="history-breakdown">' in html
    assert '<details class="history-breakdown" open>' not in html
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


def test_history_report_readability_and_keyboard_contract():
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "webui" / "js" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "webui" / "css" / "app.css").read_text(encoding="utf-8")

    assert 'role="navigation" aria-label="Primary navigation"' in html
    assert 'id="historyRefresh" role="button" tabindex="0"' in html
    assert 'id="historyState" role="status" aria-live="polite"' in html
    assert 'id="historyError" role="alert" aria-live="assertive"' in html
    assert 'id="historyFeed" aria-label="Match history" aria-busy="false"' in html
    assert 'id="historyMore" class="history-more" role="button"' in html
    assert 'id="reportBack" role="button" tabindex="0"' in html

    assert 'role="button" tabindex="0" aria-label="${esc(cardLabel)}"' in js
    assert "activateButtonOnKey" in js
    assert "$('historyRows').addEventListener('keydown'" in js
    assert "setAttribute('aria-current', 'page')" in js
    assert "setAttribute('aria-busy'" in js
    assert "setAttribute('aria-disabled'" in js

    history_report_css = css.split("/* ── history / post-game report ── */", 1)[1]
    history_report_css = history_report_css.split("/* ── override editor ── */", 1)[0]
    sizes = [float(value) for value in re.findall(r"font-size:([0-9.]+)px", history_report_css)]
    assert sizes
    assert min(sizes) >= 10
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "[role=\"button\"]:focus-visible" in css
    assert "summary:focus-visible" in css
    assert ".history-overview {" in css
    assert ".history-breakdown > summary {" in css
    assert ".history-dashboard" not in css

    history_score = float(re.search(r"\.history-score strong \{[^}]*font-size:([0-9.]+)px", css).group(1))
    history_rank = float(re.search(r"\.history-rank strong \{[^}]*font-size:([0-9.]+)px", css).group(1))
    report_score = float(re.search(r"\.report-scorebox strong \{[^}]*font-size:([0-9.]+)px", css).group(1))
    report_rank = float(re.search(r"\.report-rankbox strong \{[^}]*font-size:([0-9.]+)px", css).group(1))
    assert history_rank > history_score
    assert report_rank > report_score
    assert ".history-score-track { height:5px;" in css
    assert "Math.max(0, Math.min(100, score))" in js
