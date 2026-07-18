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
    assert 'role="tablist" aria-label="History sections"' in html
    assert 'data-history-section="matches"' in html
    assert 'data-history-section="overview"' in html
    assert 'data-history-section="champions"' in html
    assert 'data-history-section="roles"' in html
    assert 'data-history-panel="matches"' in html
    assert 'data-history-panel="overview"' in html
    assert 'data-history-panel="champions"' in html
    assert 'data-history-panel="roles"' in html
    assert html.count('role="tabpanel" aria-labelledby="historyTab') == 4
    assert html.count('role="tabpanel" aria-labelledby="historyTabOverview" tabindex="0"') == 1
    assert html.count('role="tabpanel" aria-labelledby="historyTabChampions" tabindex="0"') == 1
    assert html.count('role="tabpanel" aria-labelledby="historyTabRoles" tabindex="0"') == 1
    assert 'id="reportRankLabel"' in html
    assert 'id="reportEvidenceSource"' in html
    assert 'id="reportConfidence"' in html
    assert 'id="reportInterval"' in html
    assert 'id="reportRankConfidence"' in html
    assert 'id="reportCoaching"' in html
    assert "get_history_summary" in js
    assert "get_match_history" in js
    assert "get_match_report" in js
    assert "rankLabel" in js
    assert "loadHistory(false)" in js
    assert "closest('.history-card')" in js
    assert "case 'postgame_ready'" in js
    assert "overflow-y:auto; padding:18px" in css
    assert ".history-card" in css
    assert ".report-rankbox" in css
    assert ".report-trust" in css
    assert ".report-coaching" in css


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
    assert "setHistorySection" in js
    assert "onHistoryTabKey" in js
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
    assert ".history-tab:focus-visible" in css
    assert ".history-tabs {" in css
    assert ".history-recent {" in css
    assert ".history-analytics-grid {" in css
    assert "font:14px 'Space Mono',monospace" in css
    assert ".history-analytics-grid small, .history-analytics-head small {\n  color:var(--pd); font-size:13px;" in css
    assert ".history-dashboard" not in css
    assert ".history-breakdown {" not in css
    assert ".history-breakdown > summary" not in css

    history_score = float(re.search(r"\.history-score strong \{[^}]*font-size:([0-9.]+)px", css).group(1))
    history_rank = float(re.search(r"\.history-rank strong \{[^}]*font-size:([0-9.]+)px", css).group(1))
    report_score = float(re.search(r"\.report-scorebox strong \{[^}]*font-size:([0-9.]+)px", css).group(1))
    report_rank = float(re.search(r"\.report-rankbox strong \{[^}]*font-size:([0-9.]+)px", css).group(1))
    assert history_rank > history_score
    assert report_rank > report_score
    assert ".history-score-track" not in css
    assert "row.cs" not in js.split("function renderHistory()", 1)[1].split("function loadHistory", 1)[0]


def test_standard_interface_mode_contract():
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "webui" / "js" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "webui" / "css" / "app.css").read_text(encoding="utf-8")

    assert '<html data-phosphor="amber" data-interface="standard">' in html
    assert 'id="setInterface" data-set="interface_style" role="button" tabindex="0"' in html
    assert "const INTERFACES = [['Standard', 'standard'], ['DAEMON Classic', 'classic']]" in js
    assert "function applyInterface(name)" in js
    assert "window.API.call('set_interface', v)" in js
    assert 'data-interface="standard"' in css
    assert 'data-interface="classic"' not in css
    assert 'font-family:"Segoe UI", Arial, sans-serif' in css
    assert ".bezel-foot { display:none; }" in css
    assert '.brand-name::after { content:"_"; color:var(--p); }' in css
    assert "overflow-y:auto; padding:20px 22px" in css
    assert 'role="switch" aria-checked="true"' in html
    assert 'id="setScoreV2Beta" data-set="score_v2_beta" role="switch"' in html
    assert "state.settings.score_v2_beta = !state.settings.score_v2_beta" in js
    assert "on by default; no local model installed, v1 active" in js
    assert 'class="trigger-options" role="radiogroup"' in html
    assert '<span class="standard-copy">Save changes</span>' in html
    assert ":root[data-interface=\"standard\"] .classic-copy" in css
    assert ":root[data-interface=\"standard\"] .settings-group" in css
    assert ":root[data-interface=\"standard\"] .standard-toggle" in css
    assert ":root[data-interface=\"standard\"] .trigger-options" in css
    assert "menuitemradio" in js
    assert "e.key === 'ArrowDown'" in js
    assert "e.key === 'Escape'" in js
    assert "SPELL_ICON_FILE" in js
    assert "assets/spells/" in js
    assert 'class="spell-icons"' in js
    assert "championIconUrl" in js
    assert "EVIDENCE_SOURCES" in js
    assert "function renderCoaching" in js
    assert "CLOSE RANKING" in js
    assert "Score v2 replaces legacy category bars" in js
    assert "COACHING WITHHELD" in js
    assert "3 OF 5 CHALLENGE" in js
    assert 'class="history-champion-art"' in js
    assert 'id="historyRecentForm"' in html
    assert "Last 10 shown" in html
    assert ":root[data-interface=\"standard\"] .history-form i.win" in css
    assert ":root[data-interface=\"standard\"] .history-feed {" in css
    assert ".history-match-meta i { margin:0 5px;" in css
    assert '<span class="spell-names">${esc(b.summoners)}</span>` +' in js
    assert '<span class="spell-default standard-copy">recommended</span>' in js
    for spell_icon in (
        "SummonerFlash.png", "SummonerTeleport.png", "SummonerDot.png",
        "SummonerSmite.png", "SummonerExhaust.png",
    ):
        assert (ROOT / "webui" / "assets" / "spells" / spell_icon).is_file()
