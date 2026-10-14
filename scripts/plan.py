#!/usr/bin/env python3
"""
plan.py -- turns the link list into a job matrix (one job per link, nothing is split).

Env: INPUT_LINKS (space / comma / newline separated, empty -> links.txt),
     GITHUB_RUN_NUMBER, GITHUB_OUTPUT, GITHUB_STEP_SUMMARY
"""
import json
import os
import re
from pathlib import Path

MAX_JOBS = 256  # GitHub matrix limit
RUN = os.getenv("GITHUB_RUN_NUMBER", "0")


def read_links():
    raw = os.environ.get("INPUT_LINKS", "").strip()
    if not raw:
        lines = []
        if Path("links.txt").exists():
            for line in open("links.txt", encoding="utf-8"):
                line = line.split(" #")[0].strip()
                if line and not line.startswith("#"):
                    lines.append(line)
        raw = "\n".join(lines)
    links = []
    for tok in re.split(r"\s+", raw):
        for u in re.split(r",(?=https?://)", tok):
            u = u.strip().strip(",")
            if u and u not in links:
                links.append(u)
    return links


def main():
    links = read_links()
    if len(links) > MAX_JOBS:
        print(f"::warning::{len(links)} links, GitHub allows {MAX_JOBS} jobs; using the first {MAX_JOBS}")
        links = links[:MAX_JOBS]
    include = [{"idx": i, "url": u, "tag": f"run-{RUN}-{i}"} for i, u in enumerate(links, 1)]

    lines = ["## Plan", "| # | Link | Release tag |", "|---|---|---|"]
    lines += [f"| {e['idx']} | {e['url']} | {e['tag']} |" for e in include]
    text = "\n".join(lines)
    print(text)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text + "\n")
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"matrix={json.dumps({'include': include})}\n")
            f.write(f"count={len(include)}\n")
    print(f"{len(include)} job(s) queued")


if __name__ == "__main__":
    main()
