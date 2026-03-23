"""
simulate.py — RuneSync champ select draft simulator

Walks through the actual League draft order (blue 1 / red 2 / blue 2 / red 1)
and shows what RuneSync would output after each pick.

Run:  python simulate.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from champion_roles import infer_full_assignment, get_role_weights

ROLES = ["top", "jungle", "mid", "bot", "support"]

ROLE_LABEL = {
    "top": "Top", "jungle": "Jungle", "mid": "Mid",
    "bot": "Bot", "support": "Support"
}

# (team, pick_number_on_that_team)
DRAFT_ORDER = [
    ("blue", 1),
    ("red",  1), ("red",  2),
    ("blue", 2), ("blue", 3),
    ("red",  3), ("red",  4),
    ("blue", 4), ("blue", 5),
    ("red",  5),
]

def bar(pct):
    return "█" * round(pct / 10) + "░" * (10 - round(pct / 10))

def ask(prompt, valid=None):
    """Prompt until a non-empty valid answer is given."""
    while True:
        val = input(prompt).strip()
        if not val:
            continue
        if valid and val.lower() not in valid:
            print(f"    ✘  Please enter one of: {', '.join(valid)}")
            continue
        return val

def ask_champ(prompt):
    """Prompt for a champ name — blank is allowed (means not picked / skipping)."""
    return input(prompt).strip()


def print_runesync(my_role, my_champ, enemy_picks, pick_index):
    """Print what RuneSync would output right now."""
    assignment, guesses = infer_full_assignment(enemy_picks)
    detected   = assignment.get(my_role)
    is_guess   = False
    if not detected:
        detected = guesses.get(my_role)
        is_guess = detected is not None

    print(f"\n    ┌─ RuneSync after pick {pick_index} {'─' * 25}")
    print(f"    │  Enemy picks so far : {', '.join(enemy_picks) if enemy_picks else '(none)'}")

    if not detected:
        rl = ROLE_LABEL.get(my_role, my_role.title())
        print(f"    │  → Waiting for enemy {rl} laner...")
    elif not my_champ:
        rl = ROLE_LABEL.get(my_role, my_role.title())
        label = f"⚠  Best guess — {detected} (flex pick, may not be {rl})" if is_guess else f"⚔  Enemy {rl} laner identified: {detected}"
        print(f"    │  {label}")
        w = get_role_weights(detected)
        if w:
            pct = w.get(my_role, 0.0)
            print(f"    │     ({rl} play rate: {pct:.0f}%  {bar(pct)})")
        print(f"    │  → You haven't picked yet.")
        print(f"    │     Would fetch counterpick suggestions vs {detected} from u.gg")
    else:
        rl = ROLE_LABEL.get(my_role, my_role.title())
        label = f"⚠  Best guess — {detected} (flex pick, may not be {rl})" if is_guess else f"⚔  Enemy {rl} laner identified: {detected}"
        print(f"    │  {label}")
        w = get_role_weights(detected)
        if w:
            pct = w.get(my_role, 0.0)
            print(f"    │     ({rl} play rate: {pct:.0f}%  {bar(pct)})")
        print(f"    │  → Would run matchup lookup: {my_champ} vs {detected} on u.gg")

    print(f"    └{'─' * 40}")


def run_simulator():
    print()
    print("╔══════════════════════════════════════════════════════╗")
    print("║         RuneSync  —  Draft Simulator                 ║")
    print("╚══════════════════════════════════════════════════════╝")
    print()
    print("  This simulates a full champ select draft so you can test")
    print("  what RuneSync would output at each stage without loading a game.")
    print()

    # ── Step 1: Side ─────────────────────────────────────────────────────
    print("  ── Step 1: Your Side ──────────────────────────────────────")
    print("  Blue side picks first (pick 1), Red side gets last pick (pick 10).")
    my_side = ask("  Are you on blue or red side? (blue/red): ",
                  valid=["blue", "red", "b", "r"])
    my_side    = "blue" if my_side.startswith("b") else "red"
    enemy_side = "red"  if my_side == "blue" else "blue"
    print(f"  → You are on {my_side.upper()} side.\n")

    # ── Step 2: Role ─────────────────────────────────────────────────────
    print("  ── Step 2: Your Role ──────────────────────────────────────")
    print("  RuneSync uses this to figure out which enemy is in your lane.")
    my_role = ask(f"  Your role (top / jungle / mid / bot / support): ",
                  valid=ROLES)
    my_role = my_role.lower()
    print(f"  → Watching for enemy {ROLE_LABEL[my_role]} laner.\n")

    # ── Step 3: Draft ────────────────────────────────────────────────────
    print("  ── Step 3: Draft ──────────────────────────────────────────")
    print("  Enter each pick as it happens. Press Enter to skip a pick")
    print("  (e.g. if you don't know it yet, or it hasn't happened).")
    print()

    my_champ     = ""
    my_picks     = []   # champs your team has picked
    enemy_picks  = []   # champs enemy team has picked
    last_detected = ""  # track changes to avoid repeat output

    for overall_pick, (team, slot) in enumerate(DRAFT_ORDER, start=1):
        is_my_team = (team == my_side)
        team_label = "YOUR TEAM" if is_my_team else "ENEMY TEAM"
        side_label = team.upper()

        # Show a clear header for this pick
        print(f"  Pick {overall_pick:>2}/10  [{side_label} — {team_label} — pick #{slot}]")

        if is_my_team:
            # ── My team's pick ────────────────────────────────────────────

            # Before prompting, check if enemy laner is already known
            # and we haven't picked yet — show counterpick suggestions now
            if not my_champ and enemy_picks:
                assignment_now, guesses_now = infer_full_assignment(enemy_picks)
                laner_now = assignment_now.get(my_role)
                laner_is_guess = False
                if not laner_now:
                    laner_now = guesses_now.get(my_role)
                    laner_is_guess = laner_now is not None
                if laner_now:
                    rl = ROLE_LABEL[my_role]
                    print(f"")
                    print(f"    ╔══ IT'S YOUR PICK TURN ══════════════════════════════")
                    if laner_is_guess:
                        print(f"    ║  ⚠  Best guess — {laner_now} may be enemy {rl} laner (flex pick)")
                    else:
                        print(f"    ║  Enemy {rl} laner is: {laner_now}")
                    w = get_role_weights(laner_now)
                    if w:
                        pct = w.get(my_role, 0.0)
                        print(f"    ║  ({rl} play rate: {pct:.0f}%  {bar(pct)})")
                    print(f"    ║")
                    print(f"    ║  → RuneSync would fetch top counters vs {laner_now} here.")
                    print(f"    ║    (u.gg counter scrape runs in real app — skipped in sim)")
                    print(f"    ╚════════════════════════════════════════════════════")
                    print()
                else:
                    rl = ROLE_LABEL[my_role]
                    print(f"")
                    print(f"    ╔══ IT'S YOUR PICK TURN ══════════════════════════════")
                    print(f"    ║  → Enemy {rl} laner not picked yet — no counterpick data.")
                    print(f"    ╚════════════════════════════════════════════════════")
                    print()

            if not my_champ:
                # Haven't picked yet — could be us or an ally
                champ = ask_champ(f"    Enter your pick (or an ally's pick, or blank to skip): ")
                if champ:
                    is_you = ask(f"    Is {champ} YOUR champion? (y/n): ", valid=["y", "n"])
                    if is_you == "y":
                        my_champ = champ
                        print(f"    ✓ You locked in {my_champ} ({ROLE_LABEL[my_role]})")
                    else:
                        my_picks.append(champ)
                        print(f"    ✓ Ally picked {champ}")
            else:
                # Already have my champ — this is an ally
                champ = ask_champ(f"    Enter ally pick (or blank to skip): ")
                if champ:
                    my_picks.append(champ)
                    print(f"    ✓ Ally picked {champ}")
        else:
            # ── Enemy pick ────────────────────────────────────────────────
            champ = ask_champ(f"    Enter enemy pick (or blank if unknown/skip): ")
            if champ:
                enemy_picks.append(champ)
                print(f"    ✓ Enemy picked {champ}")

        # ── RuneSync output after this pick ───────────────────────────────
        if enemy_picks:
            assignment, guesses = infer_full_assignment(enemy_picks)
            detected   = assignment.get(my_role) or guesses.get(my_role)
            new_laner  = detected or ""

            # Print RuneSync block if the detected laner changed, or if
            # we now have our champ and the laner was already known
            if new_laner != last_detected or (my_champ and detected and not last_detected):
                print_runesync(my_role, my_champ, enemy_picks, overall_pick)
                last_detected = new_laner
            elif is_my_team is False and champ and not detected:
                # Enemy picked but still no laner — remind user we're waiting
                rl = ROLE_LABEL.get(my_role, my_role.title())
                print(f"\n    → Still waiting for enemy {rl} laner...\n")

        print()


    # ── Final summary ─────────────────────────────────────────────────────
    print("═" * 56)
    print("  Draft complete — final RuneSync state")
    print("═" * 56)
    print(f"  Your champion : {my_champ or '(not picked)'}")
    print(f"  Your role     : {ROLE_LABEL[my_role]}")
    print()

    assignment, guesses = infer_full_assignment(enemy_picks)
    print("  Enemy team role assignment:")
    for role in ROLES:
        champ  = assignment.get(role, "—  (no pick)")
        marker = " ◄ YOUR LANE" if role == my_role else ""
        print(f"    {ROLE_LABEL[role]:<12} {champ}{marker}")

    unroled = [c for c in enemy_picks
               if c not in assignment.values() and get_role_weights(c)]
    if unroled:
        print(f"\n  ⚠  Role conflicts (couldn't assign): {', '.join(unroled)}")

    detected = assignment.get(my_role) or guesses.get(my_role)
    print()
    if detected and my_champ:
        print(f"  ✔  RuneSync would run matchup lookup: {my_champ} vs {detected}")
    elif detected:
        print(f"  ✔  RuneSync would fetch counterpick suggestions vs {detected}")
    else:
        print(f"  ✘  Could not determine enemy {ROLE_LABEL[my_role]} laner from picks")
    print()


if __name__ == "__main__":
    while True:
        run_simulator()
        again = input("  Run another draft? (y/n): ").strip().lower()
        print()
        if again != "y":
            break
