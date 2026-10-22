#!/usr/bin/env python3
"""Minimal emulation of the exact jq filters used by library_health.sh / tag-tracker-issues.sh (real jq is not installed in the review sandbox).
Follows jq semantics: unparsable input -> exit 2; type error -> exit 5 (output printed before the error stays); empty input -> no output, exit 0 (4 with -e)."""
import json
import sys

args = sys.argv[1:]
flags = [a for a in args if a.startswith("-") and len(a) <= 3 and a not in ("-n",)]
if "-n" in args:
    i = args.index("--arg"); print(json.dumps({"content": args[i + 2]})); sys.exit(0)
filt = [a for a in args if not a.startswith("-")][-1]
raw = sys.stdin.read()
if not raw.strip():
    sys.exit(4 if "-e" in flags else 0)
try:
    data = json.loads(raw)
except ValueError:
    sys.stderr.write("jq: error: parse error\n"); sys.exit(2)


class Err(Exception):
    pass


def iterate(x):
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        return list(x.values())
    raise Err("Cannot iterate")


def field(x, k):
    if x is None:
        return None
    if isinstance(x, dict):
        return x.get(k)
    raise Err(f"Cannot index {type(x).__name__} with {k}")


def out(v):
    print(v if isinstance(v, str) else json.dumps(v))


def run():
    if filt == ".version":
        v = field(data, "version")
        if v in (None, False):
            sys.exit(1)
        out(v)
    elif filt == ".[]|select(.hasFile)|.movieFile.path":
        for m in iterate(data):
            if field(m, "hasFile"):
                out(field(field(m, "movieFile"), "path"))
    elif filt == ".[]|select(.statistics.episodeFileCount>0)|.id":
        for s in iterate(data):
            c = field(field(s, "statistics"), "episodeFileCount")
            if c is not None and c > 0:
                out(field(s, "id"))
    elif filt == ".[].path":
        for e in iterate(data):
            out(field(e, "path"))
    elif filt.startswith("if type==\"array\""):
        if isinstance(data, list) and len(data) > 0:
            out(len([t for t in data if field(t, "state") in ("missingFiles", "error")]))
        else:
            out("unreadable")
    elif filt.startswith("[.records[]?|select("):
        n = 0
        if isinstance(data, dict) and isinstance(data.get("records"), list):
            for r in data["records"]:
                if field(r, "trackedDownloadState") in ("importBlocked", "importFailed") or field(r, "trackedDownloadStatus") == "warning":
                    n += 1
        out(n)
    elif filt.startswith(".[]|select(.hasFile)|(.path|sub("):
        for m in iterate(data):
            if field(m, "hasFile"):
                out(str(field(m, "path")).replace("/media/", "Media/", 1) + "/" + str(field(field(m, "movieFile"), "relativePath")))
    elif filt == ".[].hash":
        for t in iterate(data):
            out(field(t, "hash"))
    elif filt.startswith(".[] | select(.url | test("):
        for t in iterate(data):
            u = field(t, "url")
            if not isinstance(u, str):
                raise Err("test input must be string")
            if u.startswith("http") and field(t, "status") == 4:
                out(field(t, "msg"))
    else:
        sys.stderr.write("shim: unsupported filter " + filt + "\n"); sys.exit(3)


try:
    run()
except Err as e:
    sys.stderr.write(f"jq: error: {e}\n"); sys.exit(5)
