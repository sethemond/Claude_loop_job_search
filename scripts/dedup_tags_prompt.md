You are performing a one-time tag consolidation on a job search system. Your output is a modified tags.json file — not a chat summary.

## Your task

Read tags.json. Identify tags that are:
1. **Duplicates** — same concept covered by two different tag IDs (e.g., "solidworks" and "solidworks-cad")
2. **Near-duplicates** — almost the same but slightly differently worded
3. **Pending tags that should be merged** — a pending tag whose concept is already covered by an approved tag (add as alias instead)

For each group of duplicates:
- Keep the tag with the higher weight (or if equal, the one with more job assignments)
- Merge the other tag's aliases into the kept tag
- Record what you merged and why

## What you may NOT do
- Change any weights
- Delete approved tags without merging their aliases
- Add new tags
- Modify jobs.json

## Output

Write the updated tags.json with:
- Duplicates consolidated (one tag kept, aliases merged)
- Pending tags that overlap approved tags either: merged as aliases (if same concept) or kept as-is (if genuinely new)

Then write a plain-text summary to `logs/dedup_report.txt` listing:
- Each merge performed: `merged <source_id> into <target_id> — reason`
- Each pending tag kept: `kept pending <id> — reason`
- Total tags before and after

Read tags.json first. Then write your changes. Then stop.
