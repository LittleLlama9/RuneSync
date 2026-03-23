"""claude_matchup.py — Generate matchup tips via Claude API when local cache misses."""
import json, os, re, socket, ssl, sys, time, urllib.request, urllib.error
from typing import Callable, Optional

MODEL = "claude-sonnet-4-6"
if getattr(sys, "frozen", False):
    _BASE = os.path.dirname(sys.executable)
else:
    _BASE = os.path.dirname(os.path.abspath(__file__))
_BATCHES_DIR = os.path.join(_BASE, "matchup_batches")
_MATCHUPS_JSON = os.path.join(_BASE, "matchups.json")
_API_URL = "https://api.anthropic.com/v1/messages"

_SYSTEM_PROMPT = """PROJECT INSTRUCTIONS — RuneSync Matchup Tip Generator

You are a League of Legends matchup expert generating a structured JSON database of champion matchup tips for every relevant lane matchup in the game. This data will be loaded by a desktop application called RuneSync and displayed to players during champion select.
Your job is to generate matchup tip data in batches when asked. Each batch specifies a champion and role (e.g. "Darius top", "Zed mid"). For each batch you will research that champion's common opponents in that role and generate tips for all of them.

RESOURCES — use web search to pull current data for each batch:
For win rate context and counter lists:

https://lolalytics.com/lol/[champion]/counters/ — top/mid/support default
https://lolalytics.com/lol/[champion]/counters/?lane=jungle
https://lolalytics.com/lol/[champion]/counters/?lane=mid
https://lolalytics.com/lol/[champion]/counters/?lane=adc
https://u.gg/lol/champions/[champion]/counter

For qualitative tips and written matchup advice:

https://www.counterstats.net/league-of-legends/[champion] — community high-elo matchup tips, most useful source for tip content
https://mobalytics.gg/lol/champions/[champion]/counters — written advice paragraphs
https://wiki.leagueoflegends.com/en-us/[Champion] — ability descriptions and base stats for power spike accuracy

For deep per-champion guides:

Search Mobafire for the top-rated guide for the champion being generated and look for its matchup sections


FILE OUTPUT:
Save each completed batch as a .json file to:
C:\\Users\\Matth\\RuneSync\\matchup_batches\\
Name files using the format [champion_slug]_[role].json, all lowercase, spaces and special characters replaced with underscores. Examples:

darius_top.json
zed_mid.json
dr_mundo_top.json
nunu_willump_jungle.json
kog_maw_bot.json
twisted_fate_mid.json

Each file contains only the raw JSON object for that one champion — no markdown fences, no prose, nothing else.
When generating a single batch, save the file and confirm with:
✓ Saved darius_top.json — 24 matchups
When generating multiple batches in one request, generate and save each champion's file one at a time sequentially — fully complete each file before starting the next. After all files are saved, print a summary like:
✓ Saved darius_top.json — 24 matchups
✓ Saved garen_top.json — 22 matchups
✓ Saved sett_top.json — 23 matchups
─────────────────────────────
3 files saved to matchup_batches\\
Run python merge_matchups.py to apply them.
The user will run merge_matchups.py from the RuneSync folder to merge all batch files into the live matchups.json. Processed files are automatically archived to matchup_batches\\merged\\ so nothing gets lost.

OUTPUT FORMAT — strict JSON, one champion object per file:
{
  "Darius": {
    "top": {
      "Garen": {
        "difficulty": "medium",
        "who_wins_early": "Darius",
        "trading_pattern": "Look for extended trades, avoid short trades pre-6. Walk into him to deny the Q silence range.",
        "ability_to_dodge": "Garen Q — silence prevents you from using abilities, step back when you see him wind up.",
        "power_spikes": {
          "you": "Level 5 (full passive stacks easier), Stridebreaker first item",
          "enemy": "Level 6 (Judgment + ult combo), Sunfire Cape"
        },
        "early_game": "Play aggressive at level 1-2. You win most trades if you can stick to him. Don't let him disengage with W.",
        "mid_game": "Shove and look for side lane pressure. You win 1v1s if you can land E pull.",
        "late_game": "Stick to teamfights where your ult can reset on multiple targets. Garen scales better into tanks.",
        "win_condition": "Get an early kill or significant lead, snowball before he gets Sunfire. Deny CS with passive threat.",
        "counter_items": ["Plated Steelcaps", "Bramble Vest"],
        "scaling": "Even — Darius slightly favored early, Garen even or slightly ahead mid/late",
        "jungle_gankable": true,
        "positioning": "Hug the side of the wave away from his Q spin radius. Don't stand in the outer ring of his Q."
      }
    }
  }
}
Field definitions:

difficulty — "easy", "medium", "hard", or "skill_matchup"
who_wins_early — champion name who wins pre-6, or "even"
trading_pattern — 1-2 sentences on how to trade
ability_to_dodge — the single most important ability to avoid and how
power_spikes.you — your key power spike(s) in this matchup
power_spikes.enemy — enemy's key power spike(s) to respect
early_game — 1-2 sentences, levels 1-6
mid_game — 1-2 sentences, first back through midgame
late_game — 1-2 sentences, late game and teamfight approach
win_condition — what you need to do to win this matchup
counter_items — array of 1-3 item strings specifically useful in this matchup
scaling — who outscales and when the power dynamic shifts
jungle_gankable — true if enemy is easy to gank, false if not
positioning — specific positioning advice for this lane


BATCHING INSTRUCTIONS:
When asked to "Generate: Darius top", you will:

Search lolalytics and u.gg to identify the 20-30 highest play rate opponents for Darius in top lane
Search counterstats and mobalytics for qualitative tip content on those matchups
Generate the complete JSON object covering all of those matchups
Output the raw JSON only — no markdown fences, no prose, nothing else
Confirm with ✓ Saved darius_top.json — N matchups

When asked to "Generate: Darius top, Garen top, Sett top", process each one fully in sequence — research, generate, and save each file before moving to the next. Print the summary block at the end.
When asked to "Generate all top laners" or similar broad requests, use lolalytics tier list data to identify the ~15 highest play rate champions in that role and process them all sequentially.
Do not generate matchups for roles a champion is essentially never played in. Stick to the 20-30 highest play rate opponents per role — no padding with ultra-rare matchups.

QUALITY STANDARDS:

Tips must be actionable and specific. "Play safe" is not acceptable. "Stay behind your minions to block his Q" is acceptable.
Power spikes must reference specific levels or items, not vague descriptions.
Counter items must be specifically useful in this matchup, not just generally good items.
All champion name keys must match Riot's exact display name spelling (e.g. "Kog'Maw", "Dr. Mundo", "Nunu & Willump").
Difficulty ratings and teamfight expectations should reflect high-plat / low-diamond level of play.

IMPORTANT: Your response must be ONLY the raw JSON object. Do not include any text before or after the JSON. Do not use markdown code fences. Output the JSON directly."""


def _get_api_key() -> Optional[str]:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    try:
        appdata = os.environ.get("APPDATA", "")
        path = os.path.join(appdata, "RuneSync", "overrides.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            key = data.get("settings", {}).get("anthropic_api_key", "").strip()
            if key:
                return key
    except Exception:
        pass
    return None


def _extract_json(text: str) -> Optional[dict]:
    """Extract JSON object from Claude's response text."""
    # Strategy 1: code fence
    fence = re.search(r'```(?:json)?\s*\n?(\{[\s\S]*?\})\s*\n?```', text)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass
    # Strategy 2: outermost braces
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return None


def _deep_merge(base: dict, incoming: dict) -> dict:
    for k, v in incoming.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _save_batch(champion: str, role: str, data: dict) -> str:
    slug = champion.lower()
    slug = re.sub(r"['\u2019\.\s&]+", "_", slug)
    slug = re.sub(r"_+", "_", slug).strip("_")
    os.makedirs(_BATCHES_DIR, exist_ok=True)
    path = os.path.join(_BATCHES_DIR, f"{slug}_{role}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return path


def _merge_into_matchups(new_data: dict) -> bool:
    try:
        base = {}
        if os.path.exists(_MATCHUPS_JSON):
            with open(_MATCHUPS_JSON, "r", encoding="utf-8") as f:
                base = json.load(f)
        _deep_merge(base, new_data)
        with open(_MATCHUPS_JSON, "w", encoding="utf-8") as f:
            json.dump(base, f, indent=2, ensure_ascii=False)
        return True
    except Exception:
        return False


_SINGLE_MATCHUP_SYSTEM_PROMPT = """You are a League of Legends matchup expert. Generate tips for ONE specific champion matchup.

The request format is: "Generate matchup: <PlayerChamp> <role> vs <EnemyChamp>"
- <PlayerChamp> is the champion the PLAYER is playing. This is always the top-level JSON key.
- <EnemyChamp> is the opponent they are facing. This is always the nested enemy key.
- Generate ONLY this one matchup (not a full batch). One enemy entry only.

Use web search to look up win rate data and qualitative tips for this exact matchup (counterstats.net, mobalytics, lolalytics).

OUTPUT FORMAT — strict JSON, exactly this structure (replace placeholders with actual names):
{
  "<PlayerChamp>": {
    "<role>": {
      "<EnemyChamp>": {
        "difficulty": "easy|medium|hard|skill_matchup",
        "who_wins_early": "<PlayerChamp> or <EnemyChamp> or even",
        "trading_pattern": "1-2 sentences on how <PlayerChamp> should trade",
        "ability_to_dodge": "the single most important <EnemyChamp> ability to avoid and how",
        "power_spikes": {"you": "<PlayerChamp>'s key level or item spike", "enemy": "<EnemyChamp>'s key spike"},
        "early_game": "1-2 sentences, levels 1-6 from <PlayerChamp>'s perspective",
        "mid_game": "1-2 sentences, first back through midgame",
        "late_game": "1-2 sentences, late game and teamfight approach",
        "win_condition": "what <PlayerChamp> needs to do to win this matchup",
        "counter_items": ["item1", "item2"],
        "scaling": "who outscales and when",
        "jungle_gankable": true,
        "positioning": "specific positioning advice for <PlayerChamp>"
      }
    }
  }
}

CRITICAL: <PlayerChamp> MUST be the root key. Do NOT put <EnemyChamp> as the root key.
Tips must be actionable and specific — not "play safe", but "stay behind minions to block his Q".
Power spikes must reference specific levels or items.
Use Riot's exact champion name spellings (e.g. "Kog'Maw", "Dr. Mundo", "Nunu & Willump").
Output ONLY the raw JSON — no markdown fences, no prose, nothing else."""


def generate_single_matchup(my_champ: str, enemy_champ: str, role: str, on_log: Callable, on_done: Callable) -> None:
    """
    Background-thread entry point. Calls Claude API to generate tips for one specific matchup.
    on_done(success: bool, champion: str, role: str) is called when complete.
    """
    api_key = _get_api_key()
    if not api_key:
        on_log("  ✗ Anthropic API key not set. Add ANTHROPIC_API_KEY env var or enter it in Settings.", "error")
        on_done(False, my_champ, role)
        return

    on_log(f"  → Requesting Claude: {my_champ} vs {enemy_champ} ({role})...", "info")
    on_log(f"  (Generating matchup tips — may take 15-30s)", "info")
    print(f"[claude] generating single matchup: {my_champ} vs {enemy_champ} {role}", file=sys.stderr)

    body = json.dumps({
        "model": MODEL,
        "max_tokens": 2048,
        "system": _SINGLE_MATCHUP_SYSTEM_PROMPT,
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}],
        "messages": [{"role": "user", "content": f"Generate matchup: {my_champ} {role} vs {enemy_champ}"}],
    }).encode("utf-8")

    print(f"[claude] model={MODEL} request_body={len(body)}b timeout=120s", file=sys.stderr)
    req = urllib.request.Request(
        _API_URL, data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "web-search-2025-03-05",
            "content-type": "application/json",
        },
        method="POST",
    )

    _t0 = time.monotonic()
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=120) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace") if e.fp else ""
        print(f"[claude] HTTP error {e.code}: {body_text[:500]}", file=sys.stderr)
        on_log(f"  ✗ Claude API HTTP {e.code}: {body_text[:200]}", "error")
        on_done(False, my_champ, role)
        return
    except (urllib.error.URLError, socket.timeout) as e:
        print(f"[claude] connection failed: {e}", file=sys.stderr)
        on_log(f"  ✗ Claude API request failed: {e}", "error")
        on_done(False, my_champ, role)
        return
    except Exception as e:
        print(f"[claude] unexpected error: {e}", file=sys.stderr)
        on_log(f"  ✗ Unexpected error calling Claude API: {e}", "error")
        on_done(False, my_champ, role)
        return

    _elapsed = time.monotonic() - _t0
    _usage = raw.get("usage", {})
    print(f"[claude] response in {_elapsed:.1f}s | stop={raw.get('stop_reason','?')} | "
          f"tokens in={_usage.get('input_tokens','?')} out={_usage.get('output_tokens','?')}",
          file=sys.stderr)

    full_text = ""
    for block in raw.get("content", []):
        if block.get("type") == "text":
            full_text += block.get("text", "")

    if not full_text:
        print(f"[claude] no text content in response", file=sys.stderr)
        on_log("  ✗ Claude returned no text content", "error")
        on_done(False, my_champ, role)
        return

    data = _extract_json(full_text)
    if data is None:
        print(f"[claude] JSON extraction failed | preview: {full_text[:300]!r}", file=sys.stderr)
        on_log("  ✗ Could not extract JSON from Claude response", "error")
        on_done(False, my_champ, role)
        return

    # Validate the JSON contains the expected structure before merging
    from matchup_data import get_matchup_tips, refresh_cache
    _found_key = None
    try:
        _top = list(data.keys())[0]
        _roles = data[_top]
        _role_key = list(_roles.keys())[0]
        _enemies = list(_roles[_role_key].keys())
        # Check if my_champ is the root key and enemy_champ is nested
        if _top.lower() == my_champ.lower() and any(e.lower() == enemy_champ.lower() for e in _enemies):
            _found_key = "correct"
        # Check if Claude inverted it (enemy is root, my_champ is nested)
        elif _top.lower() == enemy_champ.lower() and any(e.lower() == my_champ.lower() for e in _enemies):
            _found_key = "inverted"
    except Exception:
        pass

    if _found_key == "inverted":
        print(f"[claude] WARNING: generated data has inverted perspective ({_top} as root, not {my_champ}) — discarding", file=sys.stderr)
        on_log(f"  ✗ Claude generated from {enemy_champ}'s perspective instead of {my_champ}'s — discarding", "error")
        on_done(False, my_champ, role)
        return

    if _found_key is None:
        print(f"[claude] WARNING: could not validate structure — top key={_top!r}, expected={my_champ!r}", file=sys.stderr)
        on_log(f"  ✗ Claude response structure unexpected — champion name mismatch", "error")
        on_done(False, my_champ, role)
        return

    if not _merge_into_matchups(data):
        on_log("  ✗ Failed to merge into matchups.json", "error")
        on_done(False, my_champ, role)
        return

    refresh_cache()

    # Final verification: confirm the expected key is now accessible
    tips = get_matchup_tips(my_champ, enemy_champ, role)
    if tips is None:
        print(f"[claude] post-merge verification failed: {my_champ} vs {enemy_champ} ({role}) not found", file=sys.stderr)
        on_log(f"  ✗ Tips saved but lookup still failed — Claude may have used a different champion name", "error")
        on_done(False, my_champ, role)
        return

    on_log(f"  ✓ {my_champ} vs {enemy_champ} tips saved", "success")
    on_done(True, my_champ, role)


def generate_matchup_batch(champion: str, role: str, on_log: Callable, on_done: Callable) -> None:
    """
    Background-thread entry point. Calls Claude API to generate all matchups for champion/role.
    on_done(success: bool, champion: str, role: str) is called when complete.
    """
    api_key = _get_api_key()
    if not api_key:
        on_log("  ✗ Anthropic API key not set. Add ANTHROPIC_API_KEY env var or enter it in Settings.", "error")
        on_done(False, champion, role)
        return

    on_log(f"  → Requesting Claude: Generate {champion} {role} matchups...", "info")
    on_log(f"  (Searching web + generating 20-30 matchups — may take 60-120s)", "info")
    print(f"[claude] generating: {champion} {role}", file=sys.stderr)

    body = json.dumps({
        "model": MODEL,
        "max_tokens": 8192,
        "system": _SYSTEM_PROMPT,
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 10}],
        "messages": [{"role": "user", "content": f"Generate: {champion} {role}"}],
    }).encode("utf-8")

    print(f"[claude] model={MODEL} request_body={len(body)}b timeout=300s", file=sys.stderr)
    req = urllib.request.Request(
        _API_URL, data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "web-search-2025-03-05",
            "content-type": "application/json",
        },
        method="POST",
    )

    _t0 = time.monotonic()
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=300) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace") if e.fp else ""
        print(f"[claude] HTTP error {e.code}: {body_text[:500]}", file=sys.stderr)
        on_log(f"  ✗ Claude API HTTP {e.code}: {body_text[:200]}", "error")
        on_done(False, champion, role)
        return
    except (urllib.error.URLError, socket.timeout) as e:
        print(f"[claude] connection failed: {e}", file=sys.stderr)
        on_log(f"  ✗ Claude API request failed: {e}", "error")
        on_done(False, champion, role)
        return
    except Exception as e:
        print(f"[claude] unexpected error: {e}", file=sys.stderr)
        on_log(f"  ✗ Unexpected error calling Claude API: {e}", "error")
        on_done(False, champion, role)
        return

    # Log response summary
    _elapsed = time.monotonic() - _t0
    _usage = raw.get("usage", {})
    print(f"[claude] response in {_elapsed:.1f}s | stop={raw.get('stop_reason','?')} | "
          f"tokens in={_usage.get('input_tokens','?')} out={_usage.get('output_tokens','?')}",
          file=sys.stderr)
    for _i, _blk in enumerate(raw.get("content", [])):
        _btype = _blk.get("type", "?")
        if _btype == "text":
            print(f"[claude] block[{_i}] text len={len(_blk.get('text',''))}", file=sys.stderr)
        elif _btype == "tool_use":
            print(f"[claude] block[{_i}] tool_use name={_blk.get('name')} id={_blk.get('id','?')}",
                  file=sys.stderr)

    # Extract text content from response blocks
    full_text = ""
    for block in raw.get("content", []):
        if block.get("type") == "text":
            full_text += block.get("text", "")

    if not full_text:
        print(f"[claude] no text content in response", file=sys.stderr)
        on_log("  ✗ Claude returned no text content", "error")
        on_done(False, champion, role)
        return

    data = _extract_json(full_text)
    if data is None:
        print(f"[claude] JSON extraction failed | full_text len={len(full_text)}", file=sys.stderr)
        print(f"[claude] preview: {full_text[:300]!r}", file=sys.stderr)
        on_log("  ✗ Could not extract JSON from Claude response", "error")
        on_log(f"  (Response preview: {full_text[:200]})", "info")
        on_done(False, champion, role)
        return

    path = _save_batch(champion, role, data)
    # Count matchups in the generated data
    try:
        champ_key = list(data.keys())[0]
        role_key = list(data[champ_key].keys())[0]
        n_matchups = len(data[champ_key][role_key])
    except Exception:
        n_matchups = 0
    print(f"[claude] JSON extracted OK | top-level keys: {list(data.keys())}", file=sys.stderr)
    print(f"[claude] batch saved: {os.path.basename(path)} — {n_matchups} matchups", file=sys.stderr)
    on_log(f"  ✓ Batch saved: {os.path.basename(path)} — {n_matchups} matchups", "success")

    if not _merge_into_matchups(data):
        on_log("  ✗ Failed to merge batch into matchups.json", "error")
        on_done(False, champion, role)
        return

    from matchup_data import refresh_cache
    refresh_cache()
    on_log(f"  ✓ matchups.json updated and cache refreshed", "success")
    on_done(True, champion, role)
