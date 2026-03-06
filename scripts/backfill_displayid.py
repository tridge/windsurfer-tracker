#!/usr/bin/env python3
"""Backfill displayid into existing results.jsonl from users.json overrides."""

import json
import sys
from pathlib import Path


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <users.json> <results.jsonl>")
        sys.exit(1)

    users_file = Path(sys.argv[1])
    races_file = Path(sys.argv[2])

    # Build sailor_id -> display name map from users.json
    name_map = {}
    with open(users_file) as f:
        users_data = json.load(f)
    for key, val in users_data.get("users", {}).items():
        name = val.get("name")
        if not name:
            continue
        if key.startswith("did:"):
            sid = val.get("_last_id")
            if sid:
                name_map[sid] = name
        else:
            name_map[key] = name

    if not name_map:
        print("No user overrides with names found")
        sys.exit(1)

    print(f"Found {len(name_map)} name(s): {name_map}")

    # Read results.jsonl
    with open(races_file) as f:
        lines = f.read().strip().split('\n')

    if not lines or not lines[0]:
        print("Empty results.jsonl")
        sys.exit(1)

    header = json.loads(lines[0])
    races = [json.loads(line) for line in lines[1:] if line.strip()]

    # Backfill displayid
    updated_count = 0
    for race in races:
        for finisher in race.get("finishers", []):
            sid = finisher.get("sailor_id")
            if sid and sid in name_map and "displayid" not in finisher:
                finisher["displayid"] = name_map[sid]
                updated_count += 1

    if updated_count == 0:
        print("No finishers needed updating")
        return

    # Write atomically
    tmp_file = races_file.with_suffix('.tmp')
    with open(tmp_file, 'w') as f:
        f.write(json.dumps(header) + '\n')
        for race in races:
            f.write(json.dumps(race) + '\n')
    tmp_file.rename(races_file)

    print(f"Updated {updated_count} finisher(s) with displayid")


if __name__ == "__main__":
    main()
