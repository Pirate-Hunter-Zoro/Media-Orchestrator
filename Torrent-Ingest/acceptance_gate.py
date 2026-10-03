"""The acceptance gate on ingest's admission paths — Torrent-Ingest's half of §4.120.

The fleet's authoritative "should I download this?" check lived in the searcher and took a
`.torrent`'s bytes, so it ran only where a real `.torrent` existed. When every `.torrent`
cache began serving truncated files, `write_magnet` became 100% of drops and the gate went
dark for four days while nothing reported it. The gate's input was never a `.torrent`
though — it is a FILE LIST — and this process holds one at exactly the right moment:
qBittorrent resolves a magnet's metadata (seconds, kilobytes) BEFORE any content is
downloaded, so a magnet whose every file is already owned can be refused having cost the
metadata fetch and nothing else.

**And the `.torrent` path is here too, since 2026-09-07.** Leaving it in the searcher put
the gate one traffic shift away from dark a second time, and the shift came: with the
searcher quarantined, every `.torrent` reaching the watch folder was admitted with nothing
judging it, and the gate read silent for 50 h while drops kept landing. A `.torrent`
carries its own file list (`file_names_from_torrent`), so that half runs before the add —
the cheapest point on either path. Ingest is where both belong: it is the one place every
admission passes through, whatever dropped it.

The judgment itself is NOT here. It lives in the searcher's `acceptance` module, which
both repos load, because the same question asked on two paths must not be answered by two
implementations that can drift apart. This file is the adapter: it resolves which SERIES a
magnet belongs to, hands the file list over, and records the heartbeat.

**Series identity is looked up, never guessed.** The gate needs to know which series a
torrent is for, and a release title is not a reliable route to one ("[Tenrai-Sensei]
Nisekoi Season 1-2 + OVAs" → which DB row?). The searcher already knows the answer at drop
time and records it in the shared `torrents` ledger against the infohash
(`_record_torrent`), so this reads it back. Measured over the live queue: every single
queued/downloading record has a ledger row. When one does not, that is an UNKNOWN — not a
guess, and not a refusal.

**Why the AI mapper is not used here.** `item_map.map_files` needs the searcher's `config`
and `discovery`, and this repo must never import either (both repos ship a `config.py`;
that shadowing crashed the ingest daemon at start once already, §4.102). So the
deterministic mapper runs instead. That is a real limitation and it is the SAFE direction:
the deterministic mapper reads only confident `SxxExx`/`Vol.N`/`Ch.N`, and anything it
cannot read becomes "other" — which produces an UNKNOWN verdict (admitted, counted, and
reported), never a refusal. The gate can therefore refuse LESS than the searcher's would,
and never more.
"""

import importlib.util as _ilu
import sys
from pathlib import Path

import config

# Load the searcher's stdlib-only modules by file path, WITHOUT inserting the searcher's
# directory onto sys.path. `sys.path.insert(0, searcher)` shadows THIS repo's `ingest`/
# `config` for every later import in the process, which is how `direct_ingest.py`'s
# `import ingest` once resolved to the searcher's and crashed the daemon at start
# (§4.102). Order matters: `acceptance` does a bare `import librarydb` / `import parse`
# at exec time, so both must be in sys.modules before it runs.
_BRAIN_DIR = config.PROJECT_ROOT / "librarybrain"
_STATE_DIR = config.STATE_DIR


def _load(name):
    """Path-load one stdlib-only searcher module, reusing an already-loaded copy.

    `identify` loads `librarydb` the same way, so whichever module imports first wins and
    the other reuses it -- executing librarydb twice would give the two copies separate
    module state for no reason.
    """
    mod = sys.modules.get(name)
    if mod is not None:
        return mod
    spec = _ilu.spec_from_file_location(name, str(_BRAIN_DIR / f"{name}.py"))
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod            # registered BEFORE exec, so cyclic imports resolve
    spec.loader.exec_module(mod)
    return mod


parse = _load("parse")
librarydb = _load("librarydb")
acceptance = _load("acceptance")

ACCEPT = acceptance.ACCEPT
REFUSE = acceptance.REFUSE
UNKNOWN = acceptance.UNKNOWN


def _unknown(reason):
    return acceptance.Verdict(UNKNOWN, reason)


def check(info_hash, names, release_title=""):
    """Run the acceptance gate over a magnet's resolved file list. Returns a `Verdict`.

    `names` are the torrent's file paths as qBittorrent reports them. Never raises: an
    acquisition gate that can throw on the admission path would stop the queue draining,
    so every failure to reach a verdict comes back as UNKNOWN with a reason.
    """
    if not names:
        return _unknown("no file list from qBittorrent")
    try:
        conn = librarydb.connect()
    except Exception as exc:                                 # noqa: BLE001
        return _unknown(f"library DB unavailable ({exc})")
    try:
        t = librarydb.torrent_by_hash(conn, info_hash)
        if not t:
            # The searcher records every drop it makes. A magnet with no ledger row is
            # something the searcher did not drop (a hand-placed magnet, or a drop from
            # before the ledger existed), so there is no authoritative series for it.
            return _unknown("not in the searcher's torrent ledger")
        s = librarydb.series_by_id(conn, t.get("series_id"))
        if not s:
            return _unknown(f"ledger row has no series row (series_id={t.get('series_id')})")
        return acceptance.evaluate(
            conn, s["id"], s["name"], s["kind"], list(names),
            release_title or t.get("title") or "",
            ai_mapper=None,                  # see the module docstring
            # A refusal here retires a queued torrent for good, so it has to clear a
            # higher bar than one on the drop path -- measuring the live queue found three
            # "everything is owned" verdicts that were true of the MAPPING and false of
            # the torrent, and every one of them would have thrown away real content.
            refusal_is_terminal=True)
    except Exception as exc:                                 # noqa: BLE001
        return _unknown(f"gate raised ({exc})")
    finally:
        try:
            conn.close()
        except Exception:                                    # noqa: BLE001
            pass


def record(decision):
    """Heartbeat: the gate REACHED a verdict on the magnet path.

    Written into THIS repo's state dir, next to the library DB, because "has the
    acceptance gate run lately?" is one question about one gate and must not need two
    places looked at. §4.120's fault was not a broken check, it was an unreached one that
    nothing could report; freshness here is what makes it reportable.

    (It lived in the searcher's state dir until 2026-09-10, when the searcher was removed
    and the heartbeat moved here with it.)"""
    acceptance.record_verdict(_STATE_DIR, "ingest", decision)


def file_names(client, info_hash):
    """A torrent's file paths from qBittorrent, or [] when metadata has not resolved."""
    import qbt
    return [f.name for f in qbt.files(client, info_hash) if getattr(f, "name", None)]


def liveness():
    """`acceptance.liveness` over the heartbeat dir — used by fleet health.

    The heartbeat used to live in the searcher's state dir because the searcher was the
    gate's other caller. The searcher is gone (2026-09-10), ingest is the ONLY admission
    path, and the heartbeat moved here with it. §4.22's rule is unchanged: it is cleared
    by real work or not at all.
    """
    return acceptance.liveness(_STATE_DIR)


# A `.torrent` whose METADATA is hostile -- a path-traversal or an absolute path -- must
# never be added. This gate lived ONLY in the searcher (`searcher.torrent_metadata_sane`),
# which ran it on every discovered drop. The searcher was removed on 2026-09-10 and
# hand-dropping is now the fleet's ONLY admission path, so deleting the searcher without
# porting this would have taken the check away from the one route that still admits
# anything -- the §4.22 failure shape exactly (a gate on one of several paths, one traffic
# shift from dark), with the traffic already shifted.
#
# It is a SAFETY refusal, not a relevance one, so it is judged before the acceptance gate
# and cannot be softened to UNKNOWN: "I could not tell whether this is hostile" is not a
# reason to add it. qBittorrent sanitises paths too; this refuses before it is ever asked.
#
# EXECUTABLES ARE NOT A REFUSAL ON THEIR OWN (owner instruction, 2026-10-03): "just
# because the torrent has such a file doesn't mean we need to execute it." The pipeline
# never runs a release's helper files -- plan-coverage files the media and ignores the
# rest -- so `Remove-Dub.bat` or a bundled `mkvmerge.exe` beside the episodes is clutter,
# not a hazard. What still refuses is a torrent whose ONLY files are executables: it has
# nothing the fleet wants to file, so admitting it downloads a payload for no reason. The
# owner cannot re-create those releases, so this list must not refuse real media packs.
_EXECUTABLE_EXTS = {
    ".exe", ".scr", ".bat", ".cmd", ".com", ".msi", ".dmg", ".app",
    ".apk", ".deb", ".rpm", ".js", ".vbs", ".ps1", ".sh", ".pif",
    ".jar", ".lnk", ".hta",
}


def metadata_is_safe(torrent_path):
    """True if a `.torrent`'s file list is safe to hand to qBittorrent.

    ALWAYS refused: a path traversal or an absolute path -- the two shapes that can write
    outside the download directory, whatever the file is. Executable files are tolerated
    when the torrent also carries media the fleet files (`config.DIRECT_INGEST_EXTENSIONS`
    -- video, comics, archives converted to comics, e-books); a pack of ONLY executables
    is refused. A torrent with no executables passes as it always did, so this change can
    only ADMIT more, never refuse more.

    A malformed or unreadable `.torrent` is False -- refusing an unreadable file costs the
    owner a re-drop, while admitting one costs whatever it contains.

    Reads the file list through `file_names_from_torrent`, so this and the acceptance gate
    judge the SAME bytes through the same decoder. Two readers of one format drift (§4.5),
    and the searcher's copy having its own bencode reader is what let them.
    """
    import os
    names = file_names_from_torrent(torrent_path)
    if not names:
        return False
    has_executable = False
    has_wanted = False
    for n in names:
        parts = n.replace("\\", "/").split("/")
        if parts and parts[0] == "":              # absolute path
            return False
        if ".." in parts:                          # traversal
            return False
        ext = os.path.splitext(n)[1].lower()
        if ext in _EXECUTABLE_EXTS:
            has_executable = True
        elif ext in config.DIRECT_INGEST_EXTENSIONS:
            has_wanted = True
    return has_wanted or not has_executable


def file_names_from_torrent(torrent_path):
    """A `.torrent`'s file paths, read from its own metadata. `[]` if unreadable.

    The magnet path has to ask qBittorrent for the file list because a magnet has none
    until the swarm answers. A `.torrent` carries its file list on disk, so the gate can
    run BEFORE the add rather than after it -- which is why this exists as a second way
    into the same judgment.

    Shape matches the searcher's `torrent_file_names` (paths RELATIVE to the torrent root,
    no root prefix), because that is what `acceptance.evaluate` has always been fed on the
    `.torrent` path and the mapper's rules were tuned against it. Padding files are kept:
    the mapper reads them as "other", and dropping them here would make this list disagree
    with the magnet path's for the same torrent.
    """
    try:
        data = Path(torrent_path).read_bytes()
    except OSError:
        return []
    if not data or data[:1] != b"d":
        return []
    import qbt                              # local: acceptance_gate loads before qbt's dep
    try:
        meta, _ = qbt._bdecode(data, 0)     # noqa: SLF001 -- the repo's only bencode reader
    except (ValueError, IndexError):
        return []
    info = meta.get(b"info") if isinstance(meta, dict) else None
    if not isinstance(info, dict):
        return []
    files = info.get(b"files")
    if not isinstance(files, list):                       # single-file torrent
        name = info.get(b"name")
        return [name.decode("utf-8", "replace")] if isinstance(name, bytes) else []
    names = []
    for f in files:
        if not isinstance(f, dict):
            continue
        p = f.get(b"path")
        if isinstance(p, list):
            names.append("/".join(x.decode("utf-8", "replace")
                                  for x in p if isinstance(x, bytes)))
    return names
