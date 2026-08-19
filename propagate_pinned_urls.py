#!/usr/bin/env python3
"""Read /tmp/wayback_save_results.tsv; for each ok row, update the matching
summary's archive_url: with the new pinned URL and set archive_url_status: pinned."""
import csv
import re
from pathlib import Path

SOURCES = Path.home() / "Obsidian" / "brains-claude" / "llm-wiki" / "wiki" / "sources"
TSV = Path("/tmp/wayback_save_results.tsv")


def update_file(path, pinned_url):
    text = path.read_text()
    # Match the frontmatter region
    m = re.match(r"^(---\n)(.*?)(\n---\n)", text, re.DOTALL)
    if not m:
        return False, "no-frontmatter"
    head, fm, tail = m.group(1), m.group(2), m.group(3)
    rest = text[m.end():]
    lines = fm.split("\n")

    found_url = found_status = False
    for i, line in enumerate(lines):
        if re.match(r"^archive_url:", line):
            lines[i] = f"archive_url: {pinned_url}"
            found_url = True
        elif re.match(r"^archive_url_status:", line):
            lines[i] = "archive_url_status: pinned"
            found_status = True
    if not (found_url and found_status):
        return False, f"missing-fields(url={found_url},status={found_status})"
    new_text = head + "\n".join(lines) + tail + rest
    if new_text == text:
        return False, "no-change"
    path.write_text(new_text)
    return True, "ok"


def main(apply=False):
    if not TSV.exists():
        print(f"Missing {TSV}")
        return
    rows = list(csv.DictReader(TSV.open(), delimiter="\t"))
    ok_rows = [r for r in rows if r["status"] == "ok"]
    fail_rows = [r for r in rows if r["status"] != "ok"]
    print(f"Loaded {len(rows)} rows: {len(ok_rows)} ok / {len(fail_rows)} fail")

    if not apply:
        print(f"\n=== DRY RUN — would propagate {len(ok_rows)} pinned URLs ===")
        for r in ok_rows[:5]:
            print(f"  {r['filename']}  →  {r['result'][:100]}")
        if len(ok_rows) > 5:
            print(f"  ... and {len(ok_rows) - 5} more")
        print(f"\n=== {len(fail_rows)} failures (will remain needs-save) ===")
        for r in fail_rows:
            print(f"  {r['filename']}  ::  {r['status']}: {r['result'][:120]}")
        return

    updated = errors = 0
    for r in ok_rows:
        path = SOURCES / r["filename"]
        if not path.exists():
            print(f"  MISSING FILE: {r['filename']}")
            errors += 1
            continue
        success, msg = update_file(path, r["result"])
        if success:
            updated += 1
        else:
            print(f"  ERROR {r['filename']}: {msg}")
            errors += 1
    print(f"\n=== APPLIED ===  updated={updated}  errors={errors}  fail-still-needs-save={len(fail_rows)}")


if __name__ == "__main__":
    import sys
    main(apply="--apply" in sys.argv)
