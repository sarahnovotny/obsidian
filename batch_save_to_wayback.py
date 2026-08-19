#!/usr/bin/env python3
"""Batch-submit needs-save URLs to Wayback Save Page Now.

For each URL: curl -L the SPN endpoint, parse the final URL after redirects.
A successful save lands on https://web.archive.org/web/YYYYMMDDHHMMSS/<url>.
Failures land back on the SPN form or a Wayback error page.

Output: TSV with (filename, original_url, status, pinned_url_or_error).
"""
import re
import subprocess
import sys
import time
from pathlib import Path

SOURCES = Path.home() / "Obsidian" / "brains-claude" / "llm-wiki" / "wiki" / "sources"
OUT_TSV = Path("/tmp/wayback_save_results.tsv")
URL_RE = re.compile(r"https?://[^\s\"'<>)]+")
PINNED_RE = re.compile(r"^https?://web\.archive\.org/web/(\d{14})/(.+)$")
INTER_REQUEST_DELAY = 6  # seconds — polite to IA


def parse_frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not m:
        return {}
    fields = {}
    cur = None
    buf = []
    for line in m.group(1).split("\n"):
        if re.match(r"^[a-z_]+:", line):
            if cur:
                fields[cur] = "\n".join(buf).strip()
            k, _, v = line.partition(":")
            cur = k.strip()
            buf = [v.strip()]
        else:
            buf.append(line)
    if cur:
        fields[cur] = "\n".join(buf).strip()
    return fields


def extract_url(raw):
    if not raw:
        return None
    v = raw.strip().strip('"').strip("'")
    v = re.sub(r"\s+#.*$", "", v).strip()
    m = URL_RE.search(v)
    if not m:
        return None
    return m.group(0).rstrip(").,;'\"]")


def collect_targets():
    targets = []
    for path in sorted(SOURCES.glob("*.md")):
        text = path.read_text()
        fields = parse_frontmatter(text)
        if fields.get("type", "").strip() != "source":
            continue
        if fields.get("archive_url_status", "").strip() != "needs-save":
            continue
        url = extract_url(fields.get("source_url", ""))
        if url:
            targets.append((path.name, url))
    return targets


def save_one(url, timeout_sec=120):
    """Submit URL to SPN, return (status, pinned_url_or_error_str)."""
    cmd = [
        "curl",
        "-s",
        "-L",
        "-o",
        "/dev/null",
        "--max-time",
        str(timeout_sec),
        "-w",
        "%{http_code}\t%{url_effective}",
        f"https://web.archive.org/save/{url}",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec + 10)
    except subprocess.TimeoutExpired:
        return "timeout", f"curl exceeded {timeout_sec}s"
    if result.returncode != 0:
        return "curl-error", f"rc={result.returncode} stderr={result.stderr[:120]}"
    parts = result.stdout.split("\t")
    if len(parts) != 2:
        return "parse-error", result.stdout[:200]
    http_code, final_url = parts
    if not PINNED_RE.match(final_url):
        return "save-failed", f"http={http_code} final={final_url[:160]}"
    return "ok", final_url


def main():
    targets = collect_targets()
    print(f"Submitting {len(targets)} URLs to Wayback SPN ...", flush=True)
    OUT_TSV.write_text("filename\toriginal_url\tstatus\tresult\n")
    ok = fail = 0
    for i, (name, url) in enumerate(targets, 1):
        t0 = time.time()
        status, result = save_one(url)
        dt = time.time() - t0
        with OUT_TSV.open("a") as f:
            f.write(f"{name}\t{url}\t{status}\t{result}\n")
        marker = "OK" if status == "ok" else "FAIL"
        print(f"[{i:>2}/{len(targets)}] {marker:5s} {dt:5.1f}s  {name}  ::  {result[:100]}", flush=True)
        if status == "ok":
            ok += 1
        else:
            fail += 1
        if i < len(targets):
            time.sleep(INTER_REQUEST_DELAY)
    print(f"\n=== DONE === ok={ok} fail={fail} total={len(targets)}", flush=True)


if __name__ == "__main__":
    main()
