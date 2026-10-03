#!/usr/bin/env python3
"""Regression test for the hostile-`.torrent` safety gate (2026-09-10; executables
tolerated beside media, 2026-10-03).

The check refuses a `.torrent` whose file list is hostile -- a path traversal or an
absolute path -- BEFORE qBittorrent is ever asked to add it. Executable files are NOT
hostile by themselves any more: the pipeline never runs a release's helpers, so a
`Remove-Dub.bat`/`mkvmerge.exe` beside the episodes is clutter. A torrent whose ONLY
files are executables is still refused -- there is nothing the fleet would file.

**Why it lives in Torrent-Ingest now.** It used to live only in the searcher, which ran it
on every drop it made. The searcher was removed on 2026-09-10, and hand-dropping a
`.torrent` into the watch folder is the fleet's only remaining admission path -- so a
straight deletion would have taken the check away from the one route that still admits
anything. That is §4.22's shape (a gate on one of several paths, one traffic shift from
dark) with the traffic already shifted.

**Both directions, because a filter that can never be true reads exactly like a clean
result (§4.5).** Part 1 proves it REFUSES each hostile shape. Part 2 proves it ACCEPTS
every real `.torrent` on the ADMISSION paths -- if it refused those too it would be
"safe" and useless, and the fleet would stop admitting anything the owner dropped.

`failed/` is deliberately NOT one of those paths. It is where the state machine parks a
`.torrent` it has already proven unreadable, and the safety gate is consulted on the way
IN -- so scanning it here would assert that a dead file must pass the gate that the
state machine already rejected, and the check would go red every time the owner drops a
truncated file. The gate's real corpus is the watch-folder top level (a fresh drop,
before filing), the three live subfolders, and the local source mirror.

    python3 scripts/test_torrent_metadata_safety.py

Read-only. Exit 0 means every check passed.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import acceptance_gate                                           # noqa: E402
import config                                                    # noqa: E402

failures = []


def _enc(o):
    """Minimal bencoder -- fixtures only, so the test builds the bytes it judges."""
    if isinstance(o, int):
        return b"i" + str(o).encode() + b"e"
    if isinstance(o, bytes):
        return str(len(o)).encode() + b":" + o
    if isinstance(o, list):
        return b"l" + b"".join(_enc(x) for x in o) + b"e"
    if isinstance(o, dict):
        return b"d" + b"".join(_enc(k) + _enc(v) for k, v in sorted(o.items())) + b"e"
    raise TypeError(type(o))


def _torrent(paths):
    """A multi-file `.torrent`'s bytes whose file list is `paths` (list of str)."""
    files = [{b"length": 1024, b"path": [seg.encode() for seg in p.split("/")]}
             for p in paths]
    return _enc({b"announce": b"http://x/announce",
                 b"info": {b"name": b"pack", b"piece length": 262144,
                           b"pieces": b"\x00" * 20, b"files": files}})


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got}, expected {want}")
        print(f"  FAIL {label}: got {got}, expected {want}")
    else:
        print(f"  ok   {label}")


# --- Part 1: it REFUSES the shapes that can escape the download dir, and packs with
# nothing to file ---------------------------------------------------------------
print("Part 1 -- traversal, absolute paths, and executable-only packs are refused")
HOSTILE = {
    "parent traversal":       ["../../../../etc/passwd"],
    "traversal mid-path":     ["Show/../../../evil.mkv"],
    "absolute path":          ["/etc/passwd"],
    "absolute path + media":  ["/etc/passwd", "Show/S01E01.mkv"],
    "executable only":        ["Show/setup.exe"],
    "disk image only":        ["Show/reader.dmg"],
    "shortcut only":          ["Show/open.lnk"],
    "executables + nfo only": ["Show/setup.exe", "Show/release.nfo"],
}
with tempfile.TemporaryDirectory() as td:
    for label, paths in HOSTILE.items():
        f = Path(td) / "t.torrent"
        f.write_bytes(_torrent(paths))
        check(label, acceptance_gate.metadata_is_safe(f), False)

    # A malformed / unreadable .torrent is refused, not admitted on doubt.
    f = Path(td) / "bad.torrent"
    f.write_bytes(b"this is not bencode")
    check("malformed torrent", acceptance_gate.metadata_is_safe(f), False)
    check("missing file", acceptance_gate.metadata_is_safe(Path(td) / "nope.torrent"), False)

# --- Part 2: it ACCEPTS ordinary media, executable helpers beside media, and every
# real .torrent on disk ---------------------------------------------------------
print("\nPart 2 -- ordinary media (with or without executable helpers) is accepted")
BENIGN = {
    "show pack":     ["Show/Season 01/Show - S01E01.mkv", "Show/Season 01/Show - S01E02.mkv"],
    "manga volumes": ["Manga/Manga v01.cbz", "Manga/Manga v02.cbz"],
    "movie + subs":  ["Movie (2001)/Movie.mkv", "Movie (2001)/Movie.srt"],
    "with nfo":      ["Show/S01E01.mkv", "Show/S01E01.nfo"],
    "media + windows exe":   ["Show/S01E01.mkv", "Show/setup.exe"],
    "media + shell script":  ["Show/S01E01.mkv", "Show/Remove-Dub.sh"],
    "media + batch switcher": ["Show/S01E01.mkv", "Show/Switch-to-Sub.bat"],
    "comic + javascript":    ["Comic/v01.cbz", "Comic/payload.js"],
    "movie + disk image":    ["Movie/Movie.mkv", "Movie/bonus.dmg"],
}
with tempfile.TemporaryDirectory() as td:
    for label, paths in BENIGN.items():
        f = Path(td) / "t.torrent"
        f.write_bytes(_torrent(paths))
        check(label, acceptance_gate.metadata_is_safe(f), True)

# Every real .torrent on an ADMISSION path must still be admissible. This is the half
# that proves the gate is not simply refusing everything. `failed/` is excluded on
# purpose -- see the module docstring.
real = []
for d in (config.TORRENTS_DIR, config.QUEUED_DIR, config.INGESTING_DIR,
          config.FINISHED_DIR, config.TORRENT_SOURCE_MIRROR):
    if d.exists():
        real.extend(sorted(d.glob("*.torrent")))
print(f"\n  {len(real)} real .torrent file(s) on the admission paths")
refused = [t for t in real if not acceptance_gate.metadata_is_safe(t)]
if refused:
    for t in refused:
        print(f"  FAIL real torrent refused: {t}")
    failures.append(f"{len(refused)} real .torrent(s) refused by the safety gate")
else:
    print("  ok   every real .torrent on the admission paths is accepted")

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED.")
