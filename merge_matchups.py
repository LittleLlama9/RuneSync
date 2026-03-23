"""
merge_matchups.py — Merges JSON batch files from matchup_batches/ into matchups.json

USAGE:
    python merge_matchups.py

Drop any .json batch files into C:\\Users\\Matth\\RuneSync\\matchup_batches\\
and run this script. It will deep-merge all of them into matchups.json,
preserving existing data and adding/overwriting new entries.

Batch files can be named anything (e.g. darius_top.json, zed_mid.json).
After a successful merge, processed files are moved to matchup_batches/merged/
so you always know what's been applied.
"""

import json, os, shutil, sys

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
BATCHES_DIR  = os.path.join(BASE_DIR, "matchup_batches")
MERGED_DIR   = os.path.join(BATCHES_DIR, "merged")
OUTPUT_FILE  = os.path.join(BASE_DIR, "matchups.json")


def deep_merge(base: dict, incoming: dict) -> dict:
    """
    Recursively merge incoming into base.
    - Dicts are merged at every level.
    - Leaf values (strings, lists, bools) from incoming overwrite base.
    """
    for key, value in incoming.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(data: dict, path: str):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def main():
    os.makedirs(MERGED_DIR, exist_ok=True)

    # Load existing matchups.json (may be empty {})
    if os.path.exists(OUTPUT_FILE):
        try:
            base = load_json(OUTPUT_FILE)
            print(f"[merge] Loaded existing matchups.json ({len(base)} champions)")
        except json.JSONDecodeError as e:
            print(f"[merge] ERROR: matchups.json is not valid JSON: {e}")
            sys.exit(1)
    else:
        base = {}
        print("[merge] No matchups.json found — starting fresh")

    # Find all .json batch files (not subdirectories)
    batch_files = sorted([
        f for f in os.listdir(BATCHES_DIR)
        if f.endswith(".json") and os.path.isfile(os.path.join(BATCHES_DIR, f))
    ])

    if not batch_files:
        print("[merge] No batch files found in matchup_batches/ — nothing to do.")
        print(f"        Drop .json files into: {BATCHES_DIR}")
        return

    merged_count = 0
    error_count  = 0

    for filename in batch_files:
        filepath = os.path.join(BATCHES_DIR, filename)
        try:
            incoming = load_json(filepath)

            if not isinstance(incoming, dict):
                print(f"[merge] SKIP {filename} — top level is not a JSON object")
                error_count += 1
                continue

            champs_in_file = list(incoming.keys())
            deep_merge(base, incoming)

            # Move to merged/ archive
            dest = os.path.join(MERGED_DIR, filename)
            # Avoid name collisions in merged/ by appending a counter if needed
            if os.path.exists(dest):
                name, ext = os.path.splitext(filename)
                counter = 1
                while os.path.exists(dest):
                    dest = os.path.join(MERGED_DIR, f"{name}_{counter}{ext}")
                    counter += 1
            shutil.move(filepath, dest)

            print(f"[merge] OK    {filename} — {champs_in_file}")
            merged_count += 1

        except json.JSONDecodeError as e:
            print(f"[merge] ERROR {filename} — invalid JSON: {e}")
            error_count += 1
        except Exception as e:
            print(f"[merge] ERROR {filename} — {e}")
            error_count += 1

    if merged_count == 0:
        print("[merge] No files were successfully merged.")
        return

    # Save merged result
    save_json(base, OUTPUT_FILE)

    total_champs  = len(base)
    total_matchups = sum(
        len(matchups)
        for champ in base.values()
        for matchups in champ.values()
        if isinstance(matchups, dict)
    )

    print(f"\n[merge] Done! Merged {merged_count} file(s). "
          f"{error_count} error(s).")
    print(f"[merge] matchups.json now has {total_champs} champions, "
          f"{total_matchups} total matchups.")
    print(f"[merge] Processed files archived to: {MERGED_DIR}")

    # Keep dist\matchups.json in sync so the compiled exe can find it
    dist_path = os.path.join(BASE_DIR, "dist", "matchups.json")
    if os.path.isdir(os.path.join(BASE_DIR, "dist")):
        shutil.copy2(OUTPUT_FILE, dist_path)
        print(f"[merge] Copied matchups.json → dist\\")


if __name__ == "__main__":
    main()
