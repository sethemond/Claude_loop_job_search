#!/usr/bin/env python3
"""
Tag creation helper for Claude workflow.

Usage:
    python scripts/tag_create.py --id <id> --label <label> --weight <n>
        --category <cat> --description <desc>
        [--aliases a1,a2,...] [--reason <reason>]

Exit codes:
    0  tag created (status: pending)
    2  exact match — tag already exists (prints existing tag id)
    3  similar match — similar tag exists (prints similar tag id + overlap)
    1  error
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
TAGS_PATH = ROOT / "tags.json"


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def tokenize(s: str) -> set:
    return set(re.split(r"[-_\s]+", s.lower().strip()))


def similarity(a: str, b: str) -> float:
    ta, tb = tokenize(a), tokenize(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def find_match(tags: dict, new_id: str, new_label: str, new_aliases: list):
    """Return (match_type, tag_id) — match_type is 'exact', 'similar', or None."""
    new_terms = {new_id, slugify(new_label)} | {slugify(a) for a in new_aliases}

    for tid, tag in tags.items():
        existing_terms = {tid, slugify(tag.get("label", ""))} | {
            slugify(a) for a in tag.get("aliases", [])
        }
        # Exact: any term overlap
        if new_terms & existing_terms:
            return "exact", tid

    # Similar: token overlap >= 50% on id or label
    for tid, tag in tags.items():
        sim_id = similarity(new_id, tid)
        sim_label = similarity(new_label, tag.get("label", ""))
        if max(sim_id, sim_label) >= 0.5:
            return "similar", tid

    return None, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--weight", type=int, required=True)
    parser.add_argument("--category", required=True,
                        choices=["skills", "experience", "industry", "location", "salary", "culture", "avoid", "fit"])
    parser.add_argument("--description", required=True)
    parser.add_argument("--aliases", default="")
    parser.add_argument("--reason", default="")
    args = parser.parse_args()

    tag_id = slugify(args.id) if args.id != slugify(args.id) else args.id
    aliases = [a.strip() for a in args.aliases.split(",") if a.strip()] if args.aliases else []

    if not TAGS_PATH.exists():
        print(f"ERROR: {TAGS_PATH} not found", file=sys.stderr)
        sys.exit(1)

    data = json.loads(TAGS_PATH.read_text(encoding="utf-8"))
    tags = data.get("tags", {})

    match_type, matched_id = find_match(tags, tag_id, args.label, aliases)

    if match_type == "exact":
        print(json.dumps({"status": "exists", "matched_id": matched_id}))
        sys.exit(2)

    if match_type == "similar":
        print(json.dumps({"status": "similar", "matched_id": matched_id,
                          "suggestion": f"Consider using or aliasing '{matched_id}' instead"}))
        sys.exit(3)

    new_tag = {
        "id": tag_id,
        "label": args.label,
        "category": args.category,
        "weight": args.weight,
        "status": "pending",
        "description": args.description,
        "aliases": aliases,
    }
    if args.reason:
        new_tag["proposed_reason"] = args.reason

    tags[tag_id] = new_tag
    data["tags"] = tags
    TAGS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"status": "created", "id": tag_id}))
    sys.exit(0)


if __name__ == "__main__":
    main()
