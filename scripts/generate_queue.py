#!/usr/bin/env python3
"""Generate search_queue.json from keyword × location matrix.
Re-run this to reset the queue or add new combos (existing statuses are preserved).
"""
import json
from pathlib import Path

ROOT = Path(__file__).parent.parent
OUTPUT = ROOT / "search_queue.json"

KEYWORDS = [
    "mechanical design engineer",
    "product design engineer",
    "design engineer",
    "mechanical engineer entry level",
    "product development engineer",
    "robotics engineer",
]

# location, weight (used for reference — Claude scores against profile.md)
LOCATIONS = [
    # Charleston SC area — weight 1.0
    ("Charleston, SC",        1.0),
    ("North Charleston, SC",  1.0),
    ("Summerville, SC",       1.0),
    ("Mount Pleasant, SC",    1.0),
    # Coastal Florida — weight 0.9
    ("Tampa, FL",             0.9),
    ("Jacksonville, FL",      0.9),
    ("Miami, FL",             0.9),
    ("Fort Lauderdale, FL",   0.9),
    ("Sarasota, FL",          0.9),
    ("Melbourne, FL",         0.9),
    ("West Palm Beach, FL",   0.9),
    ("St. Petersburg, FL",    0.9),
    # Boston / New England — weight 0.8
    ("Boston, MA",            0.8),
    ("Cambridge, MA",         0.8),
    ("Portsmouth, NH",        0.8),
    ("Portland, ME",          0.8),
    ("Providence, RI",        0.8),
    # Pacific NW / Mid-Atlantic — weight 0.7
    ("Seattle, WA",           0.7),
    ("Portland, OR",          0.7),
    ("Virginia Beach, VA",    0.7),
    ("Baltimore, MD",         0.7),
    ("Philadelphia, PA",      0.7),
    # California coastal / Texas Gulf — weight 0.6
    ("San Diego, CA",         0.6),
    ("Los Angeles, CA",       0.6),
    ("San Francisco, CA",     0.6),
    ("Houston, TX",           0.6),
    ("Corpus Christi, TX",    0.6),
    # Remote
    ("remote",                0.7),
]

SETTINGS = {
    "batch_size": 10,
    "rerun_after_days": 14,
}

def build_queue():
    existing = {}
    if OUTPUT.exists():
        data = json.loads(OUTPUT.read_text())
        for entry in data.get("queue", []):
            existing[entry["id"]] = entry

    queue = []
    entry_id = 1
    for keyword in KEYWORDS:
        for location, weight in LOCATIONS:
            if entry_id in existing:
                entry = existing[entry_id]
            else:
                entry = {
                    "id": entry_id,
                    "order": entry_id,
                    "location": location,
                    "location_weight": weight,
                    "keyword": keyword,
                    "status": "pending",
                    "last_run": None,
                }
            queue.append(entry)
            entry_id += 1

    result = {"settings": SETTINGS, "queue": queue}
    OUTPUT.write_text(json.dumps(result, indent=2))
    print(f"Wrote {len(queue)} entries to {OUTPUT}")

if __name__ == "__main__":
    build_queue()
