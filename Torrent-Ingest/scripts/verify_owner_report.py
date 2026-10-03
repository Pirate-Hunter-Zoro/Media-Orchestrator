#!/usr/bin/env python3
"""Library-wide acceptance report (HANDOFF 10.10F).

WHY THIS IS GENERIC. The first cut of this script named the five artifacts (a show
folder, two poster hashes, the Smurfs info hash). That is incident evidence, not a
check: it cannot see the same fault on another show, and it rots when the incident is
gone. The durable guards are the registered tests, whose incident details live in their
fixtures and commit messages. This report instead computes the INVARIANTS the five
faults violated, across the whole library, and is safe to keep running forever:

  1. sidecar identity  -- no `tvshow.nfo` whose `enddate` precedes its own premiere
                           (the TZ shape: a stale other-show end date beside a
                           corrected premiere);
  2. manga shelf        -- no `vNNNN` mislabels, no chapter covered by an owned volume,
                           and no volume number held in both editions, across EVERY
                           two-tier series (computed by the reconciler's own functions,
                           so it cannot disagree with an apply);
  3. large releases     -- completed journal records that collapsed duplicate
                           destinations (content-verification review), and terminal
                           records that parked unaccounted files.

READ-ONLY. Always exits 0: it is a report, and `verify_fleet.sh` prints it as an
advisory so a queued purge or a parked pack cannot block a deploy. The blocking
versions are the tests registered in `verify_fleet.sh`.

    python3 scripts/verify_owner_report.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config                                                        # noqa: E402
import journal                                                       # noqa: E402
import library                                                       # noqa: E402
import chapter_volume_reconcile as cvr                               # noqa: E402
import audit_volume_chapter_coverage as audit                        # noqa: E402

results = []


def line(name, status, detail=""):
    results.append((name, status, detail))
    print(f"{status:7s} {name}{(': ' + detail) if detail else ''}")


def _tag(text, tag):
    m = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", text, re.I | re.S)
    return (m.group(1).strip() if m else "")


def _year(value):
    m = re.search(r"(\d{4})", str(value or ""))
    return int(m.group(1)) if m else None


def _shows_root():
    root = config.MEDIAFS_MOUNT / "Shows"
    return root if root.is_dir() else config.SHOWS_ROOT


def _resident(rel):
    """True when the path is still visible in the library (mount) or on the SSD.

    The inventory keeps a purged path until Media-Syncer reconciles, so a purge that
    already happened can look "still present" for a minute. The mount is the owner's
    view and the SSD is the local truth; absence from both means the bytes are gone,
    whatever the inventory still says.
    """
    for root in (config.MEDIAFS_MOUNT, config.MEDIA_ROOT):
        try:
            if (root / rel).exists():
                return True
        except OSError:
            return True
    return False


def _queued_deletions():
    """Paths already queued for the reaper's purge (queue + in-flight batch)."""
    out = set()
    q = config.MEDIAFS_DELETIONS_QUEUE
    for p in (q, q.with_name(q.name + ".processing")):
        try:
            for raw in p.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(item, dict) and item.get("path"):
                    out.add(str(item["path"]))
        except OSError:
            continue
    return out


def check_sidecar_identity():
    """A sidecar whose end date precedes its own premiere is self-contradictory."""
    root = _shows_root()
    bad = []
    for nfo in sorted(root.glob("*/tvshow.nfo")):
        try:
            text = nfo.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        start = _year(_tag(text, "premiered") or _tag(text, "year"))
        end = _year(_tag(text, "enddate"))
        if start and end and end < start:
            bad.append(f"{nfo.parent.name} (end {end} < start {start})")
    if bad:
        line("sidecar identity", "FAIL", f"{len(bad)} contradictory sidecar(s): "
             + "; ".join(bad[:3]))
    else:
        line("sidecar identity", "PASS")


def _manga_series():
    owned = cvr.owned_manga()
    return {label: files for label, files in owned.items() if files}


def check_manga():
    owned = _manga_series()
    if not owned:
        line("manga shelf", "SKIP", "shelf not enumerable")
        return
    queued = _queued_deletions()
    mislabels = sorted(rel for files in owned.values() for rel in files
                       if re.search(r"\bv\d{4}\b", Path(rel).name))
    fresh = [r for r in mislabels if r not in queued and _resident(r)]
    if fresh:
        line("no vNNNN mislabels", "FAIL", f"{len(fresh)} remain, e.g. {fresh[0]}")
    elif mislabels:
        line("no vNNNN mislabels", "PENDING",
             f"{len(mislabels)} queued for purge")
    else:
        line("no vNNNN mislabels", "PASS")

    # Covered chapters: the reconciler's own decision function, all series.
    purges = []
    for label, files in sorted(owned.items()):
        kinds = {k for _r, (k, _n, _c) in files.items()}
        if not {"volume", "chapter"} <= kinds:
            continue
        entry = cvr.mvm.get(label, allow_network=False)
        series_purges, _keeps = cvr.plan_decisions(
            label, files, entry, cvr.policy_for(label))
        purges.extend(series_purges)
    resident = [r for r in purges if r not in queued and _resident(r)]
    queued_now = [r for r in purges if r in queued]
    gone = [r for r in purges if r not in queued and not _resident(r)]
    if resident:
        line("chapters covered by volumes", "FAIL",
             f"{len(resident)} still present, e.g. {resident[0]}")
    elif queued_now:
        line("chapters covered by volumes", "PENDING",
             f"{len(queued_now)} queued for purge")
    elif gone:
        line("chapters covered by volumes", "PASS",
             f"{len(gone)} purged (inventory still catching up)")
    else:
        line("chapters covered by volumes", "PASS")

    # One edition per volume number, per series.
    both = []
    for label, files in sorted(owned.items()):
        colours = {}
        for rel, (mtype, number, colored) in files.items():
            if mtype == "volume":
                colours.setdefault(number, set()).add(bool(colored))
        both.extend(f"{label} v{n}" for n, c in colours.items() if len(c) > 1)
    if both:
        line("one edition per volume", "FAIL",
             f"{len(both)} number(s) in both editions, e.g. {both[0]}")
    else:
        total = sum(1 for files in owned.values()
                    for (_r, (k, _n, _c)) in files.items() if k == "volume")
        line("one edition per volume", "PASS", f"{total} volume copy(ies), one per number")


def check_franchise_layout():
    """No series the franchise table knows may sit as a top-level shelf folder.

    THE OWNER'S FAULT (2026-10-03): `Citrus` and `Citrus+` shipped as two top-level
    folders while `Akame ga KILL!` + ZERO were correctly nested. This is that invariant
    over the whole shelf, computed from the table and the owned paths -- so the report
    sees the same fault on ANY title, not just the one the owner happened to notice.
    """
    owned = _manga_series()
    if not owned:
        line("franchise layout", "SKIP", "shelf not enumerable")
        return
    splits = []
    for files in owned.values():
        for rel in files:
            parts = Path(rel).parts
            if len(parts) < 2 or parts[0] != "Comics":
                continue
            if len(parts) >= 3 and parts[1] == "Manga":
                top, kind = parts[2], "manga"
            else:
                top, kind = parts[1], "comic"
            hit = library.comic_franchise(top, kind)
            if hit and (library.normalize_folder_name(hit[0]["name"])
                        != library.normalize_folder_name(top)):
                splits.append(f"{top} (belongs under {hit[0]['name']})")
    if splits:
        line("franchise layout", "FAIL",
             f"{len(splits)} split series: " + "; ".join(sorted(set(splits))[:3]))
    else:
        line("franchise layout", "PASS")


def _title_key(text):
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def _present_titles(last):
    """{normalized source title: title} for every filed episode resident in the library.

    This is the content-verification the collapse review asks for, COMPUTED. A collapse
    is verified when the dropped file's episode is already in the library under its own
    title -- filed from another copy in the pack, or restored by a repair (the Smurfs
    refile + re-fetch, 2026-09-20). The review reappears the moment one dropped episode
    has no resident copy. The inventory decides the bulk and `_resident` catches an
    SSD-only file the inventory has not synced yet.
    """
    inv_path = (Path.home() / "Developer/Media-Orchestrator/Media-Syncer"
                / "remote_inventory.json")
    try:
        inventory = set(json.loads(inv_path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        inventory = None
    out = {}
    for rec in last.values():
        for f in (rec.get("plan") or {}).get("files") or []:
            dst, src = f.get("dst_rel"), f.get("src")
            if not dst or not src:
                continue
            title = journal.title_from_release_name(Path(str(src)).name)
            if not title or _title_key(title) in out:
                continue
            if (inventory is not None and dst in inventory) or _resident(dst):
                out[_title_key(title)] = title
    return out


def check_large_releases():
    """Journal outcomes for big packs: collapsed destinations, parked unfiled files."""
    last = {}
    try:
        for raw in (config.STATE_DIR / "journal.jsonl").read_text(
                encoding="utf-8").splitlines():
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if rec.get("info_hash"):
                last[rec["info_hash"]] = rec
    except OSError:
        line("large releases", "SKIP", "journal unreadable")
        return
    present = _present_titles(last)
    review = []
    parked = []
    verified = 0
    for rec in last.values():
        plan = rec.get("plan") or {}
        drops = plan.get("_deduped_dropped") or []
        if rec.get("status") == "completed" and drops:
            missing = []
            for d in drops:
                title = journal.title_from_release_name(Path(str(d.get("src") or "")).name)
                key = _title_key(title)
                if key and key in present:
                    continue
                if title and any(library._titles_same_episode(title, t)
                                 for t in present.values()):
                    continue
                missing.append(title or Path(str(d.get("src") or "")).name)
            if missing:
                review.append(f"{rec.get('name', '?')[:48]} "
                              f"({len(missing)} of {len(drops)} collapsed unverified)")
            else:
                verified += 1
        if rec.get("status") in ("failed", "refused") and rec.get("unfiled_count"):
            parked.append(f"{rec.get('name', '?')[:48]} "
                          f"({rec['unfiled_count']} unfiled)")
    if parked:
        line("large releases", "PENDING", "parked: " + "; ".join(parked[:3]))
    elif review:
        # Biggest first: the Smurfs (32 collapsed) must be the name a reader sees, not
        # three one-file legacy records that happen to sort earlier.
        review.sort(key=lambda s: int(re.search(r"\((\d+) of", s).group(1)),
                    reverse=True)
        more = f" (+{len(review) - 3} more)" if len(review) > 3 else ""
        line("large releases", "REVIEW",
             "destination collapses need content verification: "
             + "; ".join(review[:3]) + more)
    elif verified:
        line("large releases", "PASS",
             f"{verified} collapsed record(s) verified: every dropped episode is "
             f"resident under its own title")
    else:
        line("large releases", "PASS")


def main() -> int:
    check_sidecar_identity()
    check_manga()
    check_franchise_layout()
    check_large_releases()
    print()
    bad = [r for r in results if r[1] == "FAIL"]
    pend = [r for r in results if r[1] == "PENDING"]
    rev = [r for r in results if r[1] == "REVIEW"]
    print(f"{len(results) - len(bad) - len(pend) - len(rev)} PASS, {len(bad)} FAIL, "
          f"{len(pend)} PENDING, {len(rev)} REVIEW")
    return 0                                        # a report: never gates a deploy


if __name__ == "__main__":
    raise SystemExit(main())
