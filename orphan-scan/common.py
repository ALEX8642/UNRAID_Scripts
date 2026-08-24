"""
common.py — shared logic used by every tool in this repo. Single source of truth for
container-path -> host-path conversion, since two independently-maintained copies of this
exact logic once diverged (one got fixed for a bare-category-root edge case, the other
didn't) and caused a real hit-and-run near-miss against a live library.

This file is intentionally small and kept byte-identical across dupe-review/, orphan-scan/,
and mothership/ (Docker builds each in an isolated context, so it can't be a single physical
file on disk the way it is within mothership/, where both tools share this one copy) — if you
ever fix or extend anything in here, port the exact same change to the other two copies.
"""

# Container-path -> host-path prefixes. Edit these for your setup.
PATH_MAP = [
    ("/movies/", "/mnt/user/Media/Movies/"),
    ("/tv/", "/mnt/user/Media/tv/"),
    ("/arr/movies/", "/mnt/user/Media/arr/movies/"),
    ("/arr/shows/", "/mnt/user/Media/arr/shows/"),
]


def to_host_path(p: str) -> tuple[str, bool]:
    """Returns (converted_path, whether a PATH_MAP prefix actually matched).

    Matches with or without a trailing slash: qBittorrent reports a multi-file torrent's
    save_path as the bare category root with no trailing slash (e.g. exactly "/movies"), and
    matching only the slash-terminated form silently failed to convert it — the root cause of
    a real hit-and-run near-miss where files inside actively-seeding multi-file torrents were
    wrongly treated as unknown to qBittorrent.
    """
    for prefix, host in PATH_MAP:
        bare = prefix.rstrip("/")
        if p == bare:
            return host.rstrip("/"), True
        if p.startswith(prefix):
            return host + p[len(prefix):], True
    return p, False
