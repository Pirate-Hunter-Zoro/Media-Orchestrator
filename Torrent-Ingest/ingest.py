#!/usr/bin/env python3
"""Torrent-Ingest daemon.

Watches the iCloud Torrents folder and drives each `.torrent` (or `.magnet` link — the
searcher records a `.magnet` when a `.torrent` cache serves it truncated; with the
searcher gone, `_recover_truncated_torrent` synthesizes that magnet itself for a
hash-named drop whose bencode will not parse) through a disk-budgeted state machine.
Downloads run in parallel (qBittorrent fetches as
many as fit the local disk budget); each one, once complete, is processed
independently through:

    QUEUED -> DOWNLOADING -> DOWNLOADED -> IDENTIFIED -> STAGED -> VERIFIED -> COMPLETED

A finished torrent cascades all the way to COMPLETED in one pass — identify,
apply, verify, and cleanup — so its local copy is deleted the instant it is
proven safe in the library, not at some later cycle boundary. The moment that
space is freed we immediately admit whatever queued torrents now fit, so the
disk is kept full of work rather than draining a whole batch before starting the
next one.

The irreversible step (deleting the local download) runs ONLY after the renamed
files are proven present in the library and the VERIFIED state is journaled; the
source `.torrent` is then filed into a `finished/` folder rather than deleted, so
a completed torrent can always be re-dropped to redownload. A crash resumes from
the journal; nothing pre-existing in the library is ever touched.

A destination that already exists is not a failure: the pre-existing file is left
untouched (never overwritten) and its local copy is dropped, so a torrent whose
media is already in the library completes cleanly and is filed under finished/.
"""

import argparse
import fcntl
import json
import os
import re
import shutil
import sys
import time
import unicodedata
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

# This repo's own directory goes FIRST on sys.path. Torrent-Ingest and Torrent-Searcher
# both ship modules named `config.py`, `library.py` and `ingest.py`, and both repos are on
# `sys.path` in some processes -- so a bare `import config` resolves to whichever repo the
# launcher happened to put first. That is how `directingest` died at import on 2026-08-27,
# reading Torrent-Ingest's `config` through Torrent-Searcher's `ingest` (§5 item 4a). The
# pin makes the resolution a property of the FILE rather than of how it was launched.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import config
import identify
import journal
import library
import plan_coverage
import qbt
import reconcile

# The acceptance gate lives in Torrent-Searcher (both repos run the same judgment, §4.120)
# and is loaded across the repo boundary. That makes it the one import here that depends on
# ANOTHER repository's working tree, so it is loaded defensively: "a pull must never be
# able to stop a daemon starting" (§7, §4.109), and a searcher checkout that is behind, or
# a syntax error in a file this repo does not own, must not take ingest down with it. The
# gate then simply does not run -- which `fleet_health`'s liveness check reports as an
# ACTION, so a silently ungated fleet is exactly what cannot happen.
try:
    import acceptance_gate
except Exception as _gate_exc:                                        # noqa: BLE001
    acceptance_gate = None
    _GATE_IMPORT_ERROR = _gate_exc
else:
    _GATE_IMPORT_ERROR = None


# --- logging -----------------------------------------------------------------

def log(msg):
    line = f"[{config.log_stamp()}] {msg}"
    print(line, flush=True)
    try:
        config.rotate_log_if_large(config.LOG_FILE)
        with config.LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# --- single-instance lock ----------------------------------------------------

def acquire_lock():
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    fh = config.LOCK_FILE.open("w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("Another instance holds the lock; exiting.")
        sys.exit(0)
    fh.write(str(os.getpid()))
    fh.flush()
    return fh  # keep handle alive for process lifetime


# --- preconditions -----------------------------------------------------------

def library_ready():
    return config.MEDIA_ROOT.exists() and config.SHOWS_ROOT.exists()


def tailscale_up():
    """True if a Tailscale CGNAT address (100.64.0.0/10) is bound to an interface.

    We deliberately do NOT shell out to `tailscale status`: this machine runs the
    Tailscale *Mac app* (IPNExtension), whose socket the Homebrew CLI can't reach
    under launchd's stripped environment (it works only in a login shell). The
    interface-address check is env-independent and tests the exact condition that
    matters — qBittorrent is bound to that 100.x address, so if the address is
    gone, no traffic can leak, and if it's present the VPN path is live.
    """
    import re
    import subprocess
    try:
        r = subprocess.run(["/sbin/ifconfig"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    for m in re.finditer(r"inet (100\.\d+\.\d+\.\d+)", r.stdout):
        second_octet = int(m.group(1).split(".")[1])
        if 64 <= second_octet <= 127:          # 100.64.0.0/10 CGNAT = Tailscale
            return True
    return False


def free_bytes(path):
    return shutil.disk_usage(path).free


def disk_total(path):
    return shutil.disk_usage(path).total


# --- iCloud materialization --------------------------------------------------

def find_drop_files():
    """All new drops in the watch folder's top level: `.torrent` sources AND `.magnet`
    links (the searcher writes a `.magnet` when a `.torrent` cache serves it truncated).
    Dataless iCloud placeholders (`.Name.torrent.icloud`) are resolved back to their real
    path.

    A `.torrent` whose name begins with a "." is still a torrent: some release
    titles (e.g. `.Planetes.2003.TV...`) legitimately start with a dot, and hiding
    it behind the leading-dot convention must not strand it here. Only the `.icloud`
    placeholder dot is stripped, in the branch below."""
    out = set()
    if not config.TORRENTS_DIR.exists():
        return []
    for p in config.TORRENTS_DIR.iterdir():
        if p.name.startswith(".") and p.name.endswith(".icloud"):
            real = p.with_name(p.name[1:-len(".icloud")])
            if real.suffix in (".torrent", ".magnet"):
                out.add(real)
        elif p.suffix in (".torrent", ".magnet"):
            out.add(p)
    return sorted(out)


def materialize(path, wait_sec=60):
    """Force a dataless iCloud file to download locally, then wait for it."""
    import subprocess
    subprocess.run([config.BRCTL_BIN, "download", str(path)],
                   check=False, capture_output=True)
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        if path.exists() and path.stat().st_size > 0:
            return True
        time.sleep(1)
    return path.exists() and path.stat().st_size > 0


def _mirror_source(path, info_hash):
    """Keep a LOCAL copy of a registered `.torrent`'s bytes, keyed by infohash.

    The watch folder is iCloud-backed, and iCloud evicts a `.torrent` that has sat in
    `ingesting/` for days (a parked chunked pack) to a dataless placeholder that `brctl
    download` cannot always bring back — which strands the pack as "source .torrent is
    missing" (§ diagnosis 4.4). This mirror lives on the local SSD, so a chunked pack can
    always resume from it. The iCloud copy stays the authority for the re-drop/retry signal;
    this is only a read-back fallback. Best-effort: a mirror miss must never kill a drop.
    """
    try:
        config.TORRENT_SOURCE_MIRROR.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, config.TORRENT_SOURCE_MIRROR / f"{info_hash}.torrent")
    except OSError as exc:
        log(f"Warning: could not mirror source .torrent for {info_hash[:12]}: {exc}")


def _ensure_source(record) -> Path | None:
    """Return a readable source `.torrent` path for `record`, materializing the iCloud copy
    and falling back to the local mirror. Updates `record["torrent_path"]` when the mirror
    restores the file. Returns None only when neither copy is recoverable."""
    h = record["info_hash"]
    path = Path(record.get("torrent_path") or "")
    if not path.exists():
        # A missing iCloud file is usually an evicted-to-cloud placeholder, not a real
        # deletion; force it back down first.
        materialize(path)
    if not path.exists():
        mirror = config.TORRENT_SOURCE_MIRROR / f"{h}.torrent"
        if mirror.exists():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(mirror, path)
                record["torrent_path"] = str(path)
                journal.write_record(record)
                log(f"Restored {record['name']} source .torrent from the local mirror")
            except OSError as exc:
                log(f"Could not restore {record['name']} source .torrent from mirror: {exc}")
    return path if path.exists() else None


# --- registration ------------------------------------------------------------

def _carry_chunk_progress(old, fresh):
    """Carry a chunked torrent's per-file progress onto its re-queued record. Returns how
    many files were carried.

    A re-drop otherwise restarts a torrent from scratch, which is the right answer for a
    whole-torrent ingest: it re-downloads, and apply_plan skips whatever is already in the
    library. A chunked pack cannot afford it. Re-fetching hundreds of GB it has already
    proven into the library, one disk-bounded wave at a time, costs days and one identify
    run per wave to conclude "already present" — and every one of those waves is a window
    in which the pack can be interrupted again.

    File indices are stable for an info hash, so a `chunk_done` index means the same file
    on the new record as it did on the old one. `chunk_intent` then tells admission this
    pack is already known not to fit, so it chunks immediately instead of re-serving the
    2 h deferral it has already served.

    WHAT IS CARRIED IS WHAT CAN BE PROVEN, NOT WHAT WAS REMEMBERED.
      `chunk_done` is a claim about the PAST -- §4.10's lesson one layer down -- and the
      commonest reason to re-drop a pack by hand is that its content is GONE: purged, or
      never filed in the first place. Carrying the claim then turns the owner's
      re-acquisition into a silent no-op that reports COMPLETED, which is what happened to
      Monogatari Series (2009), Higurashi, Bakugan and Log Horizon.

      So an index is carried only when the record can name where it landed AND that
      destination is still in the library (`chunk_filed`, checked on the mount), or when
      the pack deliberately declined the file as junk (`chunk_dropped` -- re-fetching a
      creditless opening to decline it again buys nothing). Everything else is re-fetched,
      including every legacy record written before this bookkeeping existed, which can
      prove nothing and so carries nothing.

      The asymmetry is deliberate. Carrying wrongly loses content silently and tells the
      owner it worked; re-fetching wrongly costs bandwidth, is visible in the log, and
      apply_plan drops what is already present anyway (§ Already-present media is a
      success). Only one of those is reversible.
    """
    if not old.get("chunked"):
        return 0
    fresh["chunk_intent"] = True
    failed_idx = set(old.get("chunk_failed_idx") or [])
    claimed = set(old.get("chunk_done") or []) - failed_idx
    dropped = set(old.get("chunk_dropped") or []) - failed_idx

    proven, gone, mount_down = set(), set(), False
    for key, rel in (old.get("chunk_filed") or {}).items():
        try:
            i = int(key)
        except (TypeError, ValueError):
            continue
        if i in failed_idx or not rel:
            continue
        present, mount_alive = _still_in_library(rel)
        if not mount_alive:
            mount_down = True
        (proven if present else gone).add(i)

    carried = (proven | dropped) & claimed
    unproven = claimed - carried
    if carried:
        fresh["chunk_done"] = sorted(carried)
        fresh["chunk_filed"] = {k: v for k, v in (old.get("chunk_filed") or {}).items()
                                if _as_int(k) in carried}
        fresh["chunk_dropped"] = sorted(dropped & carried)
    if unproven:
        # Never a silent decision: this is the difference between a re-drop that re-fetches
        # 70 GB and one that no-ops, and the operator must be able to see which they got.
        why = (f"{len(gone)} no longer in the library"
               if gone else "no per-file record of what it filed")
        log(f"  {old.get('name')}: carrying {len(carried)} proven file(s); "
            f"{len(unproven)} will be RE-FETCHED ({why})"
            + (" -- NOTE: the mediafs mount did not answer, so presence could not be "
               "confirmed and the pack re-fetches rather than assume" if mount_down else ""))
    return len(carried)


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _read_magnet(path):
    """Parse a `.magnet` file into `(info_hash, name, magnet_uri)`.

    The infohash is read from the magnet URI itself (`urn:btih:<40-hex>`), not the
    (12-char) filename. `name` is the URI's `dn=` parameter, url-decoded."""
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text.startswith("magnet:?"):
        raise ValueError("not a magnet URI")
    ih = qbt.info_hash_from_magnet(text)
    name = None
    m = re.search(r"[?&]dn=([^&]+)", text)
    if m:
        name = urllib.parse.unquote(m.group(1))
    return ih, name, text


def _register_magnet(path, records):
    """Register a freshly-dropped `.magnet` link as a QUEUED record.

    A magnet has no `.torrent` metadata, so the record stores the magnet URI itself and a
    `total_size` of None — the real size is read from qBittorrent once it fetches metadata
    from the swarm (see `_admit_magnet`). Everything downstream of admission is identical to
    a `.torrent` drop: same info hash, same state machine."""
    if not materialize(path):
        log(f"Could not materialize {path.name}; will retry next cycle.")
        return
    try:
        h, name, magnet_uri = _read_magnet(path)
    except Exception as exc:                                             # noqa: BLE001
        _file_unparseable_torrent(path, exc)
        return
    if h in records:
        rec = records[h]
        if rec.get("status") in (journal.COMPLETED, journal.FAILED, journal.REFUSED):
            # A terminal .magnet is back at the TOP of the watch folder: honor the re-drop
            # (the same retry signal a .torrent uses) by starting it over from scratch.
            fresh = journal.new_record(h, path, name)
            fresh["magnet"] = magnet_uri
            fresh["total_size"] = None
            _file_torrent(fresh, config.QUEUED_DIR, "queued")
            records[h] = journal.write_record(fresh)
            prior = {journal.FAILED: "failed", journal.REFUSED: "refused"}.get(
                rec.get("status"), "finished")
            log(f"Re-queuing a {prior} magnet dropped again: {name} ({h[:12]})")
            return
        dest = (config.INGESTING_DIR if rec.get("status") in ACTIVE
                else config.QUEUED_DIR)
        dest_label = "ingesting" if dest == config.INGESTING_DIR else "queued"
        _file_torrent(rec, dest, dest_label)
        # A SECOND source for a hash already registered -- iCloud surfaces "X.magnet" and
        # "X 2.magnet" for one drop, and a re-dropped recovery magnet lands beside the
        # copy already filed. It is byte-identical to the source we track, so filing it
        # into queued/ just makes an orphan nothing scans; it goes to finished/ instead.
        if Path(path).exists() and str(path) != str(rec.get("torrent_path") or ""):
            _file_duplicate_source(path, records, h)
        if not rec.get("magnet"):
            rec["magnet"] = magnet_uri
            journal.write_record(rec)
        return
    rec = journal.new_record(h, path, name)
    rec["magnet"] = magnet_uri
    rec["total_size"] = None            # unknown until qBittorrent resolves metadata
    _file_torrent(rec, config.QUEUED_DIR, "queued")
    records[h] = journal.write_record(rec)
    log(f"Registered new magnet: {name} ({h[:12]})")


# A `.torrent` named with its 40-hex info hash -- the convention every drop on this
# machine uses, so the filename IS the hash when the bencode will not parse.
_INFO_HASH_NAME_RE = re.compile(r"\A[0-9a-fA-F]{40}\Z")


def _magnet_uri_from_truncated(path, info_hash):
    """The magnet a truncated hash-named drop stands for: the filename supplies the
    info hash, the partial bencode whatever display name and trackers survived."""
    name, trackers = qbt.salvage_from_truncated_file(path)
    parts = [f"magnet:?xt=urn:btih:{info_hash}"]
    if name:
        parts.append("dn=" + urllib.parse.quote(name))
    for url in trackers:
        parts.append("tr=" + urllib.parse.quote(url, safe=""))
    return "&".join(parts)


def _discard_dead_torrent(path, why) -> bool:
    """Remove a truncated `.torrent` whose bytes are dead and fully accounted for.

    Its hash, name and trackers now live on the magnet record, so the bytes are one
    thing only: a file in `failed/` that reads as "this failed" on a phone while the
    torrent is in fact queued and downloading. Returns True once it is gone; False if it
    could not be removed, which tells the caller to fall back to filing it under failed/
    so it at least cannot linger at the top of the watch folder.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log(f"Could not remove dead .torrent {path.name}: {exc}")
        return False
    log(f"Removed dead truncated .torrent {path.name} ({why}).")
    return True


def _recover_truncated_torrent(path, records) -> bool:
    """Recover a truncated `.torrent` as a magnet, instead of condemning it.

    A `.torrent` cache can serve its file cut short; the searcher used to notice that and
    write a sibling `.magnet`, but the searcher was deleted on 2026-09-10, so a truncated
    hand-drop now loops through failed/ forever with advice -- "replace the file" -- the
    owner has no way to follow. Every drop here is named with its 40-hex info hash, and a
    hash is all a magnet needs: qBittorrent fetches the real metadata from the swarm and
    the normal pipeline takes over. The partial bytes also still carry the display name
    and the announce-list, so both ride along on the synthesized URI.

    A drop is only recovered once it is past `UNPARSEABLE_GRACE_SEC` untouched -- the
    same rule as `_file_unparseable_torrent`, so a torrent iCloud is still writing is
    left to the next cycle rather than converted from an incomplete read.

    Two outcomes, both of which REMOVE the dead bytes so they cannot keep reappearing in
    a queue that reads as failed:

      * the hash already has a live record -- the owner re-dropped the dead file, or
        iCloud surfaced a second copy. Nothing to recover; discard the duplicate.
      * otherwise, register the recovered magnet. The record is live either way, so a
        non-terminal duplicate resolves to the first branch on its next appearance.

    Returns True when the drop was handled (the caller must not also file it under
    failed/), False when there is nothing to recover from or the bytes could not be
    removed.
    """
    if not _INFO_HASH_NAME_RE.match(path.stem):
        return False
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False                                     # vanished mid-scan
    if age < config.UNPARSEABLE_GRACE_SEC:
        return False                                     # still syncing; retry next cycle
    info_hash = path.stem.lower()
    rec = records.get(info_hash)
    if rec is not None and rec.get("status") not in (journal.COMPLETED, journal.FAILED,
                                                     journal.REFUSED):
        return _discard_dead_torrent(
            path, f"{rec.get('name') or info_hash[:12]} is already {rec.get('status')} "
                  f"in the journal, so re-dropping the dead bytes changes nothing")
    magnet_path = path.with_suffix(".magnet")
    try:
        magnet_path.write_text(_magnet_uri_from_truncated(path, info_hash),
                               encoding="utf-8")
    except OSError as exc:
        log(f"Could not write the recovery magnet for truncated {path.name}: {exc}")
        return False
    _register_magnet(magnet_path, records)
    log(f"Recovered truncated .torrent {path.name} as a magnet ({info_hash[:12]}); "
        f"qBittorrent will fetch its metadata from the swarm.")
    return _discard_dead_torrent(path, "superseded by the recovered magnet")


def _file_duplicate_source(path, records, info_hash):
    """File a second copy of an already-tracked drop out of the watch subfolders.

    iCloud surfaces `X.torrent` and `X 2.torrent` for one drop, and a second source
    can land beside a recovered one. It is byte-identical to the source the record
    tracks, so it goes to finished/ (or failed/, for a record already there): visible,
    out of the pipeline, and re-droppable by hand. The old code filed it into queued/,
    where `find_drop_files` never looks -- which is how two One Piece files sat in
    queued/ for a month (HANDOFF 10.6). Never deleted.
    """
    dest = (config.FAILED_DIR
            if (records.get(info_hash) or {}).get("status") in (journal.FAILED,
                                                                journal.REFUSED)
            else config.FINISHED_DIR)
    label = "failed" if dest == config.FAILED_DIR else "finished"
    if _file_torrent({"torrent_path": str(path)}, dest, label):
        log(f"Filed duplicate source {path.name} under {label}/ "
            f"(already tracked as {info_hash[:12]}).")


def _refuse_undownloadable(record, path, records) -> bool:
    """Fail a fresh drop whose `.torrent` can never reach a peer, before it is queued.

    A private, trackerless drop has every discovery channel switched off (DHT, PeX and
    LSD are forbidden by the private flag; qBittorrent reports all three as "This torrent
    is private"), so nothing can ever connect. Left to the download path, the only
    symptom is the 24h stall clock expiring on a torrent no re-drop can heal -- measured
    on a SpongeBob S16 pack, 2026-09-24. Failing it here turns that day into one log line
    and one file under failed/, where the owner can see the reason.

    Returns True when the drop was failed; the caller must not queue it.
    """
    reason = qbt.undownloadable_reason(path)
    if not reason:
        return False
    records[record["info_hash"]] = record
    _fail(record, reason)
    return True


def _source_hash(path):
    """The info hash a `.torrent`/`.magnet` in a state folder stands for, or None.

    None means "cannot tell" (a truncated or still-syncing drop), and every caller
    treats it as leave-it-alone: the sweep must never guess about bytes it cannot
    identify.
    """
    try:
        if path.suffix.lower() == ".magnet":
            h, _name, _uri = _read_magnet(path)
            return h
        return qbt.info_hash_from_file(path)
    except Exception:                                                     # noqa: BLE001
        return None


def sweep_orphan_sources(records):
    """File every terminal or duplicate source out of queued/ and ingesting/.

    `find_drop_files` reads only the watch root's TOP LEVEL, so a `.torrent` that
    ends up in `queued/` is never looked at again by anything. The tracked source
    belongs there while the record is live, but a terminal record's file (a purge
    left the record completed and the source behind) or an iCloud `" 2"` duplicate
    has no reason to stay and no path out -- two One Piece files sat there forever
    (HANDOFF 10.6). This sweep resolves each source's info hash and:
      * a TERMINAL record -> finished/ (completed) or failed/ (failed/refused);
      * a live record whose recorded source still exists -> finished/ as a duplicate;
      * a live record whose recorded source is GONE -> adopt this copy as the source
        (it is the only survivor, and without this a chunked pack could not resume);
      * no record at all -> back to the watch root, where registration picks it up.
    Never deletes, never touches a source it cannot parse.
    """
    moved = 0
    for folder, label in ((config.QUEUED_DIR, "queued"),
                          (config.INGESTING_DIR, "ingesting")):
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            if path.suffix.lower() not in (".torrent", ".magnet"):
                continue
            h = _source_hash(path)
            if not h:
                continue
            rec = records.get(h)
            if rec is None:
                try:
                    os.replace(path, config.TORRENTS_DIR / path.name)
                    log(f"Orphaned source {path.name} in {label}/ has no journal record; "
                        f"returned to the watch root for registration.")
                except OSError as exc:
                    log(f"Could not return orphaned {path.name} to the watch root: {exc}")
                continue
            status = rec.get("status")
            if status in journal.TERMINAL:
                dest = (config.FAILED_DIR if status in (journal.FAILED, journal.REFUSED)
                        else config.FINISHED_DIR)
                if _file_torrent({"torrent_path": str(path)}, dest,
                                 "failed" if dest == config.FAILED_DIR else "finished"):
                    log(f"Filed terminal source {path.name} out of {label}/ "
                        f"(record is {status}).")
                    moved += 1
                continue
            recorded = Path(rec.get("torrent_path") or "")
            try:
                live = recorded.exists()
            except OSError:
                live = False
            if live and _path_key(recorded) != _path_key(path):
                if _file_torrent({"torrent_path": str(path)}, config.FINISHED_DIR,
                                 "finished"):
                    log(f"Filed duplicate source {path.name} out of {label}/ "
                        f"(already tracked as {h[:12]}).")
                    moved += 1
            elif not live:
                rec["torrent_path"] = str(path)
                journal.write_record(rec)
                log(f"Adopted {path.name} in {label}/ as the live source for {h[:12]} "
                    f"(the recorded copy is gone).")
    if moved:
        log(f"Swept {moved} orphaned source(s) from queued//ingesting/.")
    return moved


def register_new_torrents(records):
    for path in find_drop_files():
        if path.suffix.lower() == ".magnet":
            _register_magnet(path, records)
            continue
        if not materialize(path):
            log(f"Could not materialize {path.name}; will retry next cycle.")
            continue
        try:
            h = qbt.info_hash_from_file(path)
        except Exception as exc:                                          # noqa: BLE001
            if not _recover_truncated_torrent(path, records):
                _file_unparseable_torrent(path, exc)
            continue
        _mirror_source(path, h)
        if h in records:
            rec = records[h]
            if rec.get("status") in (journal.COMPLETED, journal.FAILED, journal.REFUSED):
                # A terminal .torrent is back at the TOP of the watch folder.
                #  - COMPLETED: a deliberate re-drop to redownload, or a
                #    finished-move that didn't land.
                #  - FAILED: the source normally lives in failed/ (never scanned),
                #    so seeing it up here means it was deliberately moved back to
                #    retry it. FAILED is otherwise not auto-retried; this move IS
                #    the retry signal, so we honor it instead of requiring the
                #    journal record to be hand-cleared.
                # Either way a terminal torrent is NOT retired forever: start it
                # over from scratch rather than skipping it forever.
                name = qbt.torrent_name_from_file(path) or path.stem
                fresh = journal.new_record(h, path, name)
                fresh["total_size"] = qbt.total_size_from_file(path)
                if _refuse_undownloadable(fresh, path, records):
                    continue
                carried = _carry_chunk_progress(rec, fresh)
                _file_torrent(fresh, config.QUEUED_DIR, "queued")
                records[h] = journal.write_record(fresh)
                prior = {journal.FAILED: "failed", journal.REFUSED: "refused"}.get(
                rec.get("status"), "finished")
                log(f"Re-queuing a {prior} torrent dropped again: {name} ({h[:12]})"
                    + (f", resuming chunked waves at {carried} file(s) already filed."
                       if carried else "."))
                continue
            # Queued or in-flight: file the base `.torrent` into the subfolder its
            # status calls for (queued/ or ingesting/), so the watch folder's top
            # level holds only the human documents. This is also the one-time
            # migration of any pre-refactor backlog still sitting at the top.
            dest = (config.INGESTING_DIR if rec.get("status") in ACTIVE
                    else config.QUEUED_DIR)
            dest_label = "ingesting" if dest == config.INGESTING_DIR else "queued"
            recorded = rec.get("torrent_path")
            # Adopt the path we actually see only if the recorded one has gone away.
            # While it still exists, ignore the duplicate (iCloud surfaces "X.torrent"
            # and "X 2.torrent" for one hash); filing the extra copy keeps the top
            # level clean without flapping the record.
            if recorded != str(path) and not (recorded and Path(recorded).exists()):
                rec["torrent_path"] = str(path)
            moved = _file_torrent(rec, dest, dest_label)
            if not moved and Path(path).exists() and str(path) != rec.get("torrent_path"):
                # A second iCloud copy of a drop already tracked ("X 2.torrent").
                # Filing it into queued/ creates an orphan nothing scans; it goes to
                # finished/ as the duplicate it is (HANDOFF 10.6).
                _file_duplicate_source(path, records, h)
            if moved or recorded != rec.get("torrent_path"):
                journal.write_record(rec)
            continue
        name = qbt.torrent_name_from_file(path) or path.stem
        rec = journal.new_record(h, path, name)
        rec["total_size"] = qbt.total_size_from_file(path)   # bytes, or None if unreadable
        if _refuse_undownloadable(rec, path, records):
            continue
        _file_torrent(rec, config.QUEUED_DIR, "queued")
        records[h] = journal.write_record(rec)
        sz = rec["total_size"]
        log(f"Registered new torrent: {name} ({h[:12]}) "
            f"[{(sz >> 20) if sz else '?'}MB]")


_last_register_ts = 0.0


def _register_periodically(records):
    """Register freshly-dropped .torrent files, throttled to REGISTER_REFRESH_SEC.

    The chunked wave path spends minutes inside a single advance() -- one AI identify per
    file -- and that is exactly when the searcher is most likely to drop a new release.
    Registration has to keep running through that, or a fresh drop sits at the top of the
    watch folder looking stuck until the whole wave drains. No-op between intervals (the
    same throttle the cycle() loop already applies), so it is cheap to call at the top of
    any slow loop.
    """
    global _last_register_ts
    now = time.time()
    if now - _last_register_ts < config.REGISTER_REFRESH_SEC:
        return
    _last_register_ts = now
    register_new_torrents(records)


# --- parallel, disk-bounded scheduling --------------------------------------
#
# No single-torrent serialization: we start as many torrents at once as fit the
# local disk budget, so a slow torrent never blocks the others. qBittorrent
# downloads them in parallel (and further caps concurrent transfers via its own
# MaxActiveDownloads); each finishes and is processed independently.

ACTIVE = {journal.DOWNLOADING, journal.DOWNLOADED,
          journal.IDENTIFIED, journal.STAGED, journal.VERIFIED}


def _size_of(record):
    return record.get("total_size") or 0


def _download_root(record):
    """Where this torrent's payload lives. All torrents download straight onto the
    library drive now (INCOMING_DIR is on the SSD); records may still carry an
    explicit download_root from the old SSD/overflow split, which is honored."""
    return Path(record.get("download_root") or config.INCOMING_DIR)


# States in which a torrent is not fetching bytes at all, so its unfetched remainder must
# not be reserved against the download budget. "stalledDL"/"queuedDL" are deliberately NOT
# here: those are live downloads that still intend to fetch and may recover at any moment,
# and un-reserving them would let the disk be over-committed the instant they do.
_NO_PROGRESS_BUDGET_STATES = ("stoppedDL", "pausedDL", "error", "missingFiles")


def _remaining_budget(records, client, tmap=None):
    """Bytes we may still commit to new downloads on the MAC SSD (the Downloads
    volume, where INCOMING_DIR now lives) without breaching MIN_FREE. Torrents
    download onto the SSD -- never the SSD library root -- so the budget is the SSD's free space
    minus the floor, minus what every in-flight download still has to fetch
    (total_size - completed, live from qBittorrent). What's left is admittable.

    `tmap` is a {info_hash: torrent} snapshot (qbt.torrent_map) so this can run without
    one HTTP request per in-flight torrent; when omitted it falls back to per-hash
    `qbt.get` (still correct, just slower -- callers that run this in a loop pass the map).
    """
    if tmap is None:
        tmap = qbt.torrent_map(client)
    budget = free_bytes(config.DOWNLOADS_DIR) - config.MIN_FREE_BYTES
    for r in records.values():
        if r["status"] != journal.DOWNLOADING:
            continue
        t = tmap.get(r["info_hash"])
        if t is None:
            continue                       # completed & auto-removed; ~0 remaining
        if r.get("chunked"):
            budget -= _chunk_outstanding(r, client)
            continue
        # A torrent qBittorrent reports as stopped/paused/errored is fetching nothing, so
        # reserving its unfetched bytes is exactly as wrong as reserving a whole chunked
        # pack was: the reservation is against bytes that are not coming. `_abandon_stalled`
        # retires such a torrent for good, but only after its stall grace; excluding it here
        # releases the budget on the very next cycle, which is what stops one dead 42 GB
        # magnet from pinning the budget negative while 75 GB sits free on the disk.
        if getattr(t, "state", "") in _NO_PROGRESS_BUDGET_STATES:
            continue
        completed = getattr(t, "completed", 0) or 0
        budget -= max(0, _size_of(r) - completed)
    return budget


def _chunk_outstanding(record, client):
    """Bytes a chunked torrent's ACTIVE WAVE still has to fetch.

    A chunked torrent is the one case where `total_size - completed` is the wrong
    reservation: every file outside the active wave is parked at priority 0 and will not
    be written until a later wave admits it, so reserving the whole pack would hold the
    entire torrent's size against a disk that only ever receives one wave of it. An 80 GB
    pack would reserve 80 GB on a disk with 70 GB admittable, driving the budget negative
    and blocking every other torrent for as long as the chunked one runs. Only the active
    wave's unfetched bytes are actually coming.
    """
    active = set(record.get("chunk_active") or [])
    if not active:
        return 0                           # parked between waves; nothing is downloading
    outstanding = 0
    for f in qbt.files(client, record["info_hash"]):
        if f.index in active:
            size = int(f.size or 0)
            outstanding += max(0, size - int(size * (f.progress or 0)))
    return outstanding


_DEFER_LOGGED: dict[str, float] = {}
_DEFER_LOG_INTERVAL_SEC = 3600


def _log_deferred(record, need, budget):
    """Say out loud that a torrent is parked for want of disk.

    "Doesn't fit right now, stays QUEUED" is only benign if the disk actually drains -- and
    nothing on this box guarantees that: the pre-downloader fills the same SSD with predicted
    content, so a torrent bigger than the leftover headroom waits forever with no log line
    anywhere explaining why. (The Eminence in Shadow, 23 GB, sat queued from 2026-08-03 with the
    SSD pinned at ~31 GB free -- needing 48 GB -- and the only visible symptom was "the anime
    never downloaded".) Throttled to hourly per torrent so a long wait does not flood the log.
    """
    h = record["info_hash"]
    now = time.time()
    if now - _DEFER_LOGGED.get(h, 0) < _DEFER_LOG_INTERVAL_SEC:
        return
    _DEFER_LOGGED[h] = now
    free = free_bytes(config.DOWNLOADS_DIR)
    log(f"DEFERRED {record['name']} ({need >> 30}GB needed incl. safety factor, "
        f"{max(0, budget) >> 30}GB admittable, {free >> 30}GB free on the SSD, "
        f"{config.MIN_FREE_BYTES >> 30}GB floor): waiting for space. It will start when the SSD "
        f"drains -- if it never does, something else is holding the disk.")


def _deferred_for(record):
    """Seconds this QUEUED torrent has been waiting for disk it never got.

    Derived from `created_at` rather than a stamp of its own: a record that is still
    QUEUED has, by definition, never been admitted, so registration time *is* the start
    of the wait. That also makes the deadline survive a daemon restart for free -- a
    deadline that resets on every restart is a deadline that never arrives.
    """
    try:
        created = datetime.fromisoformat(record.get("created_at") or "")
    except ValueError:
        return 0.0
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - created).total_seconds())


def admit_downloads(records, client, tmap=None):
    """Start every QUEUED torrent that fits the Mac SSD's remaining budget.

    Torrents download onto the SSD (INCOMING_DIR, on the Downloads volume) -- NEVER
    the SSD library root -- so a torrent's heavy random I/O can't starve the directory reads mediafs
    serves and wedge the mount. apply_plan then copies the finished files cross-device
    onto the SSD. A single budget bounds admission: SSD free minus the floor, minus what
    in-flight downloads still owe. A torrent bigger than the SSD's whole usable capacity
    can never fit and is REFUSED (never spilled outside the SSD); one that merely doesn't
    fit right now stays QUEUED and starts as the SSD drains (greedy fill -- a giant
    torrent never starves smaller ones behind it).
    """
    if tmap is None:
        tmap = qbt.torrent_map(client)
    budget = _remaining_budget(records, client, tmap)
    # Admit smallest-first, not drop-order: a single episode or movie completes in
    # minutes and frees its space straight back into the budget, which keeps the disk
    # full of FAST work. A giant pack (the chunked lane) is inherently slow -- one AI
    # identify per file, one disk-bounded wave at a time -- so it goes last and only
    # claims budget when nothing quicker is waiting.
    #
    # But KNOWN-SIZE records are decided FIRST, whatever their size. Deciding one costs
    # nothing -- the size is on the record -- while an unknown-size magnet costs a blocking
    # MAGNET_METADATA_WAIT_SEC (90 s) wait for the swarm. Sorting purely by size put every
    # unknown magnet (size 0) at the front, so a queue holding a few dead ones spent the
    # whole pass waiting on them and never reached the records at the back. Observed: with
    # ~37 queued the pass advanced one magnet per 90 s, and That '70s Show -- sorted last
    # because it is the largest -- sat past its 2 h chunk deadline without the switch ever
    # being evaluated. Free decisions before expensive ones.
    all_queued = [r for r in records.values() if r["status"] == journal.QUEUED]
    known = sorted((r for r in all_queued if _size_of(r)),
                   key=lambda r: (_size_of(r), r.get("created_at", "")))
    # LEAST-RECENTLY-ATTEMPTED first, not oldest-created first. Sorting by creation date
    # is a fixed HEAD, not a rotation: the same four magnets were retried every cycle until
    # each hit its 12h abandon, and everything behind them waited. Measured on the live
    # queue: of the last 200 metadata attempts only TEN distinct magnets were tried, and
    # four ElfQuest ones accounted for 26 of the last 40 -- while 114 sat queued. The log
    # line below claimed "the rest next cycle, so they cannot starve the queue"; they were
    # starving. A never-attempted magnet sorts first (stamp 0), so every magnet gets a turn
    # before any gets a second.
    unknown = sorted((r for r in all_queued if not _size_of(r)),
                     key=lambda r: (r.get("metadata_attempt_at") or 0,
                                    r.get("created_at", "")))
    # And cap how many metadata waits one pass will pay for, so a backlog of dead magnets
    # cannot starve the next pass either.
    queued = known + unknown[:config.MAX_MAGNET_METADATA_WAITS_PER_CYCLE]
    if len(unknown) > config.MAX_MAGNET_METADATA_WAITS_PER_CYCLE:
        log(f"{len(unknown)} magnet(s) awaiting metadata; resolving "
            f"{config.MAX_MAGNET_METADATA_WAITS_PER_CYCLE} this cycle "
            f"(least-recently-attempted first, so every magnet gets a turn).")
    for record in queued:
        h = record["info_hash"]
        if record.get("magnet"):
            # A magnet's size is unknowable until qBittorrent has pulled its metadata from
            # the swarm, so the FIRST admission cannot be budget-checked up front. Once we
            # have learned the size it is persisted on the record, and from then on the
            # magnet goes through exactly the same fit/chunk decisions as a .torrent --
            # otherwise the budget is simply bypassed for every drop, which is what it was
            # doing: the searcher now drops magnets as the norm, so 231 GB of downloads got
            # admitted onto 49 GB of free disk.
            known = _size_of(record)
            if known:
                verdict = _fit_verdict(record, known, budget, client)
                if verdict is not None:
                    budget -= verdict
                    continue
            # Stamp the attempt BEFORE paying for it, so a magnet that hangs the wait
            # still goes to the back of the rotation instead of holding the head.
            record["metadata_attempt_at"] = time.time()
            record["metadata_attempts"] = int(record.get("metadata_attempts") or 0) + 1
            journal.write_record(record)
            admitted, used = _admit_magnet(record, client, tmap, budget)
            if admitted:
                budget -= used
            continue
        path = _ensure_source(record)
        if path is None:
            _fail(record, f"source .torrent vanished: {record.get('torrent_path')}")
            continue

        # THE ACCEPTANCE GATE, `.torrent` half (§4.120). Ahead of the fit check as well as
        # the add: refusing something the library already holds should not first spend
        # hours deferred against a disk budget it never needed.
        if config.ACCEPTANCE_GATE and acceptance_gate is not None \
                and not _torrent_acceptance_gate(record, h, path):
            continue

        size = _size_of(record)
        need = int(size * config.SPACE_SAFETY_FACTOR) if size else 0
        # A .torrent's size is known from its metadata, so the fit decision (chunk it,
        # defer it, or admit it) is made BEFORE anything is added. Shared with the magnet
        # path via `_fit_verdict` so the two cannot drift apart.
        if size or record.get("chunk_intent"):
            verdict = _fit_verdict(record, size, budget, client)
            if verdict is not None:
                budget -= verdict
                continue

        save_root = config.INCOMING_DIR
        try:
            save_root.mkdir(parents=True, exist_ok=True)
            qbt.add(client, path, save_root)               # add running
        except Exception as exc:                                          # noqa: BLE001
            log(f"add failed for {record['name']}: {exc}; will retry.")
            continue
        t = tmap.get(h) or qbt.get(client, h)
        if t is None:
            log(f"{record['name']} not visible after add; will retry.")
            continue
        tmap[h] = t

        # Trust qBittorrent's size once added (covers the None-from-file fallback).
        if not size:
            size = t.total_size or t.size or 0
            record["total_size"] = size
            budget -= int(size * config.SPACE_SAFETY_FACTOR)
        else:
            budget -= need

        record["status"] = journal.DOWNLOADING
        record["download_root"] = str(save_root)
        record["content_path"] = qbt.content_path(t)
        _file_torrent(record, config.INGESTING_DIR, "ingesting")
        journal.write_record(record)
        log(f"Downloading {record['name']} ({size >> 20}MB) to the SSD; "
            f"~{max(0, budget) >> 30}GB SSD budget left.")


# --- supersede: never spend the budget on a copy we are already beating -------------

# A complete-series / multi-season pack, and the seasons it covers. Handles the three
# shapes release names actually use: "S01-S08", "Seasons 1-8", "All seasons (1-8)".
# Each alternative ends in (?![\dp]) so the second number cannot be the head of a longer
# token. Without it "Peacemaker (2022) Season 1 - 2160p HDR" read as a span of seasons
# 1-21 (the "21" of "2160p"). The word form requires the PLURAL "Seasons": singular
# "Season 2 - 12 END" is an EPISODE range on one season, not a span of seasons, and
# matching it made an Erai-raws episode file look like a complete-series pack.
_PACK_SPAN_RE = re.compile(
    r"\bS(?P<a1>\d{1,2})\s*-\s*S?(?P<b1>\d{1,2})(?![\dp])"
    r"|\bSeasons\s*(?P<a2>\d{1,2})\s*-\s*(?P<b2>\d{1,2})(?![\dp])"
    r"|\bAll\s+seasons?\s*\(?\s*(?P<a3>\d{1,2})\s*-\s*(?P<b3>\d{1,2})(?![\dp])",
    re.IGNORECASE,
)

# No television series has more than this many seasons; a "span" beyond it is a misparse.
_MAX_PLAUSIBLE_SEASON = 40


def _pack_span(name):
    """(lo, hi) of the season span a multi-season pack covers, else None."""
    m = _PACK_SPAN_RE.search(name or "")
    if not m:
        return None
    for a, b in (("a1", "b1"), ("a2", "b2"), ("a3", "b3")):
        if m.group(a) and m.group(b):
            lo, hi = int(m.group(a)), int(m.group(b))
            if lo > hi:
                lo, hi = hi, lo
            if hi > _MAX_PLAUSIBLE_SEASON or lo < 1:
                return None
            return (lo, hi)
    return None


def _pack_series_key(name):
    """The series identity of a pack name: everything before the season span, folded to
    lowercase alphanumeric words. "That.70s.Show.S01-S08.COMPLETE..." and
    "That \'70s Show, All seasons (1-8) + Bloopers" both fold to "that 70s show"."""
    m = _PACK_SPAN_RE.search(name or "")
    head = (name or "")[:m.start()] if m else (name or "")
    head = re.sub(r"[^0-9A-Za-z]+", " ", head.lower())
    return re.sub(r"\s+", " ", head).strip()


# How much bigger the copy we already have must be before this one counts as redundant.
# Both packs cover the same seasons, so total size IS the quality comparison; 1.5x is well
# clear of encode-to-encode variation and nowhere near the 3.9x that triggered this.
_SUPERSEDE_SIZE_RATIO = 1.5


def _superseded_by(record, records):
    """Reason string when another record already covers this pack's content at materially
    better quality and is further along, else None.

    WHY THIS EXISTS
      `_purge_worse_queued` in the searcher deliberately never touches an in-flight copy:
      deleting its source `.torrent` strands the ingest record, so the policy was "let both
      finish, the better one wins at identify time". That is fine for one episode and badly
      wrong for a 35 GB pack. "That \'70s Show, All seasons (1-8) + Bloopers" (34.8 GB,
      ~175 MB/episode, 0 files fetched) was about to switch to chunked waves and consume
      the ENTIRE 35 GB budget -- starving "That.70s.Show.S01-S08.COMPLETE.SERIES.1080p
      .Bluray.x265-HiQVE" (135.6 GB, 665 MB/episode) which was already 160/209 files in.
      Hours of bandwidth and the whole budget, for content being acquired at 4x the quality.

    Deliberately narrow, because acting on it cancels a download:
      * only for a pack that has fetched NOTHING yet (chunk_done empty);
      * only against a pack of the SAME series whose season span COVERS this one;
      * only when that pack is materially bigger AND has real progress behind it.
    """
    span = _pack_span(record.get("name"))
    if span is None or (record.get("chunk_done") or []):
        return None
    key = _pack_series_key(record.get("name"))
    if not key:
        return None
    size = _size_of(record) or 0
    if size <= 0:
        return None
    for other in records.values():
        if other is record or other.get("info_hash") == record.get("info_hash"):
            continue
        if other.get("status") not in (journal.DOWNLOADING, journal.COMPLETED):
            continue
        if _pack_series_key(other.get("name")) != key:
            continue
        ospan = _pack_span(other.get("name"))
        if ospan is None or not (ospan[0] <= span[0] and ospan[1] >= span[1]):
            continue
        osize = _size_of(other) or 0
        if osize < size * _SUPERSEDE_SIZE_RATIO:
            continue
        done = len(other.get("chunk_done") or [])
        if other.get("status") != journal.COMPLETED and done <= 0:
            continue
        return (f"superseded: {other['name']} covers seasons {ospan[0]}-{ospan[1]} at "
                f"{osize >> 30}GB against this copy's {size >> 30}GB"
                + (f" and is already {done} file(s) in" if done else " and is complete")
                + ". Downloading it would spend the budget on content already being "
                  "acquired at materially better quality.")
    return None


# --- "already-present" must be PROVEN, never taken on a model's word ----------------

_EP_KEY_RE = re.compile(r"(?<![A-Za-z0-9])S(\d{1,2})[\s._-]*E(\d{1,3})", re.IGNORECASE)
# Where a release title stops being the show's name and starts being season/quality junk.
_TITLE_CUT_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:S\d{1,2}(?:E\d{1,3})?|Season[\s._-]*\d{1,2}|\d{3,4}p|"
    r"COMPLETE|BluRay|WEB-?Rip|WEB-?DL|HDTV)(?![A-Za-z])",
    re.IGNORECASE,
)


def _show_norm(text: str) -> str:
    """Fold a release title or library folder to the show's identifying words.
    "That 90s Show S02 1080p WEBRip x265 KONTRAST" and "That '90s Show (2023)" both
    fold to "that 90s show"."""
    m = _TITLE_CUT_RE.search(text or "")
    head = (text or "")[:m.start()] if m else (text or "")
    head = re.sub(r"[^0-9A-Za-z]+", " ", head.lower())
    head = re.sub(r"\b(?:19|20)\d{2}\b", " ", head)          # trailing (2023)
    return re.sub(r"\s+", " ", head).strip()


def _episode_keys_in_download(content) -> set:
    """(season, episode) every media file in the download claims, by its NAME."""
    root = Path(content)
    names = ([root.name] if root.is_file()
             else [q.name for q in root.rglob("*")
                   if q.is_file() and q.suffix.lower() in config.VIDEO_EXTENSIONS])
    keys = set()
    for n in names:
        m = _EP_KEY_RE.search(n)
        if m:
            keys.add((int(m.group(1)), int(m.group(2))))
    return keys


def _episode_keys_in_library(show_norm: str) -> set:
    """(season, episode) the library already holds for this show -- the UNION of the
    remote inventory and the local mount. Local alone is only the SSD cache and the tier
    engine evicts freely, so local alone would under-report and is never used by itself."""
    keys = set()
    paths = []
    remote = reconcile._remote_keys()
    if remote:
        paths.extend(remote)
    shows_root = config.SHOWS_ROOT
    if shows_root.is_dir():
        for show_dir in shows_root.iterdir():
            if show_dir.is_dir() and _show_norm(show_dir.name) == show_norm:
                paths.extend(str(q.relative_to(config.MEDIA_ROOT))
                             for q in show_dir.rglob("*") if q.is_file())
    for rel in paths:
        parts = str(rel).split("/")
        if len(parts) < 2 or parts[0] != "Shows":
            continue
        if _show_norm(parts[1]) != show_norm:
            continue
        m = _EP_KEY_RE.search(parts[-1])
        if m:
            keys.add((int(m.group(1)), int(m.group(2))))
    return keys


def _empty_plan_already_present(record, content):
    """For an empty plan over a download that HAS media, decide whether the
    "already-present" claim is TRUE. Returns (verified, detail).

    WHY THIS EXISTS
      An empty `files` list has three possible meanings and the pipeline conflated them:
      the content is already in the library, it is extras with no home, or A FREE MODEL
      SIMPLY GOT IT WRONG. "That 90s Show S02 1080p WEBRip x265 KONTRAST" downloaded to
      100%, a model returned an empty plan, and the drop was "treated as success": the
      local copy was deleted and the .torrent filed under finished/. The library holds
      only Season 03 of that show -- Seasons 01 and 02 are absent. A completed season
      pack was thrown away on an unverified claim.

      So the claim is now CHECKED. If the download's episodes are demonstrably NOT in the
      library, the empty plan is a model error and the record fails (which preserves the
      download and files the .torrent under failed/ for a retry) instead of completing.

    Conservative by construction: it only ever contradicts the claim when it can
    positively name missing episodes. If the download carries no SxxExx names, or the
    show cannot be located, it returns verified=True and the old behaviour stands -- so
    this can add safety but never break the legitimate already-present path.
    """
    want = _episode_keys_in_download(content)
    if not want:
        return True, "no episode-numbered media to check"
    show = _show_norm(record.get("name") or "")
    if not show:
        return True, "show name not derivable from the release title"
    have = _episode_keys_in_library(show)
    if not have:
        return True, f"no library episodes found for {show!r} to compare against"
    missing = sorted(want - have)
    if not missing:
        return True, f"all {len(want)} episode(s) already in the library"
    shown = ", ".join(f"S{s:02d}E{e:02d}" for s, e in missing[:8])
    return False, (f"{len(missing)} of {len(want)} episode(s) are NOT in the library "
                   f"({shown}{' ...' if len(missing) > 8 else ''})")


def _fit_verdict(record, size, budget, client):
    """Apply the disk-fit decisions to a torrent of KNOWN size.

    Returns the number of budget bytes consumed when the torrent was handled here (chunked,
    or deferred and therefore consuming nothing), or None when it fits and the caller should
    admit it normally. This is the .torrent path's logic, extracted so the magnet path
    cannot drift away from it again.
    """
    # Before committing ANY budget: is this just a worse copy of something we are already
    # most of the way through? Checked here because this is the one choke point all three
    # admission paths (.torrent, magnet, known-size-first) share.
    redundant = _superseded_by(record, journal.load_records())
    if redundant:
        # Declining a worse copy of content already being acquired is the system doing its
        # job, not a failure -- see journal.REFUSED.
        _fail(record, redundant, refused=True)
        return 0
    need = int(size * config.SPACE_SAFETY_FACTOR)
    if config.CHUNKED_TORRENTS_ENABLED and record.get("chunk_intent"):
        return min(config.torrent_chunk_bytes(), size) if _admit_chunked(record, client) else 0
    # Bigger than the whole SSD even empty: never refuse outright, download it in
    # space-bounded waves.
    if (need + config.MIN_FREE_BYTES) > disk_total(config.DOWNLOADS_DIR):
        if config.CHUNKED_TORRENTS_ENABLED:
            return min(config.torrent_chunk_bytes(), size) if _admit_chunked(record, client) else 0
        _fail(record, f"torrent too large to fit on the local SSD ({size} bytes) and "
                      f"chunked mode disabled; refusing")
        return 0
    if need > budget:
        waited = _deferred_for(record)
        if config.CHUNKED_TORRENTS_ENABLED and waited >= config.CHUNK_AFTER_DEFERRED_SEC:
            log(f"{record['name']} has not fit the SSD for {int(waited) // 3600}h "
                f"({need >> 30}GB needed, {max(0, budget) >> 30}GB admittable); "
                f"switching to chunked waves.")
            return min(config.torrent_chunk_bytes(), size) if _admit_chunked(record, client) else 0
        _log_deferred(record, need, budget)
        return 0
    return None                      # it fits: the caller admits it normally


def _magnet_acceptance_gate(record, client, h):
    """Run the DB acceptance gate over a magnet's just-resolved file list.

    Returns True to carry on admitting, False when the magnet was REFUSED (in which case
    it has already been removed from qBittorrent and retired as `refused`).

    Three verdicts, and each costs something different:

      * **accept** — at least one file is an item we do not own at equal-or-better
        quality. Admit it.
      * **refuse** — every file is already owned, and the mapping was corroborated
        (`acceptance._refusal_is_corroborated`: it read most of the torrent and did not
        collapse it onto one key). This is a deliberate decline, so it lands as `refused`
        rather than `failed` — the pipeline working, not a fault (§4.97).
      * **unknown** — the gate could not reach a verdict: no ledger row, an unreadable
        file list, or an "everything is owned" claim its own mapping could not corroborate.
        ADMITTED, and this is the deliberate choice. §7 says a fallback for "I cannot
        verify this" must not be "accept it" — but the alternative here is not "defer",
        it is "destroy": the torrent is already queued, no later sweep re-offers it, and
        an unreadable file list does not become readable by waiting (which would also
        break "a queue entry must always be able to die"). So it is admitted, LOGGED, and
        COUNTED in the gate heartbeat. That is the part §4.120 lacked: the old path did
        not admit-on-doubt, it admitted on nothing at all, silently, and no one could
        tell. An admitted UNKNOWN here is a visible, measurable number -- and its size is
        the argument for fixing the parser gaps behind it (§5 item 4).

    The verdict is recorded on the record and not re-litigated. A magnet that does not fit
    the disk budget comes back through here every cycle, and re-running the gate would
    re-read the DB each time and let one verdict flap into another as the library changes.
    """
    return _acceptance_gate(record, h, lambda: acceptance_gate.file_names(client, h),
                            source="magnet", added_to=client)


def _torrent_acceptance_gate(record, h, path):
    """The same gate, on the `.torrent` path, BEFORE the torrent is added.

    §4.120 was one gate reachable from only one of two drop paths, and it went dark when
    the traffic moved to the other. That is the shape again, mirrored: the `.torrent`
    drop path's gate lives in the SEARCHER, at drop time, so a `.torrent` that reaches the
    watch folder any other way -- a hand-drop, or any drop at all while the searcher is
    quarantined -- was admitted with nothing judging it. Measured 2026-09-07: 100% of the
    queue was `.torrent`, the searcher had been silent for 50h, and every drop that
    afternoon went in ungated.

    A `.torrent` carries its own file list, so this runs BEFORE `qbt.add` -- earlier and
    cheaper than the magnet path, where the list only exists after the swarm answers. A
    refusal here costs nothing: nothing has been added and nothing downloaded.
    """
    # SAFETY first, and it is not the relevance gate. A hostile file list (traversal,
    # absolute path, or executable-only with no media) is refused outright before
    # anything is added. This check used to live in the searcher, which judged every drop
    # it made; the searcher is gone and hand-drops are the only admission path left, so it
    # runs here now (§4.22). Executables BESIDE media are admitted since 2026-10-03 at
    # the owner's instruction -- the pipeline never executes them (see acceptance_gate).
    if not acceptance_gate.metadata_is_safe(path):
        record["gate_decision"] = acceptance_gate.REFUSE
        record["gate_reason"] = ("unsafe .torrent metadata (absolute path, path "
                                 "traversal, or executable files with no media)")
        acceptance_gate.record(acceptance_gate.REFUSE)
        _fail(record, f"acceptance gate: {record['gate_reason']}", refused=True)
        return False

    return _acceptance_gate(record, h,
                            lambda: acceptance_gate.file_names_from_torrent(path),
                            source=".torrent", added_to=None)


def _acceptance_gate(record, h, get_names, source, added_to):
    """The gate body both drop paths share, so the two cannot answer differently.

    `get_names` is deferred (a callable) because reading the file list costs an API call
    on the magnet path and a file read on the `.torrent` path -- neither is worth paying
    for a record whose verdict is already on it. `added_to` is the qBittorrent client when
    the torrent is ALREADY added and a refusal therefore has to remove it, and None when
    the gate runs ahead of the add and there is nothing to take back.
    """
    if record.get("gate_decision"):
        return record["gate_decision"] != acceptance_gate.REFUSE

    names = get_names()
    v = acceptance_gate.check(h, names, record.get("name") or "")
    acceptance_gate.record(v.decision)
    record["gate_decision"] = v.decision
    record["gate_reason"] = v.reason

    if v.decision == acceptance_gate.REFUSE:
        if added_to is not None:
            try:
                qbt.remove(added_to, h, delete_files=True)
            except Exception as exc:                                     # noqa: BLE001
                log(f"  could not remove gate-refused {source} {record['name']}: {exc}")
        _fail(record, f"acceptance gate: {v.reason}", refused=True)
        return False

    if v.decision == acceptance_gate.UNKNOWN:
        log(f"{record['name']}: acceptance gate reached no verdict ({v.reason}); "
            f"admitting.")
    else:
        log(f"{record['name']}: acceptance gate accepts ({v.reason}).")
    journal.write_record(record)
    return True


def _admit_magnet(record, client, tmap, budget=None):
    """Admit a QUEUED magnet: add it running (qBittorrent fetches its metadata from the
    swarm — which is what yields the real total size — then downloads), wait for that
    metadata, and record the size so the completion signal stays correct. Returns
    `(admitted, bytes_used)`.

    A magnet whose metadata never resolves (a dead swarm / no DHT / no reachable tracker)
    is removed again and left QUEUED for a later cycle rather than failed — it costs
    nothing to retry and the swarm may simply be slow. But only for a BOUNDED window: a
    genuinely dead magnet retried forever holds a queue slot and reprints its "unresolved"
    line every cycle, which is exactly what buried the real work under a loop of
    "Mobile.Suit.Gundam.Unicorn.Re.0096.E13 … leaving QUEUED" forever. After
    MAGNET_METADATA_ABANDON_SEC of never once resolving, it is failed like a stalled
    download."""
    h = record["info_hash"]
    save_root = config.INCOMING_DIR
    save_root.mkdir(parents=True, exist_ok=True)
    try:
        qbt.add_magnet(client, record["magnet"], save_root)
    except Exception as exc:                                             # noqa: BLE001
        log(f"add-magnet failed for {record['name']}: {exc}; will retry.")
        return False, 0
    t = tmap.get(h) or qbt.get(client, h)
    if t is None:
        log(f"{record['name']} not visible after magnet add; will retry.")
        return False, 0
    tmap[h] = t
    # qBittorrent reports total_size = -1 until a magnet's metadata has resolved; clamp that
    # to "unknown" (0) so the wait below actually waits, and the completion signal (which
    # compares on-disk bytes against total_size) can never read a negative size.
    size = max(0, getattr(t, "total_size", 0) or getattr(t, "size", 0) or 0)
    deadline = time.time() + config.MAGNET_METADATA_WAIT_SEC
    while time.time() < deadline and not size:
        time.sleep(2)
        t = qbt.get(client, h)
        if t is not None:
            size = max(0, getattr(t, "total_size", 0) or getattr(t, "size", 0) or 0)
    if not size:
        try:
            qbt.remove(client, h, delete_files=True)
        except Exception as exc:                                         # noqa: BLE001
            log(f"  could not remove unresolved magnet {record['name']}: {exc}")
        # The clock lives on the RECORD, not in memory: an in-memory stamp would re-arm on
        # every daemon restart, and a dead magnet would then outlive every abandon window.
        # Written once, on the first failure, so a retry costs no journal write.
        now = time.time()
        since = record.get("magnet_unresolved_since")
        if not since:
            # Seed the clock from when the magnet was REGISTERED, not from now: a magnet
            # that has already been failing every cycle for hours must not be handed a
            # fresh full window just because this bound arrived after it did. Same
            # created_at-derived reasoning (and restart-survival) as `_deferred_for`.
            since = now - _deferred_for(record)
            record["magnet_unresolved_since"] = since
            journal.write_record(record)
        waited = now - since
        attempts = int(record.get("metadata_attempts") or 0)
        if (waited >= config.MAGNET_METADATA_ABANDON_SEC
                and attempts >= config.MAGNET_METADATA_MIN_ATTEMPTS):
            _fail(record, f"magnet metadata never resolved in {int(waited // 3600)}h "
                          f"across {attempts} attempt(s) (dead swarm/DHT, no reachable "
                          f"tracker)")
            return False, 0
        log(f"{record['name']}: magnet metadata unresolved (dead swarm/DHT); "
            f"leaving QUEUED for a later cycle "
            f"({int(waited // 60)}m of "
            f"{config.MAGNET_METADATA_ABANDON_SEC // 3600}h before it is abandoned).")
        return False, 0
    t = qbt.get(client, h) or t            # freshest view now that metadata has resolved
    record.pop("magnet_unresolved_since", None)   # it resolved; the abandon clock is void

    # THE ACCEPTANCE GATE (§4.120). The metadata that just landed IS the file list, and
    # not one byte of content has been fetched yet -- this is the only moment on the
    # magnet path where the gate's input exists and refusing is still free. Runs before
    # the size is persisted and before any budget is committed.
    if config.ACCEPTANCE_GATE and acceptance_gate is not None \
            and not _magnet_acceptance_gate(record, client, h):
        return False, 0
    # PERSIST the size the moment we learn it, before deciding anything. That is what lets
    # every LATER cycle budget-check this magnet up front (see admit_downloads) instead of
    # adding it blind again.
    record["total_size"] = size
    journal.write_record(record)

    if budget is not None:
        verdict = _fit_verdict(record, size, budget, client)
        if verdict is not None:
            # It does not fit right now (or it was handed to the chunked lane). Take it back
            # out of qBittorrent so it is not quietly downloading against a budget that has
            # already been spent; it stays QUEUED and is reconsidered next cycle, now with a
            # known size. Without this the disk budget means nothing for magnets.
            try:
                qbt.remove(client, h, delete_files=True)
            except Exception as exc:                                     # noqa: BLE001
                log(f"  could not un-admit over-budget magnet {record['name']}: {exc}")
            return False, verdict
    record["status"] = journal.DOWNLOADING
    record["download_root"] = str(save_root)
    record["content_path"] = qbt.content_path(t)
    _file_torrent(record, config.INGESTING_DIR, "ingesting")
    journal.write_record(record)
    log(f"Downloading {record['name']} ({size >> 20}MB) via magnet to the SSD.")
    return True, int(size * config.SPACE_SAFETY_FACTOR)


# --- state transitions -------------------------------------------------------

# info_hash -> first time we observed this torrent stalled (qBittorrent `stalledDL`).
# Only a fallback for the rare torrent with neither an `added_on` stamp nor a record
# creation time. In-memory on purpose so it never adds a snapshot per stalled torrent per
# cycle; the primary clock is qBittorrent's own `added_on`, which is exact and persistent.
_STALL_SINCE: dict[str, float] = {}


_NO_PROGRESS_STATES = (
    "stalledDL", "queuedDL", "stoppedDL", "pausedDL", "error", "missingFiles",
)


def _has_fetched_anything(record, t):
    """Whether this torrent has EVER moved a byte, from qBittorrent and the record.

    THE LINE THE OWNER DREW (2026-09-26): a week to prove the swarm can serve it, then
    infinite patience once it has. qBittorrent's counters are the live fact (`progress`
    is the respected one; `downloaded`/`completed` are belt-and-braces). The record is
    consulted too because a chunked pack is stopped between waves and its live counters
    reset when it is re-added, while `chunk_done`/`chunk_filed`/`applied` survive as
    proof of bytes that were fetched and filed.

    A torrent with zero bytes after the grace is dead weight holding its admission
    reservation; one with a single verified byte is a slow swarm worth waiting out.
    """
    for attr in ("progress", "downloaded", "completed"):
        try:
            if float(getattr(t, attr, 0) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    for key in ("chunk_done", "chunk_filed", "chunk_failed", "chunk_dropped", "applied"):
        if record.get(key):
            return True
    return False


def _added_at(record, t):
    """Unix time this torrent began waiting to prove it can fetch.

    qBittorrent's `added_on` is the authoritative start: it survives a daemon restart, so
    the grace window cannot re-arm, and a re-dropped torrent starts a fresh window.
    `created_at` from the record is the fallback (the journal is durable even when the
    torrent has been removed), and the in-memory stamp is the last resort for a synthetic
    torrent with neither.
    """
    try:
        stamp = float(getattr(t, "added_on", 0) or 0)
        if stamp > 0:
            return stamp
    except (TypeError, ValueError):
        pass
    created = record.get("created_at")
    if created:
        try:
            from datetime import datetime as _dt
            ts = _dt.fromisoformat(str(created).replace("Z", "+00:00")).timestamp()
            if ts > 0:
                return ts
        except (TypeError, ValueError):
            pass
    h = record["info_hash"]
    since = _STALL_SINCE.get(h)
    if since is None:
        since = time.time()
        _STALL_SINCE[h] = since
    return since


def _abandon_stalled(record, t, client):
    """Fail a download that has fetched NOTHING within the first-progress grace.

    One deadline, one condition (HANDOFF §6, owner decision 2026-09-26):
    `STALL_FIRST_PROGRESS_GRACE_SEC` (one week) from qBittorrent's `added_on`, and ONLY
    for a torrent that has never moved a byte. The moment a torrent has fetched anything
    it is never abandoned, however long its swarm goes quiet -- a public/DHT-only pack
    that stalled at 70% has proven it can be served, and its partial payload is the one
    thing a retry cannot recreate cheaply.

    A never-starting torrent still has to drain, because it keeps its unfetched bytes
    reserved in `_remaining_budget` (for a chunked pack, the whole active wave), so left
    alone it holds the whole admission queue behind a drop that can never advance. That
    is the only thing this fails. The abandon keeps every byte
    (`delete_files=False`): a re-drop resumes from whatever is on disk, and the janitor
    reclaims a directory that is never retried after its own grace.

    Returns True when the record was failed, so the caller can stop advancing it.
    """
    h = record["info_hash"]
    # Every state here means "this torrent is not fetching bytes". "stalledDL" is an
    # active download with no peers; "queuedDL" is the same torrent waiting for a slot
    # under the queueing ceiling; "stoppedDL"/"pausedDL" (qBittorrent v5 / v4 naming) is
    # a download that is not even trying; "error"/"missingFiles" cannot complete without
    # intervention. A never-started torrent must drain out of ANY of them -- otherwise it
    # holds its reservation forever just as if it were stalled.
    #
    # Omitting the stopped states is what produced the deadlock this function's docstring
    # describes. A 42 GB pack was added, stopped after 22 MB, and left in `stoppedDL`:
    # `_remaining_budget` reserved its full 39.5 GB of unfetched bytes, this guard
    # returned False because "stoppedDL" was not in the list, and nothing ever reclaimed
    # it. The budget sat at -5.6 GB with 75 GB free on the disk, and every chunked wave on
    # the box parked itself with "0MB admittable" while nothing downloaded.
    #
    # CHUNKED RECORDS REACH HERE TOO. This comment used to assert they could not, and the
    # code disagreed with it: `_advance_chunked` calls this on every in-flight wave. A
    # chunked pack stopped between waves is normal; if the whole torrent has never fetched
    # anything, the grace below still runs from its own add time, not the current wave's.
    state = getattr(t, "state", "")
    if state not in _NO_PROGRESS_STATES:
        _STALL_SINCE.pop(h, None)
        return False
    if _has_fetched_anything(record, t):
        _STALL_SINCE.pop(h, None)
        return False
    now = time.time()
    since = _added_at(record, t)
    if now - since < config.STALL_FIRST_PROGRESS_GRACE_SEC:
        return False
    # delete_files=False ON PURPOSE. A never-started torrent has no partial payload to
    # lose today, but the same path serves a re-dropped torrent whose bytes arrive
    # between the clock's start and this check; keeping the files makes the rule uniform
    # and lets a re-drop resume.
    try:
        qbt.remove(client, h, delete_files=False)
    except Exception as exc:                                              # noqa: BLE001
        log(f"stall-abandon: could not remove {record['name']} from qBittorrent: {exc}")
    _STALL_SINCE.pop(h, None)
    _fail(record, f"no progress in {int((now - since) // 86400)}d since it was added "
                  f"(not one byte ever fetched); abandoned to release the download "
                  f"budget; partial download kept for a retry")
    return True


def advance(record, client, records=None, tmap=None):
    status = record["status"]
    if status == journal.DOWNLOADING:
        if record.get("chunked"):
            _advance_chunked(record, client, records or {record["info_hash"]: record}, tmap)
        else:
            _advance_downloading(record, client, tmap)
    elif status == journal.DOWNLOADED:
        _advance_identify(record, client)
    elif status == journal.IDENTIFIED:
        _advance_stage(record, client)
    elif status == journal.STAGED:
        _advance_verify(record, client)
    elif status == journal.VERIFIED:
        _advance_cleanup(record, client)


def _advance_downloading(record, client, tmap=None):
    h = record["info_hash"]
    t = tmap.get(h) if tmap is not None else qbt.get(client, h)
    if t is not None:
        # Self-heal a magnet record whose total_size/content_path were recorded before its
        # metadata resolved (qBittorrent reports total_size -1 until then). Once the torrent
        # is present with a real size, backfill the record so the completion signal below is
        # correct even if qBittorrent auto-removes the torrent the moment it finishes.
        live_size = max(0, getattr(t, "total_size", 0) or getattr(t, "size", 0) or 0)
        if _size_of(record) <= 0 and live_size > 0:
            record["total_size"] = live_size
        if live_size > 0 and not record.get("content_path"):
            record["content_path"] = qbt.content_path(t)
        if qbt.is_complete(t):
            record["content_path"] = qbt.content_path(t)
            _mark_downloaded(record, client)
        else:
            pct = int((t.progress or 0) * 100)
            log(f"  {record['name']}: {pct}% ({t.state})")
            if _abandon_stalled(record, t, client):
                return
        return

    # qBittorrent no longer has the torrent. Because qBittorrent auto-removes on
    # completion (keeping files), "gone" usually means "finished" — so decide by
    # what's on disk, not by qBittorrent's memory. Complete iff the downloaded
    # bytes match the known total size; short means it was cancelled/errored.
    content = record.get("content_path")
    size = _size_of(record)
    on_disk = _dir_size(content) if content else 0
    if content and Path(content).exists() and size and size > 0 and on_disk >= int(size * 0.999):
        log(f"Download complete (auto-removed by qBittorrent at 100%): {record['name']}")
        _mark_downloaded(record, client)
    elif on_disk <= 0:
        # Nothing was ever fetched, so nothing was lost -- the torrent left qBittorrent
        # before a single byte landed. The old wording for this ("on-disk data incomplete
        # (0MB of 4355MB)") read to a human as "a 4.3 GB download disappeared", and that is
        # how it was written up in §4.88 as the one class that might be losing content. It
        # is not: the FIRST number is what is on disk, not what vanished. Say so plainly.
        _fail(record, f"removed from qBittorrent before any data was fetched "
                      f"(0MB of {max(0, size) >> 20}MB on disk, so nothing was lost); "
                      f"re-drop the source to retry it")
    else:
        _fail(record, f"torrent gone from qBittorrent with only part of its data on disk "
                      f"({on_disk >> 20}MB of {max(0, size) >> 20}MB fetched)")


def _pick_batch(files, done, budget, max_files=None):
    """Greedily pick file indices not yet done whose sizes sum to <= budget (in torrent
    order), at most max_files of them.

    The budget is a hard cap, never rounded up to fit one more file: it is the live disk
    headroom, so exceeding it by a single 4 GB episode is exactly the MIN_FREE breach
    chunking exists to prevent. The caller guarantees the budget holds at least the
    smallest remaining file, so a non-empty batch is always possible when work remains.
    """
    limit = max_files or config.CHUNK_MAX_FILES_PER_WAVE
    batch, total = [], 0
    for f in files:
        if len(batch) >= limit:
            break
        if f.index in done:
            continue
        if total + f.size > budget:
            continue
        batch.append(f.index)
        total += f.size
    return batch


_CHUNK_STARVED_LOGGED: dict[str, float] = {}


def _log_chunk_starved(record, wave_budget, smallest):
    """Say out loud that a chunked torrent can't even start its next wave.

    Chunking is the answer to a disk that stays full, so it must not reproduce the silent
    wait it was built to fix: if the live budget cannot hold the smallest remaining file,
    the pack is stalled and something else is holding the disk. Throttled hourly per
    torrent, same as DEFERRED.
    """
    h = record["info_hash"]
    now = time.time()
    if now - _CHUNK_STARVED_LOGGED.get(h, 0) < _DEFER_LOG_INTERVAL_SEC:
        return
    _CHUNK_STARVED_LOGGED[h] = now
    log(f"DEFERRED (chunked) {record['name']}: {max(0, wave_budget) >> 20}MB admittable, "
        f"smallest remaining file is {smallest >> 20}MB. The wave waits for space.")


def _stop_torrent(client, info_hash) -> None:
    """Stop a torrent, across qBittorrent API generations (`stop` on v5, `pause` before)."""
    for meth in ("torrents_stop", "torrents_pause"):
        fn = getattr(client, meth, None)
        if fn is None:
            continue
        try:
            fn(torrent_hashes=info_hash)
            return
        except Exception:                                                 # noqa: BLE001
            continue
    log(f"  could not stop {info_hash}: no usable qBittorrent stop/pause method")


def _admit_chunked(record, client, tmap=None):
    """Admit an oversized torrent in CHUNKED mode: add it paused, refuse only if a single
    file exceeds the wave budget, else mark it chunked and hand off to _advance_chunked
    (which selects/enables each wave). Returns True once it is registered as chunked."""
    if tmap is None:
        tmap = qbt.torrent_map(client)
    h = record["info_hash"]
    save_root = config.INCOMING_DIR
    # A MAGNET has no .torrent to hand qBittorrent -- `_ensure_source` would return the
    # `.magnet` text file and `qbt.add` would reject it. Since the searcher now drops
    # magnets as the norm, taking only the .torrent route here meant the chunked lane --
    # the ONLY way an oversized pack can ever download -- was unreachable for most drops.
    if record.get("magnet"):
        # A magnet carries no file list: qBittorrent has to pull the metadata from the
        # swarm, and a STOPPED torrent never does -- so it cannot simply be added paused
        # like a .torrent. It is added running, then WAITED ON and immediately stopped
        # again as soon as the metadata lands.
        #
        # The waiting and the stopping are both load-bearing. Without them this returned
        # "files not listed yet; will retry" on the same cycle it added the torrent and
        # left it RUNNING -- so a 135 GB pack that had just been ruled too big to fit
        # started downloading unrestricted against a 19 GB budget, which is precisely the
        # disk-fill the chunked lane exists to prevent. Whatever this function does next,
        # it must not leave an unprioritised torrent running.
        try:
            save_root.mkdir(parents=True, exist_ok=True)
            qbt.add_magnet(client, record["magnet"], save_root, paused=False)
        except Exception as exc:                                          # noqa: BLE001
            log(f"chunked magnet add failed for {record['name']}: {exc}; will retry.")
            return False
        deadline = time.time() + config.MAGNET_METADATA_WAIT_SEC
        while time.time() < deadline and not qbt.files(client, h):
            time.sleep(2)
        _stop_torrent(client, h)          # never leave it running unprioritised
        if not qbt.files(client, h):
            log(f"{record['name']}: magnet metadata not in yet for chunking; "
                f"stopped and will retry next cycle.")
            return False
    else:
        path = _ensure_source(record)
        if path is None:
            log(f"chunked add skipped for {record['name']}: source .torrent unrecoverable")
            return False
        try:
            save_root.mkdir(parents=True, exist_ok=True)
            qbt.add(client, path, save_root, paused=True)
        except Exception as exc:                                          # noqa: BLE001
            log(f"chunked add failed for {record['name']}: {exc}; will retry.")
            return False
    t = tmap.get(h) or qbt.get(client, h)
    if t is None:
        log(f"{record['name']} not visible after chunked add; will retry.")
        return False
    tmap[h] = t
    fls = qbt.files(client, h)
    if not fls:
        log(f"{record['name']} files not listed yet; will retry.")
        return False
    biggest = max(f.size for f in fls)
    if biggest > config.MAX_SINGLE_FILE_BYTES:
        qbt.remove(client, h, delete_files=True)
        _fail(record, f"un-chunkable: a single file ({biggest} bytes) exceeds the "
                      f"{config.MAX_SINGLE_FILE_BYTES}-byte MEGA cap")
        return False
    # Park every file (prio 0); _advance_chunked enables the first wave. The share-limit
    # exemption must land BEFORE any wave is enabled -- without it qBittorrent removes the
    # torrent the instant wave 1 completes (§ qbt.exempt_from_share_limits).
    try:
        qbt.exempt_from_share_limits(client, h)
        qbt.set_file_priority(client, h, [f.index for f in fls], 0)
    except Exception as exc:                                              # noqa: BLE001
        log(f"chunked priority-park failed for {record['name']}: {exc}; will retry.")
        return False
    record["chunked"] = True
    # A re-drop can carry forward the files a previous attempt already proved into the
    # library (_carry_chunk_progress); those stay done so the pack resumes instead of
    # re-fetching hundreds of GB it has already filed.
    record["chunk_done"] = sorted(set(record.get("chunk_done") or []))
    record["chunk_active"] = []
    record["chunk_attempts"] = {}
    record["chunk_failed"] = []
    record["chunk_failed_idx"] = []
    record["status"] = journal.DOWNLOADING
    record["download_root"] = str(save_root)
    record["content_path"] = qbt.content_path(t)
    _file_torrent(record, config.INGESTING_DIR, "ingesting")
    journal.write_record(record)
    resumed = (f", resuming with {len(record['chunk_done'])} file(s) already filed"
               if record["chunk_done"] else "")
    log(f"Chunked download started for {record['name']} "
        f"({_size_of(record) >> 30}GB across {len(fls)} files, "
        f"{config.torrent_chunk_bytes() >> 30}GB waves{resumed}).")
    return True


def _park_chunked(record, client, fls, smallest):
    """Deregister a chunked pack that has nothing to fetch until the disk frees.

    Between waves a chunked torrent has every SELECTED file complete, and that is the only
    thing qBittorrent measures: it reports 100% and sits in the list as a finished torrent,
    however many hundreds of GB the pack still has to fetch. Stopping it silences the
    upload but not the lie -- the row is still there, still saying "completed", for what can
    be days. The torrent is registered for exactly one reason, to fetch the next wave, and
    between waves it is not doing that.

    So take it out of qBittorrent, files kept, and put it back when its next wave can
    actually start. Everything needed to resume is already durable -- the source `.torrent`
    and `chunk_done` -- which is the same guarantee _readopt_chunked already relies on.

    Refuses to park a pack whose source `.torrent` is gone: removal would then be
    unrecoverable, and a stopped torrent in the list beats a stranded one. Returns whether
    the pack was parked.
    """
    h = record["info_hash"]
    path = _ensure_source(record)
    if path is None:
        try:
            qbt.stop(client, h)
        except Exception:                                                 # noqa: BLE001
            pass
        return False
    try:
        qbt.remove(client, h, delete_files=False)
    except Exception as exc:                                              # noqa: BLE001
        log(f"could not park {record['name']}: {exc}; leaving it registered.")
        return False
    record["chunk_parked"] = True
    record["chunk_park_min_bytes"] = int(smallest)
    record["chunk_parked_at"] = time.time()
    record["chunk_active"] = []
    journal.write_record(record)
    log(f"Parked chunked {record['name']} "
        f"({len(record.get('chunk_done') or [])}/{len(fls)} files filed): out of "
        f"qBittorrent until {smallest >> 20}MB is admittable. Files kept.")
    return True


def _unpark_chunked(record, client, records, tmap=None):
    """Put a parked pack back into qBittorrent once its next wave will fit, else leave it out.

    Re-adding the moment it is missed would defeat parking entirely -- the row would
    reappear within a cycle and sit at "completed" again. The pack comes back when there is
    work for it, and not before.
    """
    need = int(record.get("chunk_park_min_bytes") or 0)
    # A pack parked moments ago is not yet a candidate for re-adoption, even if the live
    # budget momentarily clears its smallest file: the budget is a snapshot that other
    # torrents are consuming in the same cycle, so re-adding on a hairline margin just
    # parks again next cycle (§ config.CHUNK_PARK_COOLDOWN_SEC). Require the pack to have
    # been parked long enough that the space is plausibly durable before paying the
    # add-then-remove round-trip.
    parked_at = record.get("chunk_parked_at")
    if parked_at and time.time() - float(parked_at) < config.CHUNK_PARK_COOLDOWN_SEC:
        return False
    budget = min(config.torrent_chunk_bytes(), _remaining_budget(records, client, tmap))
    if need and budget < need:
        return False
    record["chunk_parked"] = False
    journal.write_record(record)
    return _readopt_chunked(record, client, tmap)


def _readopt_chunked(record, client, tmap=None):
    """Re-add a chunked torrent that is no longer in qBittorrent, instead of failing it.

    A chunked pack is the one torrent whose lifetime is measured in days, so it WILL
    outlive a qBittorrent restart, a crash, or a stray manual removal — and unlike a
    whole-torrent download, "gone from qBittorrent" can never mean "finished", because a
    chunked torrent is only finished when _advance_chunked itself removes it. Do not turn
    absence back into a failure here: it strands every file the pack has not yet reached,
    and the pack is the one case where those bytes exist nowhere else.

    Everything needed to resume is already durable: the source `.torrent` in the watch
    folder, and `chunk_done` in the journal. The re-add parks every file at priority 0
    and clears the active wave, so the next cycle selects a fresh wave from whatever is
    still unfiled. Bytes on disk from a wave that was in flight are not lost — qBittorrent
    rechecks them when that file is next selected, and filing is idempotent anyway.
    """
    if tmap is None:
        tmap = qbt.torrent_map(client)
    h = record["info_hash"]
    save_root = Path(record.get("download_root") or config.INCOMING_DIR)
    if record.get("magnet"):
        # A MAGNET has no `.torrent` for qBittorrent to parse: `_ensure_source` hands back
        # the `.magnet` TEXT file and `qbt.add` rejects it with "not a bencoded dict".
        # `_admit_chunked` already knows this and takes the magnet route; this function did
        # not, so a chunked MAGNET pack that ever left qBittorrent could never be re-adopted
        # -- it just reprinted "chunked re-add failed ... will retry" every cycle forever.
        # Observed on two packs stuck for 154 minutes, one of them Dawn of the Croods S02.
        #
        # Same shape as the admission path: added RUNNING so it can pull metadata from the
        # swarm (a stopped magnet never does), then stopped as soon as the metadata lands,
        # so it is never left downloading unprioritised.
        try:
            save_root.mkdir(parents=True, exist_ok=True)
            qbt.add_magnet(client, record["magnet"], save_root, paused=False)
        except Exception as exc:                                          # noqa: BLE001
            log(f"chunked magnet re-add failed for {record['name']}: {exc}; will retry.")
            return False
        deadline = time.time() + config.MAGNET_METADATA_WAIT_SEC
        while time.time() < deadline and not qbt.files(client, h):
            time.sleep(2)
        _stop_torrent(client, h)
        if not qbt.files(client, h):
            log(f"{record['name']}: magnet metadata not in yet for chunked re-adopt; "
                f"stopped and will retry next cycle.")
            return False
    else:
        path = _ensure_source(record)
        if path is None:
            _fail(record, f"chunked torrent gone from qBittorrent and its source .torrent "
                          f"is missing, so it cannot be resumed: {record.get('torrent_path')}")
            return False
        try:
            save_root.mkdir(parents=True, exist_ok=True)
            qbt.add(client, path, save_root, paused=True)
        except Exception as exc:                                          # noqa: BLE001
            log(f"chunked re-add failed for {record['name']}: {exc}; will retry.")
            return False
    t = tmap.get(h) or qbt.get(client, h)
    fls = qbt.files(client, h) if t is not None else []
    if t is None or not fls:
        log(f"{record['name']} not visible after chunked re-add; will retry.")
        return False
    tmap[h] = t
    try:
        qbt.exempt_from_share_limits(client, h)
        qbt.set_file_priority(client, h, [f.index for f in fls], 0)
    except Exception as exc:                                              # noqa: BLE001
        log(f"chunked re-park failed for {record['name']}: {exc}; will retry.")
        return False
    record["chunk_active"] = []
    record["content_path"] = qbt.content_path(t)
    journal.write_record(record)
    log(f"Re-adopted chunked torrent {record['name']} "
        f"({len(record.get('chunk_done') or [])}/{len(fls)} files already filed); "
        f"waves resume next cycle.")
    return True


# Sentinel returned by _ingest_one_file when a chunked file is media that has no
# library home (a creditless OP/ED, an NCOP/NCED, or a repeat already on disk) and
# is dropped cleanly rather than filed. Distinct from [] (a genuine failure whose
# bytes are kept for retry) and from a non-empty applied list.
_NO_HOME = object()
# A file whose only destination collides with an episode already on disk under a
# different name. Distinct from _NO_HOME on purpose: _NO_HOME is a clean drop (the bytes
# are freed), _PARK must never free them -- the collision is an unresolved question, and
# the release parks with the bytes intact (HANDOFF 10.2).
_PARK = object()


# A no-library-home EXTRA, as opposed to a real episode: a creditless or standalone
# OP/ED, an NCOP/NCED, a TV CM, a placeholder. These are deleted the same way declined
# manga chapters are — the bytes are freed, never renamed and never a failure. Used to
# keep the dual-audio exception honest: a dual-audio torrent's opening is not a
# dual-audio *upgrade*, so it must be dropped cleanly rather than failed.
#
# The token list now lives in `plan_coverage`, which the coverage contract also consults,
# so the wave cleanup and the plan-coverage verdict can never disagree about what an
# extra is.
def _looks_like_no_home_extra(name):
    return plan_coverage.looks_like_no_home_extra(name)


def _ingest_one_file(record, file_abs, sub_id, sibling_seasons=None):
    """Run the normal identify -> validate -> apply -> verify pipeline on a SINGLE completed
    file from a chunked torrent, filing it into the library. Returns applied entries, []
    on a genuine failure (bytes kept for retry), _NO_HOME when the file is media with no
    library home and is dropped cleanly, or _PARK when its only slot collides with an
    existing episode and the release must park rather than free it. Media-Syncer uploads
    what lands, then frees the local library copy."""
    try:
        stored = identify.load_stored_plan(record.get("info_hash"))
        plan, rationale = identify.run_identify(sub_id, str(file_abs), log_fn=log,
                                                stored_plan=stored,
                                                settled=identify.settled_ok(stored),
                                                sibling_seasons=sibling_seasons)
        journal.log_decision(sub_id, file_abs.name,
                             (rationale or "(no rationale)") + "\n\nPLAN:\n" + _pretty(plan))
        # An empty plan over a media file is the same "nothing to place" verdict the
        # whole-torrent path accepts (§ An empty plan over media is a skip, not a
        # failure): a creditless OP/ED, an NCOP/NCED, or a repeat already on disk.
        # validate_plan rejects an empty list to stop the "already-present" shortcut,
        # so without this the file burns its retries into chunk_failed and ends the
        # whole pack FAILED. Dual-audio is the one loud exception — the searcher's
        # upgrade signal — and is left to the normal failure path for review.
        if not plan.get("files"):
            if _looks_like_dual_audio(record["name"]) \
                    and not _looks_like_no_home_extra(file_abs.name):
                log(f"  chunked file is a possible dual-audio upgrade with no plan; "
                    f"failing for review: {file_abs.name}")
                return []
            log(f"  chunked file has no library home (extras/repeat); "
                f"dropping: {file_abs.name}")
            return _NO_HOME
        library.validate_plan(plan, str(file_abs.parent),
                              sibling_seasons=sibling_seasons,
                              release_name=record.get("name"))
        if plan.get("_collision_parked"):
            # The only copy of this file collides with an episode already at that slot
            # under a different name, and the harness will not guess which is right.
            # Return a PARK verdict, never `_NO_HOME`: dropping it would delete the
            # download on a collision the plan never resolved (HANDOFF 10.2).
            log(f"  chunked file collides with an existing episode; parking the "
                f"release instead of freeing it: {file_abs.name}")
            return _PARK
        applied = library.apply_plan(plan, sub_id)
        ok, msg = library.verify_applied(applied)
        if not ok:
            log(f"  chunked file verify failed ({file_abs.name}): {msg}")
            return []
        return applied
    except identify.IdentifyUnavailable:
        # Deliberately NOT swallowed into the `return []` below. The caller deletes this
        # wave's local files and marks it done immediately after this returns, so a []
        # here means the bytes are unlinked having never been filed -- and for a chunked
        # torrent those bytes are the only copy. Propagate so the caller aborts the wave
        # with the files still on disk.
        raise
    except library.CollisionPark as exc:
        # `validate_plan` proved the slot's existing file is a DIFFERENT episode (the
        # journal's source title, or the existing file's own name). This is the raising
        # twin of the `_collision_parked` check above, and it must end the same way: park
        # with every byte on disk. Falling through to `return []` would keep the bytes
        # for CHUNK_FILE_MAX_ATTEMPTS more cycles and then FREE them UNFILED, which is
        # the one thing a collision must never do (HANDOFF 10.2).
        log(f"  chunked file collides with an existing episode (identity proven "
            f"different); parking the release instead of freeing it: {file_abs.name} "
            f"({exc})")
        return _PARK
    except Exception as exc:                                              # noqa: BLE001
        log(f"  chunked file ingest failed ({file_abs.name}): {exc}")
        return []


def _path_key(p):
    """Comparison key for an on-disk path. NFC-normalized because the two sides being
    matched come from different sources -- qBittorrent's file list and a directory listing
    the identify run walked -- and macOS hands out decomposed (NFD) filenames while most
    other producers emit composed ones. An unnormalized compare silently misses every
    accented filename, which for a chunked torrent means never freeing a file that IS in
    the library."""
    try:
        return unicodedata.normalize("NFC", str(Path(p).resolve()))
    except OSError:
        return unicodedata.normalize("NFC", str(p))


def _chunk_content_root(t, save_path, by_index, pending):
    """The directory (or single file) holding this torrent's payload, and nothing else.

    Never widened to the shared INCOMING_DIR: that directory holds every other torrent's
    download too, and handing it to an identify run would have the run plan files that
    belong to a different torrent entirely.
    """
    cp = qbt.content_path(t)
    if cp:
        cp = Path(cp)
        if cp != save_path and cp.exists():
            return cp
    # Fall back to the torrent's own root component (qBittorrent names a multi-file
    # torrent's folder after the torrent; a single-file torrent has no folder and this
    # resolves to the file itself, which the identify listing handles).
    first = Path(by_index[pending[0]].name)
    return save_path / first.parts[0]


_SEASON_EP_RE = re.compile(r"[Ss](\d{1,3})[Ee]\d{1,4}")
_SEASON_DIR_RE = re.compile(r"(?i)\bseason[ ._-]*(\d{1,3})\b")


def _torrent_seasons(by_index):
    """Every season number this torrent's OWN file paths advertise, across all waves.

    Handed to `validate_plan` as `sibling_seasons` so its season-gap guard is not fooled
    by the holes chunking necessarily leaves: waves are sized by disk headroom, not by
    season, so a pack legitimately files S04 while S02/S03 are still queued behind it and
    the library reads `[1, 6]`. Read from the release's own numbering, so it closes a gap
    only when the torrent really does contain the missing season.
    """
    seasons = set()
    for f in by_index.values():
        for rx in (_SEASON_EP_RE, _SEASON_DIR_RE):
            for m in rx.finditer(f.name):
                n = int(m.group(1))
                if n > 0:
                    seasons.add(n)
    return seasons


def _wave_disk_files(content_root):
    """`[(relative_path, size)]` for what is on disk under a wave's content root.

    A chunked torrent is identified one wave at a time and only the wave exists on
    disk (earlier waves were unlinked as they filed; later ones are parked at
    priority 0), so this walk IS the wave's release list for the coverage contract.
    qBittorrent's incomplete-file suffix (`.!qB`) falls outside the media extensions
    and is accounted for as non-media, correctly.
    """
    root = Path(content_root)
    try:
        if root.is_file():
            return [(root.name, root.stat().st_size)]
        out = []
        for p in root.rglob("*"):
            if p.is_file():
                try:
                    out.append((str(p.relative_to(root)), p.stat().st_size))
                except (OSError, ValueError):
                    continue
        return out
    except OSError:
        return []


def _identify_wave(record, t, save_path, by_index, pending):
    """Identify and apply a whole WAVE in one headless AI run.

    Returns (plan_accepted, filed, planned, unresolved), where `filed` maps a download
    file's path key to the library destination now proven to hold it, `planned` is the
    path key of every download file the plan named at all, and `unresolved` is the
    release-relative list of files the plan did NOT account for. The caller needs ALL
    of them: a file in neither `filed` nor `planned` and not provably junk is a
    partial-plan gap and parks the release, while a file in `planned` but not `filed`
    had its apply or verify fail and must keep its bytes. Raises IdentifyUnavailable so
    the caller can abort the wave with everything still on disk.

    One run per wave, not one per file. Only the wave's files exist on disk -- qBittorrent
    does not preallocate a file parked at priority 0, and every earlier wave's files were
    unlinked as they were filed -- so the listing the run sees IS the wave. Three things
    follow, and all three are why per-file identification was the wrong unit:

      * **Cost.** A 175-file pack costs ~6 runs instead of 175; a 500 GB pack costs ~30
        instead of ~1,000. Per-file, the identify time alone runs to days and walks
        straight into a usage limit on every pack big enough to need chunking in the
        first place -- the exact torrents this path exists to serve.
      * **Correctness.** Every numbering call the prompt makes -- absolute vs seasoned,
        which season an episode extends, movie vs special -- is a judgment about a file
        *among its siblings*. A single episode handed over alone has none of that
        context.
      * **Junk.** A creditless opening or a sample is simply absent from a wave plan,
        which is how the whole-torrent path has always treated junk. Identified alone it
        instead produces an empty plan, which the validator rejects as a *failure* -- so
        every NCOP in a pack burned its retries and then landed in `chunk_failed`,
        ending an otherwise complete torrent as FAILED.
    """
    h = record["info_hash"]
    content_root = _chunk_content_root(t, save_path, by_index, pending)
    wave_id = f"{h}-w{min(pending)}"
    try:
        stored = identify.load_stored_plan(h)
        # The WHOLE release's file list, not just this wave's. Only the wave exists on
        # disk, so without this the structure analysis in the prompt sees a fifth of the
        # pack and the model makes numbering decisions about arcs it cannot see --
        # Monogatari's first wave is 32 of 103 files and hides the very folders whose
        # numbering conflicts.
        release_files = [getattr(f, "name", "") for f in by_index.values()
                         if getattr(f, "name", "")]
        plan, rationale = identify.run_identify(wave_id, str(content_root), log_fn=log,
                                                stored_plan=stored,
                                                settled=identify.settled_ok(stored),
                                                sibling_seasons=_torrent_seasons(by_index),
                                                release_files=release_files)
        journal.log_decision(wave_id, f"{record['name']} (wave of {len(pending)})",
                             (rationale or "(no rationale)") + "\n\nPLAN:\n" + _pretty(plan))
        # An empty wave plan over media is the same "nothing to place" verdict the
        # whole-torrent path accepts (§ An empty plan over media is a skip, not a
        # failure): a wave made up entirely of extras (creditless OP/ED, NCOP/NCED)
        # or repeats already on disk. Returning (True, {}, set(), []) lets the caller
        # drop every wave file through its existing "not in plan (junk)" branch,
        # rather than validate_plan rejecting the empty list and the wave burning
        # its retries into chunk_failed. Dual-audio stays loud for review.
        #
        # "Made up entirely of extras" is now decided by the SAME coverage classifier
        # the whole-release contract uses, not by a filename token alone: if even one
        # wave file is not provably junk, the wave is a partial plan wearing an empty
        # plan's clothes and the release parks. (The Doctor Who (2005) wave that lost
        # 38 files included `Rose` and four specials -- not extras, and one of them was
        # the episode the pack was named for.)
        if not plan.get("files") and pending:
            unresolved, _acct = plan_coverage.release_gaps(
                _wave_disk_files(content_root), [], content_root)
            all_extras = all(_looks_like_no_home_extra(by_index[i].name) for i in pending)
            if unresolved:
                log(f"  chunked wave plan is empty but {len(unresolved)} of its file(s) "
                    f"are not provably extras (e.g. {unresolved[0]!r}); parking the "
                    f"release with bytes intact.")
                return False, {}, set(), unresolved
            if _looks_like_dual_audio(record["name"]) and not all_extras:
                log(f"  chunked wave is a possible dual-audio upgrade with no plan; "
                    f"failing for review: {record['name']}")
                return False, {}, set(), []
            log(f"  chunked wave has media but no library home (extras/repeat); "
                f"dropping the wave cleanly.")
            return True, {}, set(), []
        library.validate_plan(plan, str(content_root),
                              sibling_seasons=_torrent_seasons(by_index),
                              serial_map=identify.serial_release_map(release_files),
                              release_name=record.get("name"))
        # THE PLAN-COVERAGE CONTRACT. Checked BEFORE apply_plan, because a plan that
        # names part of the wave is not permission to free the rest: the old
        # "not in plan (junk/duplicate); dropping" branch deleted 38 files / 60.9 GB
        # of Doctor Who (2005) exactly this way (HANDOFF 10.1/10.2). A gap parks the
        # whole release untouched; the caller may only free files this contract has
        # called accounted-for.
        _resolved = [d.get("src") for d in (plan.get("_deduped_dropped") or [])
                     if isinstance(d, dict)]
        unresolved, _acct = plan_coverage.release_gaps(
            _wave_disk_files(content_root), plan.get("files") or [], content_root,
            resolved_srcs=_resolved)
        if unresolved:
            log(f"  chunked wave plan leaves {len(unresolved)} file(s) unaccounted "
                f"(e.g. {unresolved[0]!r}); parking the release with bytes intact.")
            return False, {}, set(), unresolved
        applied = library.apply_plan(plan, wave_id)
    except identify.IdentifyUnavailable:
        raise
    except Exception as exc:                                              # noqa: BLE001
        log(f"  chunked wave identify/apply failed: {exc}")
        return False, {}, set(), []
    # Verified per entry, not per wave: the caller frees a file's only copy on the strength
    # of this, so one bad entry must not certify the other thirty-one.
    #
    # Matched by DESTINATION, never by source. An applied entry's `src` is the STAGING path
    # apply_plan copied from -- and is None outright for a destination that already existed
    # -- so it cannot be matched back to a download file. The validated plan is the bridge:
    # validate_plan leaves plan["files"] carrying the real download paths (including any it
    # healed), and `dst_rel` is the one identifier both sides state.
    verified_dsts = set()
    for entry in applied:
        ok, msg = library.verify_applied([entry])
        if ok:
            verified_dsts.add(_path_key(entry["dst"]))
        else:
            log(f"  chunked wave verify failed, keeping bytes: {msg}")
    filed, planned = {}, set()
    for f in plan.get("files") or []:
        src_key = _path_key(f.get("src"))
        planned.add(src_key)
        dst = _path_key(config.MEDIA_ROOT / Path(f.get("dst_rel") or ""))
        if dst in verified_dsts:
            filed[src_key] = dst
    # The wave's own verify pass is what authorizes the cleanup below, so it is also the
    # moment a verified manga volume may retire its covered chapters. Cache-only; never
    # blocks the wave.
    if any(str(f.get("dst_rel") or "").startswith("Comics/Manga/")
           for f in plan.get("files") or []):
        _manga_chapter_reconcile(plan)
    return True, filed, planned, []


def _media_relpath(abs_key):
    """Library-relative path for an absolute destination, or None if it is not under
    MEDIA_ROOT.

    The RELPATH is the part that survives eviction. It names the item in both roots -- the
    SSD and the mediafs mount -- which is what makes a filed claim checkable later, and an
    absolute SSD path is not: `~/Media` missing means EVICTED, not deleted (§4.1).
    """
    for root in (config.MEDIA_ROOT.resolve(), config.MEDIA_ROOT):
        try:
            return str(Path(abs_key).relative_to(root))
        except (ValueError, OSError):
            continue
    return None


def _still_in_library(rel):
    """Whether a library-relative path is STILL in the library, and how we know.

    Returns (present, mount_alive). Judged on the MOUNT first, because that is the only
    root that answers the question: a file absent from `~/Media` may simply be EVICTED to
    the pool (§4.1), and the mount is the merged view of local + pool. The SSD is consulted
    only as a second YES -- a file sitting on the SSD is in the library whatever the mount
    says -- so a mount that is down narrows what can be proven instead of inverting it.

    `mount_alive` is returned separately because "not present" and "could not look" must
    not be collapsed: absent-with-a-live-mount is content that is gone, while absent with
    the mount down is a fact about the mount (§4.2, §4.9). The caller says which it got.
    """
    mount_alive = False
    try:
        mount_alive = (config.MEDIAFS_MOUNT / "Shows").is_dir()
    except OSError:
        mount_alive = False
    for root in (config.MEDIAFS_MOUNT, config.MEDIA_ROOT):
        try:
            if (root / rel).exists():
                return True, mount_alive
        except OSError:
            continue
    return False, mount_alive


def _park_chunked_unfiled(record, client, unresolved, by_index):
    """Park a chunked pack whose wave plan left media unaccounted for.

    Distinct from `_park_chunked` (a disk-budget pause) and from `_free` (a harness-
    verified junk verdict): this is the terminal for a PARTIAL plan. It stops the
    torrent and zeroes every file's priority, but deletes nothing -- the download is
    the only copy of the unfiled files, and `_fail` leaves the local content in place.
    The unfiled list lands on the record so the next reader sees exactly which files
    the plan never covered instead of reconstructing it from the log.

    This is the branch Doctor Who (2005)'s first wave needed and did not have: its
    plan covered 32 files of the wave, the rest were "not in plan", and the wave
    freed 38 of them as junk. A partial plan must not be able to delete anything.
    """
    h = record["info_hash"]
    # A COLLISION WITH A DISPLACED DUPLICATE IS RESOLVABLE (2026-09-26). Before parking
    # for good, check whether the slots this wave cannot file are held by a separate pack
    # whose library footprint is a PROVEN uniform episode shift and every episode of
    # which this release's own filenames name. If so, supersede that displaced footprint
    # through the sanctioned purge path, retire the duplicate pack, and leave this wave
    # active so it retries against the freed slots. The gates live in `pack_conflict`;
    # anything less proven fails open to the historical park below. See
    # `pack_conflict`'s docstring for the two-release collision that motivated it.
    try:
        import pack_conflict                                              # noqa: PLC0415
        if pack_conflict.resolve_parked(record, client):
            return
    except Exception as exc:                                              # noqa: BLE001
        log(f"  pack-conflict resolution skipped ({exc}); parking as usual")
    try:
        qbt.set_file_priority(client, h, list(by_index), 0)
        qbt.stop(client, h)
    except Exception as exc:                                              # noqa: BLE001
        log(f"  could not stop {record['name']} while parking: {exc}")
    record["chunk_unfiled"] = [str(u) for u in sorted(unresolved)][:500]
    record["unfiled_count"] = len(unresolved)
    _fail(record, f"chunked: the wave plan left {len(unresolved)} file(s) unaccounted "
                  f"(e.g. {', '.join(str(u) for u in unresolved[:3])}); every byte is "
                  f"left on disk -- a partial plan is not permission to delete the rest")


def _finish_chunked(record, client, fls):
    """Retire a chunked pack whose every file is accounted for: remove the torrent, then
    settle the record as COMPLETED or FAILED depending on whether anything went unfiled."""
    qbt.remove(client, record["info_hash"], delete_files=True)
    failed = record.get("chunk_failed") or []
    if failed:
        # Some files were downloaded but never filed. The pack is gone from the disk
        # either way, so this is not recoverable by retrying the record -- say exactly
        # which files are missing and file the source under failed/ so the gap is visible
        # instead of reading as a clean COMPLETED.
        _fail(record, f"chunked: {len(failed)} of {len(fls)} files could not be "
                      f"filed into the library: {', '.join(failed[:10])}"
                      + (" ..." if len(failed) > 10 else ""))
        return
    # "Every file is accounted for" is not "the library got something". A pack whose whole
    # chunk_done set was INHERITED from a previous life -- carried onto a re-drop by
    # _carry_chunk_progress -- arrives here having fetched nothing, identified nothing and
    # filed nothing, and the only thing distinguishing it from a real success is that it
    # can name neither a destination it filed nor a file it deliberately declined. That is
    # exactly the "I cannot verify this, so accept it" fallback §4.4 forbids, and it is how
    # [MTBB] Monogatari Series (BD 1080p) reported COMPLETED three times over a library
    # that never held it. A pack that can prove neither half FAILS, visibly and recoverably.
    if not (record.get("chunk_filed") or {}) and not (record.get("chunk_dropped") or []):
        _fail(record, f"chunked: all {len(fls)} file(s) were marked done, but this record "
                      f"can name no destination it filed and no file it deliberately "
                      f"declined -- it retired on inherited progress it could not prove, "
                      f"so the library may hold none of it. Re-drop the .torrent to "
                      f"re-acquire (progress is now carried only where it is provable).")
        return
    record["status"] = journal.COMPLETED
    journal.write_record(record)
    _file_torrent_finished(record)
    log(f"COMPLETED chunked {record['name']}: "
        f"{len(record.get('chunk_filed') or {})} file(s) filed, "
        f"{len(record.get('chunk_dropped') or [])} declined; torrent removed.")


def sweep_chunked_idle(records, client, tmap=None):
    """Retire finished chunked packs and silence idle ones, before the slow per-torrent work.

    advance() walks the active torrents in one blocking pass, and a single chunked wave
    holds it for as long as an identify run takes -- minutes each, hours across a full
    sweep. A pack that finished its last wave therefore keeps its torrent registered, and
    seeding, until the loop happens to come back around to it. That delay is what makes an
    otherwise-correct engine look like it has stopped reaping: the torrent is done, the
    library has the files, and qBittorrent is still uploading it.

    This pass is cheap -- one file list per chunked torrent, no identify, no disk -- so it
    can run every cycle ahead of the expensive work and settle both cases immediately:
    remove a pack with nothing left to fetch, and stop one that is merely between waves.
    """
    if tmap is None:
        tmap = qbt.torrent_map(client)
    for record in list(records.values()):
        if not record.get("chunked") or record["status"] != journal.DOWNLOADING:
            continue
        h = record["info_hash"]
        t = tmap.get(h)
        if t is None:
            continue                       # absent: _advance_chunked re-adopts it
        fls = qbt.files(client, h)
        if not fls:
            continue
        done = set(record.get("chunk_done") or [])
        if not [f for f in fls if f.index not in done]:
            _finish_chunked(record, client, fls)
            continue
        # A wave that is fully downloaded has nothing left to fetch but is not yet filed,
        # so the torrent must stay registered while _advance_chunked works through it --
        # minutes per identify run. It must not seed through that. A pack with no active
        # wave at all is left alone here: _advance_chunked either enables its next wave or
        # parks it out of qBittorrent entirely, both this same cycle.
        by_index = {f.index: f for f in fls}
        active = [i for i in (record.get("chunk_active") or []) if i in by_index]
        if not active:
            continue
        fetching = [i for i in active if (by_index[i].progress or 0) < 1.0
                    and (by_index[i].priority or 0) > 0]
        if not fetching and t.state not in {"stoppedDL", "stoppedUP",
                                            "pausedDL", "pausedUP"}:
            try:
                qbt.stop(client, h)
                log(f"  stopped idle chunked torrent (not seeding between waves): "
                    f"{record['name']}")
            except Exception as exc:                                      # noqa: BLE001
                log(f"  could not stop idle chunked torrent {record['name']}: {exc}")


def _smallest_remaining(record):
    """Smallest not-yet-filed file size for a chunked torrent, read from its source
    `.torrent` (no qBittorrent round-trip). None if the sizes can't be read, in which
    case the caller falls back to re-adding the torrent to ask."""
    path = _ensure_source(record)
    if path is None:
        return None
    sizes = qbt.file_sizes_from_file(path)
    if not sizes:
        return None
    done = set(record.get("chunk_done") or []) | set(record.get("chunk_failed_idx") or [])
    remaining = [s for i, s in enumerate(sizes) if i not in done and s > 0]
    return min(remaining) if remaining else 0


def _park_out_of_qbt(record, smallest):
    """Mark a chunked torrent parked (out of qBittorrent) without touching the client.

    The one place a pack can be parked when it is already absent from qBittorrent: reading
    the `.torrent` told us its next wave cannot fit, so there is nothing to gain from
    re-adding it just to remove it again. Everything needed to resume is durable (the
    source `.torrent` and `chunk_done`), which is the same guarantee `_readopt_chunked`
    and `_park_chunked` rely on.
    """
    record["chunk_parked"] = True
    record["chunk_park_min_bytes"] = int(smallest)
    record["chunk_parked_at"] = time.time()
    record["chunk_active"] = []
    journal.write_record(record)


def _live_owned_keys(series_id):
    """The LIVE library DB's owned item_keys for a series, or None when the re-check cannot
    run (DB unavailable, or the plan predates `series_id`).

    Ingest re-checks an `owned` verdict against the live DB rather than trusting the
    searcher's snapshot (§ diagnosis 6.3.2): the library is write-once so an item the
    searcher saw as owned is still owned, but a plan that no longer resolves must not be
    trusted. Returning None makes the caller skip nothing — fail safe."""
    libdb = getattr(identify, "librarydb", None)
    if libdb is None or not series_id:
        return None
    try:
        conn = libdb.connect()
    except Exception:                                    # noqa: BLE001
        return None
    try:
        return set(libdb.owned_items(conn, int(series_id)).keys())
    except Exception:                                    # noqa: BLE001
        return None
    finally:
        try:
            conn.close()
        except Exception:                                # noqa: BLE001
            pass


def _skip_owned_indices(record, by_index):
    """File indices the searcher's stored plan declared OWNED or SKIP, so a chunked pack
    never downloads, files, or identifies them (§ diagnosis 6.3.2).

    Trust is bounded and fail-safe:

      * an `owned` file is RE-CHECKED against the live library DB at ingest time — the
        searcher's snapshot can be days old, and only an item the DB still owns is skipped;
      * a `skip` file (a non-media / delete file the searcher declined) is skipped only
        because ingest declines the same files anyway;
      * any file whose stored `src` does not map to a torrent file, or whose re-check
        cannot run, is left for the normal path — a skip is an optimisation, never a risk.

    Returns a set of file indices to treat as already done.
    """
    stored = identify.load_stored_plan(record.get("info_hash"))
    if not stored or not isinstance(stored, dict):
        return set()
    files = [f for f in (stored.get("files") or []) if isinstance(f, dict)]
    if not files:
        return set()

    def _index_for(src):
        if not src:
            return None
        base = Path(src).name.lower()
        hit = None
        for i, fi in by_index.items():
            if fi.name == src:
                return i
            if Path(fi.name).name.lower() == base:
                if hit is None:
                    hit = i
                else:
                    return None              # ambiguous basename -> do not guess
        return hit

    owned_keys = None
    series_id = stored.get("series_id")
    skip = set()
    for f in files:
        status = (f.get("status") or "").lower()
        if status not in ("owned", "skip"):
            continue
        i = _index_for(f.get("src"))
        if i is None:
            continue
        if status == "owned":
            if owned_keys is None:
                owned_keys = _live_owned_keys(series_id)
                if owned_keys is None:
                    return set()             # cannot re-check -> skip nothing (fail safe)
            key = identify.librarydb.item_key(f.get("type") or "episode",
                                              f.get("season"), f.get("number"))
            if key not in owned_keys:
                continue                     # not owned anymore -> normal path
        skip.add(i)
    return skip


def _advance_chunked(record, client, records, tmap=None):
    """Drive a chunked torrent: wait for the active wave to finish, ingest each of its
    files into the library, free their local space, then enable the next wave -- until every
    file is done, at which point the torrent is removed and the record completed."""
    if tmap is None:
        tmap = qbt.torrent_map(client)
    h = record["info_hash"]
    t = tmap.get(h)
    if t is None:
        # ACQUISITION PAUSE: a pack that is not in qBittorrent has nothing on disk to file,
        # so every branch below is about putting it BACK to fetch more -- unparking it,
        # re-adopting it. That is acquisition. Leave it exactly where it is; its
        # chunk_done set is durable and resuming picks up at the same file.
        if config.ACQUISITION_PAUSED:
            return
        # Absence is never "finished" for a chunked pack -- only this function retires one.
        # It means either that WE parked it between waves (§ _park_chunked), in which case
        # it comes back when its next wave fits, or that something else took it: a
        # qBittorrent restart, a crash, the share-limit reaper on a torrent admitted before
        # the exemption existed. Either way, resume from chunk_done rather than failing.
        if record.get("chunk_parked"):
            _unpark_chunked(record, client, records, tmap)
            return
        # Not parked, but also absent. The old behaviour re-added the torrent here on
        # every cycle, which for a disk already too full to hold the next wave meant
        # hundreds of packs were re-added only to be removed again next cycle -- an O(n^2)
        # qBittorrent churn that made a single cycle take hours and starved registration
        # of new drops. Decide by budget first, from the `.torrent` alone: if even the
        # smallest remaining file cannot fit, park it in place and skip the round-trip.
        smallest = _smallest_remaining(record)
        if smallest is not None and smallest > 0:
            wave_budget = min(config.torrent_chunk_bytes(),
                              _remaining_budget(records, client, tmap))
            if wave_budget < smallest:
                _log_chunk_starved(record, wave_budget, smallest)
                _park_out_of_qbt(record, smallest)
                log(f"Parked chunked {record['name']} (not in qBittorrent): next wave "
                    f"needs {smallest >> 20}MB and only {max(0, wave_budget) >> 20}MB is "
                    f"admittable.")
                return
        _readopt_chunked(record, client, tmap)
        return
    fls = qbt.files(client, h)
    if not fls:
        return                                     # transient; retry next cycle
    by_index = {f.index: f for f in fls}
    done = set(record.get("chunk_done", []))
    # Fold the searcher's already-owned / declined files into done so a chunked pack never
    # downloads them (§ diagnosis 6.3.2). Idempotent: once in done they stay done, and the
    # live-DB re-check runs until the fold lands in the journal.
    skipped = _skip_owned_indices(record, by_index) - done
    if skipped:
        done |= skipped
        record["chunk_done"] = sorted(done)
        # Recorded as DELIBERATELY DECLINED, not merely done: an already-owned file is a
        # verdict this pack reached, so a pack whose every file is owned has genuinely
        # finished and must not trip _finish_chunked's "proved nothing" gate. The re-check
        # against the live DB happens above, in _skip_owned_indices.
        record["chunk_dropped"] = sorted(set(record.get("chunk_dropped") or []) | skipped)
        journal.write_record(record)
        names = ", ".join(by_index[i].name for i in sorted(skipped))
        log(f"  chunked skip {len(skipped)} already-owned/declined file(s) in "
            f"{record['name']}: {names[:200]}")
    active = [i for i in record.get("chunk_active", []) if i in by_index]
    save_path = Path(getattr(t, "save_path", None) or record.get("download_root")
                     or config.INCOMING_DIR)

    # No active wave? Select and enable the next one (or finish if none remain).
    if not active:
        remaining = [f for f in fls if f.index not in done]
        if not remaining:
            _finish_chunked(record, client, fls)
            return
        # ACQUISITION PAUSE: enabling the next wave is a NEW download, not the filing of an
        # old one, so it is acquisition and it stops here. A wave already in flight below
        # is left to finish and be filed; a pack between waves simply waits. The pack keeps
        # its chunk_done set, so resuming picks up at the exact file it stopped on.
        if config.ACQUISITION_PAUSED:
            log(f"  {record['name']}: {len(remaining)} file(s) left, next wave NOT enabled "
                f"(acquisition paused).")
            return
        # Size the wave against what the disk can take RIGHT NOW, not just the static
        # chunk fraction. The fraction is a ceiling on how much disk torrent ingest may
        # occupy; it is not a promise that much is free, and a wave larger than the live
        # budget would breach MIN_FREE -- the exact failure chunking exists to prevent.
        wave_budget = min(config.torrent_chunk_bytes(), _remaining_budget(records, client, tmap))
        smallest = min(f.size for f in remaining)
        if wave_budget < smallest:
            _log_chunk_starved(record, wave_budget, smallest)
            # No wave can be enabled until the disk drains -- a wait measured in other
            # torrents completing, so hours or days. The pack has nothing to fetch in the
            # meantime and would sit in the list reporting "completed" the whole time, so
            # take it out of qBittorrent until its next wave fits.
            _park_chunked(record, client, fls, smallest)
            return
        batch = _pick_batch(fls, done, wave_budget)
        try:
            # Re-asserted on every wave, not just at admission: a torrent adopted from an
            # older record (or re-added by hand) would otherwise still carry the global
            # "remove when finished" rule and be reaped the moment this wave completes.
            qbt.exempt_from_share_limits(client, h)
            qbt.set_file_priority(client, h, batch, 1)
            qbt.resume(client, h)
        except Exception as exc:                                          # noqa: BLE001
            log(f"chunked wave enable failed for {record['name']}: {exc}; retry.")
            return
        record["chunk_active"] = batch
        # WHEN THIS PACK WAS LAST ASKED TO FETCH. Between waves a chunked torrent is
        # deliberately STOPPED (see the end of this function) while the finished wave is
        # filed, so qBittorrent's `last_activity` goes stale by design -- for as long as
        # filing takes, which is hours when identify is capped. Without this stamp the
        # next wave inherits that stale clock and `_abandon_stalled` destroys the whole
        # pack seconds after resuming it. Measured 2026-09-10: a wave enabled at 09:11:55
        # was killed at 09:12:16 as "stalled 8h with no progress (no seeders/peers)"
        # while the swarm had 450 seeders and the same torrent pulled 10 MB/s by hand.
        record["wave_started_at"] = time.time()
        journal.write_record(record)
        log(f"  chunked wave: {len(batch)} file(s), "
            f"{sum(by_index[i].size for i in batch) >> 30}GB of a "
            f"{wave_budget >> 30}GB budget "
            f"({len(done)}/{len(fls)} files already done).")
        return

    # Self-heal a wedged wave before judging its progress. An ACTIVE file parked at
    # priority 0 is one qBittorrent will never fetch, so the completion gate below can
    # never pass: the wave stalls forever, and because a chunked pack is exempt from the
    # share-limit reaper it sits there seeding indefinitely -- indistinguishable, from the
    # outside, from a torrent the engine has simply abandoned.
    #
    # The state is reachable whenever a wave is interrupted between _free() dropping a
    # file to priority 0 and the journal write that would have recorded it done: a crash,
    # a kill, a restart, or an exception mid-wave. The record then says "active" while
    # qBittorrent says "not wanted", and nothing reconciles them. qBittorrent's live
    # priority is the authority on what will actually be fetched, so re-arm from it rather
    # than trusting the record to be self-consistent.
    stalled = [i for i in active
               if (by_index[i].priority or 0) == 0 and (by_index[i].progress or 0) < 1.0]
    if stalled:
        try:
            qbt.set_file_priority(client, h, stalled, 1)
            qbt.exempt_from_share_limits(client, h)
            qbt.resume(client, h)
            # Re-arming is a fresh request to fetch, so the stall clock restarts with it.
            record["wave_started_at"] = time.time()
            journal.write_record(record)
            log(f"  re-armed {len(stalled)} stalled file(s) in {record['name']}'s wave "
                f"(parked at priority 0 while still active).")
        except Exception as exc:                                          # noqa: BLE001
            log(f"  could not re-arm {record['name']}'s stalled wave: {exc}; will retry.")
        return

    # Active wave in flight -- done only when every active file is fully downloaded.
    if not all((by_index[i].progress or 0) >= 1.0 for i in active):
        pct = int(100 * sum(by_index[i].progress or 0 for i in active) / max(1, len(active)))
        log(f"  {record['name']} chunked wave: {pct}%")
        if _abandon_stalled(record, t, client):
            return
        return

    # Every active file is on disk, so nothing remains to fetch until the next wave is
    # enabled. Filing the wave takes minutes; seeding through it is pure upload for no
    # benefit, so stop the torrent here and let the next wave's enable resume it.
    try:
        qbt.stop(client, h)
    except Exception as exc:                                              # noqa: BLE001
        log(f"  could not stop {record['name']} for ingest: {exc}")

    # Wave complete: ingest it as ONE unit, then free each file proven into the library.
    #
    # The free step is IRREVERSIBLE for a chunked torrent: it unlinks the local copy and
    # marks the file done, and those bytes are the only copy (the whole point of chunking
    # is that the pack does not fit the disk twice). So a file is freed ONLY once it is
    # proven to be in the library, or once a successful plan has explicitly declined it as
    # junk. Two failure modes are handled distinctly:
    #   * A usage limit aborts the whole wave -- nothing deleted, nothing marked done,
    #     chunk_active left intact -- and the next cycle re-ingests it.
    #   * A rejected plan or a verify mismatch keeps the bytes on disk for another attempt
    #     rather than deleting them, and is only given up on -- and recorded in
    #     chunk_failed -- after CHUNK_FILE_MAX_ATTEMPTS, with a per-file identify as the
    #     last resort before that.
    # Filing is idempotent, so re-running a wave is always safe: apply_plan detects the
    # pre-existing destination and skips it (§ Already-present media is a success).
    applied_all = []
    attempts = dict(record.get("chunk_attempts") or {})
    failed = list(record.get("chunk_failed") or [])
    failed_idx = set(record.get("chunk_failed_idx") or [])
    # WHY these two accumulate across waves: `chunk_done` alone cannot say WHY a file is
    # done -- filed, or deliberately declined -- and without that a later re-drop has
    # nothing to check its inherited progress against (see _carry_chunk_progress). They
    # are the evidence, so they are written on the same journal write as `chunk_done`.
    chunk_filed = dict(record.get("chunk_filed") or {})
    dropped = set(record.get("chunk_dropped") or [])

    def _record_filed(index, dst):
        """Note that `index` landed at `dst`, by the library-relative path that outlives
        eviction. A destination outside MEDIA_ROOT is not recorded rather than recorded
        wrong -- an unprovable claim must not read like a proven one (§4.4)."""
        rel = _media_relpath(_path_key(dst)) if dst else None
        if rel:
            chunk_filed[str(index)] = rel

    def _free(index):
        """Park the file at priority 0 (so qBittorrent won't re-fetch it) and unlink it."""
        try:
            qbt.set_file_priority(client, h, [index], 0)
        except Exception:                                                 # noqa: BLE001
            pass
        try:
            (save_path / by_index[index].name).unlink(missing_ok=True)
        except OSError:
            pass

    pending = [i for i in sorted(active) if i not in done]
    if not pending:                       # wave already fully filed; select the next one
        record["chunk_active"] = []
        journal.write_record(record)
        return

    # A file whose extension the library never ingests -- RARBG.txt, a tracker ad page, a
    # release .nfo, a screenshot -- cannot appear in any plan. Declining it here is both
    # cheaper than an AI run and load-bearing: a wave holding ONLY such files yields an
    # empty plan, which validate_plan rejects as a FAILURE rather than reading it as the
    # verdict it is, so the file burns its retries into chunk_failed and ends the whole
    # pack FAILED with every episode already correctly in the library. Torrents put their
    # junk last, so that all-junk final wave is the common shape, not a corner case. This
    # is the same drop the whole-torrent path gives anything outside its plan.
    junk = [i for i in pending
            if Path(by_index[i].name).suffix.lower() not in config.MEDIA_EXTENSIONS]
    if junk:
        for i in junk:
            log(f"  non-media file; dropping without an identify run: "
                f"{Path(by_index[i].name).name}")
            _free(i)
            done.add(i)
            dropped.add(i)
        pending = [i for i in pending if i not in set(junk)]
        if not pending:
            record["chunk_done"] = sorted(done)
            record["chunk_dropped"] = sorted(dropped)
            record["chunk_active"] = [i for i in active if i not in done]
            journal.write_record(record)
            log(f"  chunked wave was all non-media ({len(done)}/{len(fls)} files done).")
            return
    try:
        _register_periodically(records)
        wave_plan_ok, filed, planned, gaps = _identify_wave(record, t, save_path,
                                                            by_index, pending)
    except identify.IdentifyUnavailable as exc:
        log(f"  identify API unavailable mid-wave for {record['name']}; "
            f"wave left intact and will be re-ingested next cycle ({exc})")
        return

    if gaps:
        # A partial plan is never permission to free the files it does not name. Park
        # the whole release with every byte on disk; the record ends FAILED with the
        # unfiled list, and the .torrent lands in failed/ for review (HANDOFF 10.1).
        _park_chunked_unfiled(record, client, gaps, by_index)
        return

    for i in pending:
        # A wave takes minutes (one AI run per file); keep new drops registering through
        # it so the searcher's fresh releases are never stranded at the watch-folder top.
        _register_periodically(records)
        f = by_index[i]
        file_abs = save_path / f.name
        if not file_abs.exists():
            # NOT done. This file is in the wave, is not in the library, and its bytes are
            # gone -- which happens when a wave is interrupted after _free() unlinked the
            # file but before the journal recorded why. Marking it done here would retire
            # it silently: absent from the library, absent from chunk_failed, and invisible
            # to the re-drop path that exists to recover exactly this. The torrent is still
            # registered, so the bytes are recoverable -- re-arm the file and let a later
            # wave fetch it again.
            n = attempts.get(str(i), 0) + 1
            attempts[str(i)] = n
            if n <= config.CHUNK_FILE_MAX_ATTEMPTS:
                log(f"  chunked file missing on disk; re-fetching (attempt {n}): {f.name}")
                try:
                    qbt.set_file_priority(client, h, [i], 1)
                except Exception:                                         # noqa: BLE001
                    pass
                continue
            # Re-fetching it did not bring it back either. Record it as a visible gap
            # rather than a silent one.
            log(f"  giving up on {f.name} after {n} re-fetch attempts; it is not on disk "
                f"and not in the library.")
            done.add(i)
            failed.append(f.name)
            failed_idx.add(i)
            continue
        key = _path_key(file_abs)
        if key in filed:
            applied_all.append({"dst": filed[key]})
            _record_filed(i, filed[key])
            _free(i)
            done.add(i)
            continue
        if wave_plan_ok and key not in planned:
            # The coverage contract has already certified this file as junk or as an
            # intra-torrent duplicate whose surviving copy IS in the plan -- that is
            # the only way `_identify_wave` returns plan_ok with this file absent from
            # `planned`. A file the plan DID name but that is not in `filed` failed its
            # apply or verify, and falls through to the retry path below with its bytes
            # intact -- never deleted as junk.
            log(f"  not in plan (accounted junk/duplicate); dropping: {f.name}")
            _free(i)
            done.add(i)
            dropped.add(i)
            continue
        n = attempts.get(str(i), 0) + 1
        attempts[str(i)] = n
        if n < config.CHUNK_FILE_MAX_ATTEMPTS:
            log(f"  {f.name} not filed (attempt {n}); bytes kept for a retry next cycle.")
            continue
        # Last resort before giving up: identify this file on its own. A wave plan is
        # rejected as a unit, so one pathological file would otherwise take its whole
        # wave down with it.
        applied = _ingest_one_file(record, file_abs, f"{h}-{i}",
                                   sibling_seasons=_torrent_seasons(by_index))
        if applied is _NO_HOME:
            log(f"  no library home (extras/repeat); dropping: {f.name}")
            _free(i)
            done.add(i)
            dropped.add(i)
            continue
        if applied is _PARK:
            _park_chunked_unfiled(record, client, [f.name], by_index)
            return
        if applied:
            applied_all.extend(applied)
            for entry in applied:
                _record_filed(i, entry.get("dst"))
            _free(i)
            done.add(i)
            continue
        log(f"  giving up on {f.name} after {n} attempts; freeing its bytes "
            f"UNFILED (it will not be in the library).")
        _free(i)
        done.add(i)
        failed.append(f.name)
        failed_idx.add(i)

    record["chunk_done"] = sorted(done)
    record["chunk_attempts"] = attempts
    record["chunk_failed"] = failed
    record["chunk_failed_idx"] = sorted(failed_idx)
    record["chunk_filed"] = chunk_filed
    record["chunk_dropped"] = sorted(dropped)
    # A chunked pack used to finish with `applied` still empty, because this list was built
    # only to decide whether to poke Jellyfin. That made a pack that filed 103 files
    # indistinguishable in the journal from one that filed none -- the ambiguity the
    # Monogatari diagnosis had to reconstruct from the log to resolve. Accumulate it.
    record["applied"] = (record.get("applied") or []) + applied_all
    # Anything still unfiled stays active so the next cycle retries just those files.
    record["chunk_active"] = [i for i in active if i not in done]
    journal.write_record(record)
    if any(str(a.get("dst", "")).find("/Shows/") >= 0 or str(a.get("dst", "")).find("/Movies/") >= 0
           for a in applied_all):
        _jellyfin_rescan()
    log(f"  chunked wave ingested ({len(done)}/{len(fls)} files done).")


def _mark_downloaded(record, client):
    record["status"] = journal.DOWNLOADED
    journal.write_record(record)
    log(f"Download complete: {record['name']}")
    _advance_identify(record, client)      # keep moving in the same cycle


def _release_files_for_coverage(record, content_path):
    """The release's own file list for the coverage contract, or None when it cannot
    be enumerated (fail open -- a lookup failure must never park a release).

    Torrent metadata FIRST: the `.torrent` mirror names every file the release will
    ever write, and unlike a disk walk it stays complete for a chunked pack whose
    earlier waves were already unlinked. The walk is the fallback for a magnet whose
    metadata was never fetched, and for direct drops.
    """
    tp = _ensure_source(record)
    if tp is not None:
        got = qbt.file_list_from_file(tp)
        if got:
            return got
    if not content_path:
        return None
    root = Path(content_path)
    try:
        if root.is_file():
            return [(root.name, root.stat().st_size)]
        out = []
        for p in root.rglob("*"):
            if p.is_file():
                try:
                    out.append((str(p.relative_to(root)), p.stat().st_size))
                except (OSError, ValueError):
                    continue
        return out or None
    except OSError:
        return None


def _coverage_gaps(record, content_path, plan):
    """Release files the plan neither files nor provably accounts for, or [] fail-open.

    The plan's `_deduped_dropped` (an intra-torrent duplicate collapsed onto a copy
    that IS in the plan) counts as accounted-for. `_collision_parked` deliberately
    DOES NOT -- a same-slot file already on disk is not a junk verdict, so the release
    files it stands for stay unresolved and park the release (HANDOFF 10.2).
    """
    release = _release_files_for_coverage(record, content_path)
    if not release:
        return []
    resolved = [d.get("src") for d in (plan.get("_deduped_dropped") or [])
                if isinstance(d, dict)]
    unresolved, _accounted = plan_coverage.release_gaps(
        release, plan.get("files") or [], content_path or None, resolved_srcs=resolved)
    return unresolved


def _park_unfiled(record, unresolved, why):
    """Retire a record with the download LEFT ON DISK and the gap named.

    `_fail` already leaves the local content for inspection and files the `.torrent`
    under failed/; this adds the machine-readable unfiled list, so the next reader can
    see WHICH files the plan never accounted for instead of reconstructing it from the
    log. This is the terminal the Smurfs were missing: 365 files were deleted because
    a partial plan's cleanup did not know it was partial (HANDOFF 10.1).
    """
    record["unfiled"] = [str(u) for u in sorted(unresolved)][:500]
    record["unfiled_count"] = len(unresolved)
    _fail(record, why)
    log(f"  parked {len(unresolved)} unaccounted file(s); the download is intact at "
        f"{record.get('content_path')} and the .torrent is under failed/. Nothing was "
        f"deleted.")


def _looks_like_dual_audio(name):
    """Whether a torrent NAME advertises a dual/multi-audio track.

    This is the one empty-plan-over-media case that must NOT be silently skipped.
    Dual-audio is the searcher's explicit upgrade signal (its `is_upgrade()` re-queues
    a repeat when a dual-audio copy appears), so a dual-audio torrent that yields an
    empty plan is a real discrepancy — the write-once library would drop the upgrade —
    and deserves a failed/ entry for review rather than a quiet COMPLETED.
    """
    low = (name or "").lower()
    return any(m in low for m in ("dual audio", "dual-audio", "dual_audio",
                                  "multi-audio", "multi audio"))


def _advance_identify(record, client):
    h = record["info_hash"]
    content = record["content_path"]
    log(f"Identifying {record['name']} via a headless AI run...")
    try:
        stored = identify.load_stored_plan(h)
        if stored:
            log(f"  reusing the searcher's stored file->item map for {record['name']}")
            # Deterministic fast-path (§ diagnosis 6.4): for the common case — a
            # show/volume/chapter with a complete stored map, not owned, not a movie —
            # derive the plan without any model call. A None here falls back to the AI;
            # a plan that validate_plan rejects also falls back rather than failing the
            # torrent.
            fast = identify.fast_path_plan(h, content, stored)
            if fast is not None:
                try:
                    library.validate_plan(fast, content, release_name=record.get("name"))
                    # A stored map is the searcher's snapshot and may predate files the
                    # release actually carries. A coverage gap means the deterministic
                    # path cannot account for the download, so fall through to the AI --
                    # NOT park: the run may legitimately plan the remainder.
                    gaps = _coverage_gaps(record, content, fast)
                    if gaps:
                        raise library.PlanError(
                            f"stored map leaves {len(gaps)} release file(s) unaccounted "
                            f"(e.g. {gaps[0]!r})")
                except Exception as exc:                              # noqa: BLE001
                    log(f"  deterministic fast-path plan rejected ({exc}); "
                        f"falling back to the AI identify")
                else:
                    journal.log_decision(h, record["name"],
                                         "(deterministic fast-path; no AI call)\n\nPLAN:\n"
                                         + _pretty(fast))
                    record["plan"] = fast
                    record["status"] = journal.IDENTIFIED
                    journal.write_record(record)
                    log(f"Plan accepted (deterministic fast-path) for {record['name']} "
                        f"({fast.get('media_type')}, {len(fast.get('files', []))} files).")
                    _advance_stage(record, client)
                    return
        # The release's OWN file list travels with the run: it is what lets identify
        # compute the release->broadcast numbering, hand a 400-file pack a skeleton,
        # and require the plan to cover every file (HANDOFF 10.9). The chunked path
        # already passed its wave list; this one did not, so a whole-torrent pack got
        # none of the 10.9 machinery -- exactly the Smurfs shape.
        plan, rationale = identify.run_identify(
            h, content, log_fn=log, stored_plan=stored,
            settled=identify.settled_ok(stored),
            release_files=_release_files_for_coverage(record, content))
        journal.log_decision(h, record["name"],
                             (rationale or "(no rationale)") + "\n\nPLAN:\n"
                             + _pretty(plan))
        # A download that holds no ingestible media at all -- no archive/video
        # AND no packageable loose page images (a samples-only pack, a release of
        # only individual chapters the run declined) -- is a legitimate "nothing
        # to shelve" verdict, not a failure. The run returns an empty files list on
        # purpose here, but validate_plan rejects empty lists to stop the
        # "already-present" shortcut (§ Already-present media is a success). That
        # guard must stay, so we detect this case deterministically -- no media
        # extensions under the download -- and complete the drop cleanly (filed
        # under finished/) rather than failing it into failed/. Loose page images
        # (bare jpg/png) are NOT caught here: they are packageable, so a run that
        # plans them returns a non-empty list and never reaches this branch.
        has_media = identify.content_has_media(content)
        if not plan.get("files") and not has_media:
            plan["_skipped_no_media"] = True
            record["plan"] = plan
            record["status"] = journal.IDENTIFIED
            journal.write_record(record)
            log(f"  {record['name']} contains no library media; skipping "
                f"(nothing to place). Treated as success, filed under finished/.")
            _advance_stage(record, client)
            return
        # Media is present but the run returned an empty plan. Two legitimate
        # "nothing to place" verdicts land here — the download is already in the
        # library (a repeat/redownload), or it is extras with no library home (a
        # creditless OP/ED, a TV CM, an NCOP placeholder) — and both used to FAIL
        # because validate_plan rejects an empty plan to stop the "already-present"
        # shortcut. Treat them as the success they are: nothing is moved, the local
        # download is dropped, the .torrent is filed under finished/. The one
        # exception is a dual-audio upgrade, whose files would collide with the
        # lower-quality copies already on disk and be dropped by the write-once
        # library — that is a discrepancy to review, not a skip.
        if not plan.get("files") and has_media:
            if _looks_like_dual_audio(record["name"]):
                _fail(record, "empty plan over a possible dual-audio upgrade; "
                              "the write-once library would drop the upgrade — review")
                return
            verified, detail = _empty_plan_already_present(record, content)
            if not verified:
                _fail(record, f"empty plan claimed 'already present', but it is not: "
                              f"{detail}. Refusing to delete a complete download on an "
                              f"unverified claim; re-drop the .torrent to retry.")
                return
            plan["_skipped_repeated"] = True
            record["plan"] = plan
            record["status"] = journal.IDENTIFIED
            journal.write_record(record)
            log(f"  {record['name']} has media but no library home (already-present "
                f"or extras); treated as success, filed under finished/.")
            _advance_stage(record, client)
            return
        library.validate_plan(plan, content,
                              serial_map=identify.serial_release_map_for_content(content),
                              release_name=record.get("name"))
        # THE PLAN-COVERAGE CONTRACT. A plan is permission to delete THIS DOWNLOAD only
        # when it accounts for every medium in it. The Smurfs lost 365 files / 31 GB to
        # a 40-file plan whose cleanup deleted the whole root; Doctor Who (2005) lost 38
        # files / 60.9 GB to a wave whose "not in plan" branch freed them as junk. Both
        # are the same missing check, so it runs here for the whole-torrent path and in
        # full again in `_advance_cleanup` -- the moment before the bytes actually go.
        gaps = _coverage_gaps(record, content, plan)
        if gaps:
            _park_unfiled(
                record, gaps,
                f"plan accounts for {len(plan.get('files') or [])} file(s) but leaves "
                f"{len(gaps)} release file(s) unfiled (e.g. "
                f"{', '.join(str(g) for g in gaps[:3])}). A partial plan is not "
                f"permission to delete the rest; the release is parked intact.")
            return
    except identify.IdentifyUnavailable as exc:
        # The CLI could not run -- usage window exhausted, or no usable credential. Either
        # way the plan was never attempted. _fail() here would file the .torrent into
        # failed/ and abandon a download that is already 100% complete on disk, for a
        # condition that has nothing to do with the content. Leave the record at DOWNLOADED
        # and say so; the next cycle re-enters this same function with the download
        # untouched. Nothing else in the pipeline needs to know.
        log(f"  identify API unavailable; deferring {record['name']} ({exc})")
        return
    except Exception as exc:                                              # noqa: BLE001
        _fail(record, f"identify/validate failed: {exc}")
        return
    record["plan"] = plan
    record["status"] = journal.IDENTIFIED
    journal.write_record(record)
    log(f"Plan accepted for {record['name']} "
        f"({plan.get('media_type')}, {len(plan.get('files', []))} files, "
        f"owned={plan.get('owned')}).")
    _advance_stage(record, client)


def _advance_stage(record, client):
    try:
        applied = library.apply_plan(record["plan"], record["info_hash"])
    except Exception as exc:                                              # noqa: BLE001
        _fail(record, f"apply failed: {exc}")
        return
    record["applied"] = applied
    record["status"] = journal.STAGED
    journal.write_record(record)
    log(f"Applied {len(applied)} files into the library for {record['name']}.")
    _advance_verify(record, client)


def _advance_verify(record, client):
    ok, msg = library.verify_applied(record["applied"])
    if not ok:
        _fail(record, f"verification failed: {msg}")
        return
    # Journal VERIFIED *before* any deletion — this is the point of no regret.
    record["status"] = journal.VERIFIED
    journal.write_record(record)
    log(f"Verified {record['name']} present in library. Safe to clean up.")
    # A verified manga volume is the deterministic moment to retire the chapters the
    # cached volume map covers. Best-effort and cache-only; it never blocks on a network
    # call and never fails the ingest.
    _manga_chapter_reconcile(record.get("plan") or {})
    # Cascade straight into cleanup so the local copy is deleted NOW, in the same
    # pass — not deferred to a later cycle. Freeing this torrent's disk the moment
    # it is safe is what lets the next queued torrent start immediately.
    _advance_cleanup(record, client)


def _manga_chapter_reconcile(plan):
    """Hook: reconcile the manga series a just-verified plan filed a volume into.

    The module lives under `scripts/`, which is not on sys.path for the daemon; import it
    lazily so a missing/broken tool can never stop the ingest at module load. Every
    failure is swallowed with a log line -- reconciliation is bookkeeping, the filing is
    the point.
    """
    try:
        scripts = str(config.PROJECT_ROOT / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import chapter_volume_reconcile
        chapter_volume_reconcile.after_plan(plan, log_fn=log)
    except Exception as exc:                                              # noqa: BLE001
        log(f"manga chapter reconcile skipped: {exc}")


def _advance_cleanup(record, client):
    h = record["info_hash"]
    # THE LAST LOOK BEFORE THE IRREVERSIBLE STEP. The coverage contract ran at identify
    # time; this is the instant `qbt.remove(delete_files=True)` and
    # `_delete_local_content` would touch bytes, so it is re-checked against the
    # download as it exists NOW. A non-empty gap means a plan that was accepted for
    # what it names is still not permission to delete what it does not -- park with
    # the bytes intact instead (HANDOFF 10.1/10.2).
    gaps = _coverage_gaps(record, record.get("content_path"), record.get("plan") or {})
    if gaps:
        _park_unfiled(
            record, gaps,
            f"cleanup refused: the plan does not account for {len(gaps)} file(s) still "
            f"in the download (e.g. {', '.join(str(g) for g in gaps[:3])}). Leaving "
            f"every byte in place.")
        return
    # Comics are served by YACReader (which scans its own folders); only video
    # lands in Jellyfin. Fire the rescan if any file went to Shows/ or Movies/
    # (covers "mixed" torrents whose media_type isn't literally show/movie).
    plan_files = (record.get("plan") or {}).get("files") or []
    if any(str(f.get("dst_rel", "")).startswith(("Shows/", "Movies/")) for f in plan_files):
        _jellyfin_rescan()
    # Auto-maintain curated playlists (e.g. One Piece): judge each newly-placed
    # episode of an auto-show and append the keepers to its playlist. Best-effort
    # and fully self-contained — it never raises into the ingest path.
    try:
        import playlist_watch
        dst_rels = [str(f.get("dst_rel", "")) for f in plan_files]
        playlist_watch.consider_new_episodes(dst_rels, log_fn=log)
    except Exception as exc:                                              # noqa: BLE001
        log(f"playlist auto-update skipped: {exc}")
    # Free the local download. qBittorrent has usually already auto-removed the
    # torrent at completion (keeping the files), so its delete_files can't fire —
    # we delete the on-disk content directly. Best-effort remove-from-qBittorrent
    # too, in case it's somehow still listed.
    try:
        qbt.remove(client, h, delete_files=True)
    except Exception:                                                     # noqa: BLE001
        pass
    _delete_local_content(record.get("content_path"))
    # File the source .torrent into the finished/ folder (instead of deleting it).
    # It leaves the watch folder so it is never re-ingested, but survives so it can
    # be re-dropped to redownload — nothing is marked "done forever."
    _file_torrent_finished(record)
    record["status"] = journal.COMPLETED
    journal.write_record(record)
    log(f"COMPLETED {record['name']}. Local storage freed; source filed under finished/.")


def _fail(record, reason, refused=False):
    """Retire a record. `refused=True` when the system DECLINED it on purpose.

    A refusal and a failure are both terminal and neither touches the library, so they
    shared a status for a long time -- which is why 13 of the 71 records the owner
    complained about were the pipeline working correctly (§4.88). They are separated now
    so `failed` means "something went wrong" and nothing else.
    """
    record["status"] = journal.REFUSED if refused else journal.FAILED
    record["error"] = reason
    # File the source .torrent into failed/ so a dead torrent is out of the watch
    # folder and never looks like it is still "in the queue." (Updates torrent_path
    # before the single journal write below.) The local download is left in place
    # for inspection; nothing pre-existing in the library is ever touched.
    _file_torrent_failed(record)
    journal.write_record(record)
    label = "REFUSED" if refused else "FAILED"
    log(f"{label} {record['name']}: {reason} (nothing pre-existing was touched; "
        f"local download left for inspection; source .torrent filed under failed/).")


# Statuses whose payload is still on local disk (or in qBittorrent) and not yet safely
# in the library -- these are the ones an oversized purge must remove.
_OVERSIZED_PURGE_STATUSES = {
    journal.QUEUED, journal.DOWNLOADING, journal.DOWNLOADED,
    journal.IDENTIFIED, journal.STAGED,
}


def _largest_file(record, client, tmap, info_hash):
    """Largest single file in a torrent's payload, or None if unknown.

    Prefers the live qBittorrent file list (most accurate, and covers a download that
    has already started); falls back to the `.torrent` metadata for a torrent that has
    not been added to qBittorrent yet (QUEUED).
    """
    if info_hash in tmap:
        try:
            sizes = [int(f.size) for f in qbt.files(client, info_hash) if f.size]
            if sizes:
                return max(sizes)
        except Exception:                                                # noqa: BLE001
            pass
    tp = Path(record.get("torrent_path") or "")
    if tp.exists():
        try:
            sizes = qbt.file_sizes_from_file(tp)
            if sizes:
                return max(sizes)
        except Exception:                                                # noqa: BLE001
            pass
    return None


def purge_oversized(records, client, tmap=None, dry_run=False):
    """Fail and remove every active torrent whose largest single file exceeds the MEGA
    single-file cap (`config.MAX_SINGLE_FILE_BYTES`). A file that big can never be stored
    on one MEGA account, so the torrent will only burn disk downloading it. Removes it
    from qBittorrent (deleting its files), deletes the local download, and files the
    `.torrent` under failed/. Idempotent: once a record is FAILED it is skipped.

    Returns the number of torrents purged.
    """
    tmap = tmap or {}
    purged = 0
    for info_hash, record in list(records.items()):
        if record.get("status") not in _OVERSIZED_PURGE_STATUSES:
            continue
        biggest = _largest_file(record, client, tmap, info_hash)
        if biggest is None or biggest <= config.MAX_SINGLE_FILE_BYTES:
            continue
        if dry_run:
            log(f"WOULD purge {record.get('name')}: a single file is {biggest} bytes "
                f"(> {config.MAX_SINGLE_FILE_BYTES} cap).")
            purged += 1
            continue
        if info_hash in tmap:
            try:
                qbt.remove(client, info_hash, delete_files=True)
            except Exception as exc:                                     # noqa: BLE001
                log(f"purge-oversized: qBittorrent remove failed for "
                    f"{record.get('name')}: {exc}")
                continue
        _delete_local_content(record.get("content_path"))
        _fail(record, f"purged: a single file ({biggest} bytes) exceeds the "
                      f"{config.MAX_SINGLE_FILE_BYTES}-byte MEGA cap")
        purged += 1
    if purged:
        log(f"Purged {purged} oversized torrent(s) "
            f"(largest single file > {config.MAX_SINGLE_FILE_BYTES >> 30} GiB).")
    return purged


# --- helpers -----------------------------------------------------------------

def _dir_size(path):
    """Total bytes under a path (file or directory)."""
    if not path:
        return 0
    p = Path(path)
    try:
        if p.is_file():
            return p.stat().st_size
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    except OSError:
        return 0


def _delete_local_content(content_path):
    """Delete a finished torrent's downloaded files. Confined to our own download
    dirs — the local INCOMING_DIR and the library-drive overflow LIBRARY_INCOMING_DIR
    — so a bad content_path can never reach outside our own download areas (and, in
    particular, can never touch the live library tree that sits alongside the
    overflow dir on the same volume)."""
    if not content_path:
        return
    p = Path(content_path).resolve()
    allowed = [config.INCOMING_DIR.resolve(), config.LIBRARY_INCOMING_DIR.resolve()]
    if not any(root == p or root in p.parents for root in allowed):
        log(f"Refusing to delete content outside {allowed}: {p}")
        return
    try:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()
    except OSError as exc:
        log(f"Warning: could not delete local content {p}: {exc}")
        return

    # Prune now-empty parent dirs up to (not including) the incoming root. A
    # single-file-in-a-folder torrent's content_path is the FILE, so unlink leaves
    # the empty title folder behind -- this removes it (and any .DS_Store junk that
    # would otherwise block the rmdir), climbing until a non-empty parent or the root.
    root = next((r for r in allowed if r == p or r in p.parents), None)
    if root is None:
        return
    parent = p.parent
    while parent != root and root in parent.parents:
        junk = parent / ".DS_Store"
        try:
            if junk.exists():
                junk.unlink()
            parent.rmdir()          # only succeeds if now empty
        except OSError:
            break                    # non-empty (real siblings) or gone -> stop
        parent = parent.parent


def _file_torrent(record, dest_dir, label):
    """Move a source `.torrent` into one of the state subfolders and point the
    record's `torrent_path` at its new home. Idempotent: a file already in
    `dest_dir` (or gone) is left alone. The watch folder's top level is the only
    place `find_drop_files` scans, so a filed torrent never re-registers on its
    own -- and moving a `.torrent` back to the top level is still the retry signal.
    """
    tp = Path(record.get("torrent_path") or "")
    try:
        if not tp.exists():
            return False                             # already gone; nothing to file
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / tp.name
        if tp.resolve() == dest.resolve():
            return False                             # already filed there
        os.replace(tp, dest)       # same iCloud volume; atomically overwrites any prior copy
        record["torrent_path"] = str(dest)
        return True
    except OSError as exc:
        log(f"Warning: could not file {tp.name} under {label}/: {exc}")
        return False


def _file_torrent_finished(record):
    """Move a fully-ingested source .torrent into finished/. The file survives (so
    re-dropping it into the watch folder triggers a fresh redownload) but is no
    longer scanned as new work. finished/ is a subdir of the watch folder, and
    find_drop_files scans only its top level, so a filed torrent never
    re-registers on its own."""
    if _file_torrent(record, config.FINISHED_DIR, "finished"):
        log(f"Filed source .torrent under finished/: {record['name']}")


def _file_torrent_failed(record):
    """Move a FAILED source .torrent into failed/ (a sibling of finished/, likewise
    not scanned for new work). This is the visibility fix for 'stuck in the queue
    forever': a failure is filed away for inspection instead of lingering in the
    watch folder indistinguishable from a queued drop. Re-droppable (drop it back
    into the watch folder), though retrying still requires clearing the FAILED
    journal record — a failed torrent is deliberately not auto-retried."""
    if _file_torrent(record, config.FAILED_DIR, "failed"):
        log(f"Filed failed .torrent under failed/: {record['name']}")


def _file_unparseable_torrent(path, exc):
    """File a `.torrent` whose bencode will not parse into failed/.

    A torrent that cannot be parsed has no info hash, so it can never get a
    journal record: without this it would sit at the top of the watch folder and
    be re-skipped every cycle forever, indistinguishable from queued work except
    for one log line a minute. Filing it to failed/ makes the state visible on the
    filesystem, where every other terminal state already lives.

    The cause is almost always a truncated file (an interrupted download or a
    partial iCloud sync), so the parse fails at a byte offset past the end of the
    file. That is not transiently recoverable by re-dropping: moving the file back
    into the watch folder re-runs the same parse and it lands back in failed/. A
    torrent that keeps returning to failed/ is truncated -- replace the file rather
    than re-drop it.

    Hash-named drops never reach here: the filename IS the info hash, so
    `_recover_truncated_torrent` rebuilds the drop as a magnet and lets qBittorrent
    fetch the real metadata from the swarm. What lands in failed/ by this path is a
    truncated drop with no hash to recover from.

    A drop iCloud is still writing is exempt: parsing is only judged final once the
    file has been untouched for config.UNPARSEABLE_GRACE_SEC.
    """
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return                                       # vanished mid-scan; nothing to file
    if age < config.UNPARSEABLE_GRACE_SEC:
        log(f"Unreadable torrent {path.name} ({exc}); still within the "
            f"{config.UNPARSEABLE_GRACE_SEC}s sync grace -- will retry next cycle.")
        return
    try:
        config.FAILED_DIR.mkdir(parents=True, exist_ok=True)
        dest = config.FAILED_DIR / path.name
        os.replace(path, dest)       # same iCloud volume; atomically overwrites any prior copy
        log(f"Filed unreadable .torrent under failed/: {path.name} ({exc}). "
            f"The file is truncated or corrupt and carries no hash-named recovery "
            f"path -- replace it rather than re-dropping it.")
    except OSError as os_exc:
        log(f"Warning: could not file unreadable .torrent {path.name} under failed/: {os_exc}")


def _pretty(obj):
    import json
    return json.dumps(obj, indent=2, ensure_ascii=False)


def _jellyfin_rescan():
    if not config.JELLYFIN_URL:
        return
    try:
        import requests
        requests.post(
            f"{config.JELLYFIN_URL.rstrip('/')}/Library/Refresh",
            params={"api_key": config.JELLYFIN_API_KEY},
            timeout=10,
        )
        log("Triggered Jellyfin library rescan.")
    except Exception as exc:                                              # noqa: BLE001
        log(f"Jellyfin rescan skipped: {exc}")


# --- main loop ---------------------------------------------------------------

_BLOCKLIST_PATH = config.STATE_DIR / "blocklist.json"


def _block_key(name: str) -> str:
    s = re.sub(r"\((?:19|20)\d{2}\)", " ", name or "")
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _blocked_keys() -> set:
    """Normalized titles the owner has purged, from the searcher's blocklist.

    Read fresh each cycle and never cached: the owner edits this file when they purge, and
    a stale copy is exactly the failure this guard exists to prevent.
    """
    try:
        raw = json.loads(_BLOCKLIST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    names = raw.get("titles", []) if isinstance(raw, dict) else raw
    return {k for k in (_block_key(n) for n in names if isinstance(n, str)) if k}


def retire_blocked(records, client) -> int:
    """Retire journal records for titles the owner has purged, and drop their torrents.

    WHY INGEST NEEDS THIS AND THE SEARCHER'S BLOCKLIST IS NOT ENOUGH
        The searcher's blocklist filters `load_wants`, which stops a purged title being
        SEARCHED for again. It says nothing about work already in this queue, and this queue
        is self-perpetuating: `_advance_chunked` re-adopts any chunked pack that is missing
        from qBittorrent, because absence normally means a crash or a restart rather than a
        deletion. So purging a title's files put it straight back — qBittorrent went from 0
        to 15 torrents within minutes of a reboot, every one of them re-added from a journal
        record, several of them titles that had just been purged.

        The blocklist is therefore consulted HERE too, once per cycle, before anything can
        re-adopt: a blocked record is refused (a deliberate decline, not a failure), and its
        torrent and any files it still holds are removed. Refused is terminal, so it is never
        re-adopted and never re-registered.
    """
    blocked = _blocked_keys()
    if not blocked:
        return 0
    retired = 0
    for record in list(records.values()):
        if record.get("status") in (journal.COMPLETED, journal.FAILED, journal.REFUSED):
            continue
        key = _block_key(record.get("name", ""))
        if not key:
            continue
        # Whole-phrase match only: a blocked "Nisekoi"
        # must catch "Nisekoi S1+S2 [BDrip]" without a bare word ever swallowing an
        # unrelated title that merely contains it.
        if not any(key == b or key.startswith(b + " ") or f" {b} " in f" {key} "
                   for b in blocked):
            continue
        h = record.get("info_hash")
        if h and client is not None:
            try:
                qbt.remove(client, h, delete_files=True)
            except Exception as exc:                                   # noqa: BLE001
                log(f"  could not remove blocked torrent {record['name']}: {exc}")
        record["status"] = journal.REFUSED
        record["error"] = ("owner purged this title; it is on the ingest "
                           "blocklist and must not be re-acquired")
        journal.write_record(record)
        retired += 1
        log(f"RETIRED (owner-purged) {record['name']}")
    if retired:
        log(f"Retired {retired} record(s) for purged titles; their torrents were removed.")
    return retired


def cycle():
    # Before the replay, not after: a compaction here shrinks the file load_records() is
    # about to read. Costs one stat() on every cycle that is under threshold, which is
    # nearly all of them. Safe at this point specifically because journal writes are
    # single-threaded and single-instance (the lock file), so nothing can be appending
    # between the read and the atomic swap.
    stats = journal.compact_if_needed()
    if stats:
        log(f"Journal compacted: {stats['lines_before']:,} snapshots -> "
            f"{stats['records']:,} torrents, "
            f"{stats['bytes_before'] / 1048576:.1f} MB -> {stats['bytes_after'] / 1048576:.1f} MB.")

    records = journal.load_records()
    if config.ACQUISITION_PAUSED:
        log("ACQUISITION PAUSED: no drops will be registered and no downloads started. "
            "The filing path (identify -> stage -> verify -> cleanup) still runs. "
            "Set ACQUISITION_PAUSED=0 to resume.")
    if not library_ready():
        log("SSD library root not available; idling.")
        return config.IDLE_INTERVAL_SEC
    if not tailscale_up():
        log("Tailscale down; not starting/continuing downloads. Idling.")
        return config.IDLE_INTERVAL_SEC

    # Reconcile completed records against the remote inventory before trusting them: a
    # torrent whose files were evicted locally without landing on MEGA is re-queued rather
    # than left as a stale "completed" lie. Cheap (cached inventory read) and non-
    # destructive, so it runs every cycle.
    try:
        re_queued = reconcile.reconcile(records, log_fn=log)
        if re_queued:
            log(f"Reconciled {re_queued} stale completion(s); re-queued their .torrent files")
    except Exception as exc:                                            # noqa: BLE001
        log(f"reconciliation skipped: {exc}")

    try:
        client = qbt.connect()
    except Exception as exc:                                              # noqa: BLE001
        log(f"qBittorrent unavailable: {exc}; idling.")
        return config.IDLE_INTERVAL_SEC

    # The auto-remove-on-completion rule is the whole-torrent path's completion signal, not
    # a preference, and one wrong value on it (RemoveWithContent) deletes downloads before
    # they are filed. Re-assert it every cycle rather than trusting it was set once.
    try:
        qbt.assert_share_limit_policy(client, log_fn=log)
    except Exception as exc:                                              # noqa: BLE001
        log(f"could not verify qBittorrent's share-limit policy: {exc}")

    config.INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    # The library-drive overflow dir is created lazily too (the SSD library root is present —
    # cycle() bailed above if not). Overflow torrents land here instead of failing.
    config.LIBRARY_INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    # ACQUISITION PAUSE (2026-09-03): registration and admission are the only two places a
    # cycle takes on NEW work. Skipping just these two leaves the whole filing path below
    # untouched, so a download that is already on disk still identifies, stages, verifies
    # and frees its space -- the disk drains while nothing refills it.
    if not config.ACQUISITION_PAUSED:
        register_new_torrents(records)
    # Cleanup, not acquisition, so it runs even while paused: sources stranded in
    # queued//ingesting/ by a terminal record or an iCloud " 2" duplicate are filed
    # out of the state folders here (HANDOFF 10.6). Never deletes and never touches a
    # source it cannot parse.
    try:
        sweep_orphan_sources(records)
    except Exception as exc:                                              # noqa: BLE001
        log(f"orphan source sweep skipped: {exc}")

    # One bulk snapshot of every torrent in qBittorrent, threaded through the whole
    # cycle. Resolving a torrent used to be one HTTP request per hash, which with a
    # thousand-plus chunked torrents made `_remaining_budget` (itself called once per
    # chunked torrent) an O(n^2) request storm -- a single cycle took hours and new
    # drops sat unregistered at the top of the watch folder the whole time.
    tmap = qbt.torrent_map(client)

    # Retire anything the owner has purged BEFORE the advance sweep can re-adopt it. The
    # queue is self-perpetuating -- `_advance_chunked` re-adds any chunked pack missing from
    # qBittorrent, because absence normally means a restart rather than a deletion -- so
    # without this a purged title reappears within one cycle of its files being deleted.
    retire_blocked(records, client)

    # Purge any active torrent whose largest single file exceeds the MEGA cap before it
    # can download further: a file that big can never be stored, so it would only waste
    # disk. Idempotent and cheap (only scans active records).
    purge_oversized(records, client, tmap)

    # Cheap, before the blocking per-torrent work: retire chunked packs that have nothing
    # left to fetch and stop the ones merely waiting between waves, so neither lingers in
    # qBittorrent seeding for however long a full advance() sweep takes.
    sweep_chunked_idle(records, client, tmap)

    # Fill any space that is already free — from a previous cycle's completions,
    # from newly registered drops, or from the disk simply having room — before
    # doing the slow per-torrent work, so downloads start promptly.
    if not config.ACQUISITION_PAUSED:
        admit_downloads(records, client, tmap)

    # Advance every already-active torrent. A finished download cascades all the
    # way through identify -> stage -> verify -> cleanup in this one call, so its
    # local copy is deleted the moment it is safe. A slow download never blocks a
    # finished one; each is processed independently.
    active = sorted((r for r in records.values() if r["status"] in ACTIVE),
                    key=lambda r: r.get("created_at", ""))
    last_register = time.time()
    for record in active:
        advance(record, client, records, tmap)
        # The instant a torrent reaches COMPLETED its local copy is gone and disk
        # is freed — immediately refill that space with whatever queued torrents
        # now fit, rather than waiting for the whole batch to drain first.
        if record["status"] == journal.COMPLETED and not config.ACQUISITION_PAUSED:
            admit_downloads(records, client, tmap)
        # A large chunked backlog makes one advance() sweep take a long time; re-queue
        # any drops that landed during it on a timer rather than leaving a `.torrent`
        # sitting at the top of the watch folder for the whole sweep. Cheap (one iCloud
        # directory read) and idempotent, so a fresh drop is registered within
        # REGISTER_REFRESH_SEC even while the queue is full and chewing.
        if (time.time() - last_register >= config.REGISTER_REFRESH_SEC
                and not config.ACQUISITION_PAUSED):
            register_new_torrents(records)
            last_register = time.time()

    # Register again now that the slow per-torrent work is done: a `.torrent` dropped
    # while the advance() sweep above was still churning (a large chunked backlog makes
    # one sweep take many minutes) is picked up at the end of *this* cycle instead of
    # waiting for the next one. Idempotent — the start-of-cycle call already filed
    # everything it saw, so this only catches drops that landed in between.
    if not config.ACQUISITION_PAUSED:
        register_new_torrents(records)

    # Poll briskly while anything is active or waiting; otherwise idle.
    busy = any(r["status"] in ACTIVE or r["status"] == journal.QUEUED
               for r in records.values())
    return config.POLL_INTERVAL_SEC if busy else config.IDLE_INTERVAL_SEC


def _run_purge_oversized(dry_run: bool) -> int:
    """One-shot: connect to qBittorrent, load the journal, and purge every active torrent
    whose largest single file exceeds the MEGA cap. Returns the purge count."""
    try:
        client = qbt.connect()
    except Exception as exc:                                             # noqa: BLE001
        log(f"qBittorrent unavailable; cannot purge: {exc}")
        return 0
    records = journal.load_records()
    tmap = qbt.torrent_map(client)
    return purge_oversized(records, client, tmap, dry_run=dry_run)


def main():
    ap = argparse.ArgumentParser(description="Torrent-Ingest daemon.")
    ap.add_argument("--purge-oversized", action="store_true",
                    help="purge queued/ingesting torrents whose largest single file "
                         "exceeds the MEGA cap (and delete their downloaded files), "
                         "then exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --purge-oversized: report only, delete nothing")
    args = ap.parse_args()

    acquire_lock()
    if args.purge_oversized:
        log("Torrent-Ingest: purge-oversized (dry-run)" if args.dry_run
            else "Torrent-Ingest: purge-oversized")
        _run_purge_oversized(args.dry_run)
        return

    log("Torrent-Ingest daemon started.")
    # Say out loud whether the acceptance gate is armed. §4.120's whole cost was that a
    # load-bearing check stopped being reached and NOTHING said so for four days, so the
    # one thing this must never do is start quietly ungated.
    if not config.ACCEPTANCE_GATE:
        log("acceptance gate DISABLED by config (ACCEPTANCE_GATE=0); magnets AND "
            ".torrents will be admitted without a file-list check (§4.120).")
    elif acceptance_gate is None:
        log(f"acceptance gate UNAVAILABLE: {_GATE_IMPORT_ERROR!r}. Magnets and .torrents "
            f"will be admitted unchecked; fleet_health will raise this as an ACTION "
            f"(§4.120).")
    else:
        log("acceptance gate armed (file-list check active on both drop paths: "
            "magnet and .torrent).")
    # Assert the plan API still behaves as its callers require. This repo's own ingest is
    # one of them, but the load-bearing reason is the OTHER one: the YouTube ingest
    # (~/Developer/Media-Orchestrator/YouTube-Downloader) calls validate_plan/apply_plan/verify_applied
    # directly, so a change here breaks it silently -- its plans just start being rejected
    # on a machine nobody is watching. Reported and NOT fatal: a broken cross-repo
    # contract must never stop torrents from ingesting.
    try:
        import contract
        contract.assert_ok(log_fn=log)
    except Exception as exc:                                              # noqa: BLE001
        log(f"contract self-check could not run: {exc!r}")
    while True:
        try:
            nap = cycle()
        except KeyboardInterrupt:
            log("Interrupted; exiting.")
            break
        except Exception as exc:                                          # noqa: BLE001
            log(f"Cycle error (continuing): {exc!r}")
            nap = config.IDLE_INTERVAL_SEC
        time.sleep(nap)


if __name__ == "__main__":
    main()
