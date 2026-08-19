#!/usr/bin/env python3
"""LLM-wiki hot.md staleness check.

Run as a Claude Code SessionStart hook. Auto-scopes to the llm-wiki vault
by detecting wiki/hot.md + wiki/sources/ in $PWD; silently exits otherwise.

Reads any items in hot.md's "In `raw/`, ready" section, extracts each
raw/ path, and checks whether a wiki/sources/summary-*.md already lists
that path in its source_file: field. Surfaces stale carry-over so the
session does not treat already-ingested items as actionable.

Auto-memory rule: hot.md follow-up lists must be verified by file-system
check before listing. Recurring stale-carry-over events on 2026-05-01
(2-for-2 in a single day on Tier-I/Tier-K items) motivated promotion of
this check from per-session-discipline to settings.json hook.

Stdin JSON is consumed and ignored. Output is a single SessionStart
hookSpecificOutput JSON object on stdout when staleness is detected;
silent when clean.
"""

import json
import os
import re
import sys
from pathlib import Path


def main():
    try:
        sys.stdin.read()
    except Exception:
        pass

    cwd = Path.cwd()
    hot_md = cwd / "wiki" / "hot.md"
    sources_dir = cwd / "wiki" / "sources"

    if not hot_md.is_file() or not sources_dir.is_dir():
        return

    try:
        text = hot_md.read_text(errors="replace")
    except Exception:
        return

    section_re = re.compile(
        r"\*\*In\s+`raw/`,\s+ready:?\*\*\s*\n(.+?)(?=\n\s*\*\*[A-Z]|\n\s*###|\Z)",
        re.DOTALL,
    )
    m = section_re.search(text)
    if not m:
        return

    section = m.group(1)

    paths = re.findall(r"raw/[a-zA-Z0-9_\-]+/[^\s`)]+", section)
    if not paths:
        return

    summaries = list(sources_dir.glob("*.md"))
    if not summaries:
        return

    summary_blobs = []
    for s in summaries:
        try:
            summary_blobs.append((s.name, s.read_text(errors="replace")))
        except Exception:
            continue

    seen = set()
    stale = []
    for path in paths:
        path = path.rstrip(".,;:")
        if path in seen:
            continue
        seen.add(path)

        basename = re.sub(r"\.\{[^}]+\}$", "", path)
        basename = re.sub(r"\.[a-zA-Z0-9]+$", "", basename)
        basename = os.path.basename(basename)
        if not basename or len(basename) < 4:
            continue

        for name, blob in summary_blobs:
            sf_re = re.compile(
                r"^source_file:.*" + re.escape(basename), re.MULTILINE
            )
            if sf_re.search(blob):
                stale.append((path, name))
                break

    if not stale:
        return

    lines = [
        "[llm-wiki staleness] hot.md \"in raw/, ready\" items already covered by an existing summary — verify before treating as actionable:"
    ]
    for path, summary in stale:
        lines.append(f"  - {path} -> {summary}")
    lines.append(
        "Auto-memory rule: hot.md follow-up lists are verified by file-system check before listing. "
        "Re-grep wiki/sources/*.md for source_file: matches before claiming any item is ready, and re-baseline hot.md if drift is found."
    )

    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n".join(lines),
        }
    }
    sys.stdout.write(json.dumps(payload))


if __name__ == "__main__":
    main()
