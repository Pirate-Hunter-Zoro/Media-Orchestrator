#!/usr/bin/env python3
"""media_doctor.py -- the library's continuous health daemon.

The nightly metadata net (audit_metadata -> repair_metadata -> save_posters) is
DISK-centric: it detects blank `.nfo` and missing on-disk posters. It has a blind
spot for the failure the user actually hits while browsing on a phone -- a title
that is fine ON DISK but broken in Jellyfin's PRESENTATION:

  * a Series present in the DB whose children Jellyfin never resolved (0 or fewer
    episodes than exist on disk) -- "Monogatari just doesn't show up";
  * a Series with no Primary image even though a `folder.jpg` sits right next to
    it on disk -- "Berserk has no poster";
  * a Series whose artwork belongs to a DIFFERENT SHOW, or whose episode images
    are posters rather than stills -- "The Seven Deadly Sins' cover and episode
    images are completely out of whack" (see `artwork_identity_stale` /
    `artwork_bogus` in `diagnose_show`);
  * an episode whose stored title is release-group junk (`[Judas] x265 10b`) or a
    bare `Episode N`, even though the filename carries the real title -- "Sinbad
    has janky episode titles";
  * two files for the same SxxExx (the Fairy Tail repeat), a 0-byte stub, macOS
    `._AppleDouble`/`.DS_Store` litter.

This daemon reconciles DISK TRUTH against JELLYFIN TRUTH every cycle and heals the
gap. It fixes the mechanical failures itself, on an escalation ladder that is
safe to re-run (refresh -> delete+rescan -> escalate), and for the judgment cases
(fill a real synopsis, re-identify a wrong TMDB match, pick which duplicate to
keep) it does what the rest of the fleet does: **spawns a headless AI run to
fix it** -- one show per cycle, budget-gated, so the foolish human never has to.

NOTHING here deletes a library media file -- not the mechanical fixes, and not the
escalated run. Mechanical fixes touch only sidecars (`.nfo`, posters) and the
Jellyfin DB (refresh/relink/rename). A real media deletion (a duplicate, a stub) is
reported for human review and performed through the proper purge runbook: through
the mount, so mediafs' unlink path propagates it to the drives and queues the pool
purge. The escalated run cannot do it even if it decides it should -- `ai_client`'s
Bash tool refuses any command that would remove or relocate library media, because
through the mount that delete reaches every drive at once.

Runs under launchd (`com.mikeyferguson.mediadoctor`) every 30 min (`CYCLE_SEC`); also
usable by hand:

    python3 scripts/media_doctor.py --once            # one pass, apply fixes
    python3 scripts/media_doctor.py --once --dry-run   # report only, change nothing
    python3 scripts/media_doctor.py --once --show "Berserk (1997)"   # one title
    python3 scripts/media_doctor.py --no-escalate      # mechanical fixes only
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config      # noqa: E402
import library     # noqa: E402

# --- tunables ----------------------------------------------------------------

# Reconcile cadence. 30 min, and this default is the SINGLE source of truth -- the
# plist deliberately does NOT set MEDIA_DOCTOR_CYCLE_SEC, because two places naming
# the same number is how this drifted: the default here said 900 ("# 15 min") while
# the plist overrode to 1800, so the daemon had never once run at the interval its
# own code advertised, and the README documented the plist's value.
#
# 30 min is not about pass cost -- a full 234-show pass is ~30 s, so the daemon is
# idle >98% of the time either way. It is about the ESCALATION CEILING: at most one
# headless AI run is spawned per cycle, so the interval IS the rate limit on
# API spend (~48 runs/day here; halving it to 15 min silently doubles that).
# It also keeps a two-refresh ladder honest -- REFRESH_SETTLE_SEC is 600 s, so a
# 30 min cycle guarantees a show's second refresh is a genuinely separate attempt
# rather than a retry of one Jellyfin has not finished reacting to.
CYCLE_SEC = int(os.environ.get("MEDIA_DOCTOR_CYCLE_SEC", "1800"))    # 30 min
# How long to wait when a pass could not reach Jellyfin at all. Short, because the usual
# cause is a restart that is seconds from finishing, and a missed pass means the health
# report keeps describing a library that has already changed.
RETRY_SEC = int(os.environ.get("MEDIA_DOCTOR_RETRY_SEC", "60"))
# Give Jellyfin's own scan its chance before we intervene, keyed off the newest
# file in a show. A brand-new drop that Jellyfin simply hasn't reached yet is not
# a bug.
MIN_AGE_REFRESH_SEC = int(os.environ.get("MEDIA_DOCTOR_MIN_AGE_REFRESH_SEC", str(2 * 3600)))
MIN_AGE_HEAVY_SEC = int(os.environ.get("MEDIA_DOCTOR_MIN_AGE_HEAVY_SEC", str(6 * 3600)))
REFRESH_SETTLE_SEC = int(os.environ.get("MEDIA_DOCTOR_REFRESH_SETTLE_SEC", "600"))
MAX_ESCALATIONS_PER_SIG = 2
# Heal gradually, not in one burst: a per-item refresh ffprobes the show's missing
# episodes, which HYDRATES any that are pool-only (~pricey). Cap how many shows we
# refresh (and how many heavy rescans we fire) per cycle so a library with a big
# backlog of broken shows recovers over several cycles instead of all at once.
MAX_REFRESH_PER_CYCLE = int(os.environ.get("MEDIA_DOCTOR_MAX_REFRESH_PER_CYCLE", "4"))
MAX_RESCAN_PER_CYCLE = int(os.environ.get("MEDIA_DOCTOR_MAX_RESCAN_PER_CYCLE", "1"))
# Per-episode artwork re-adoptions per cycle. Each is one Jellyfin RemoteImages lookup
# plus one provider download of a ~200 KB JPEG -- cheap, but a whole broken season is 24
# of them, so cap it and let a big backlog drain over several cycles.
MAX_ART_FIX_PER_CYCLE = int(os.environ.get("MEDIA_DOCTOR_MAX_ART_FIX_PER_CYCLE", "40"))

# How long to let a `RemoteSearch/Apply` re-identification settle before writing the
# corrected nfos. Jellyfin's NFO saver runs asynchronously after the apply and will
# overwrite freshly written files with the stale DB values if they are written first;
# 20 s is generous for a series item (measured on TZ: the refresh touched the folder
# for ~15 s). One show per repair, so the sleep is not on any hot path.
REIDENTIFY_SETTLE_SEC = int(os.environ.get("MEDIA_DOCTOR_REIDENTIFY_SETTLE_SEC", "20"))

# How long a provider's "I have no still for this episode" answer is believed before the
# image is offered to it again. Thirty days, the same reasoning `epguide` uses for its
# episode cache: the answer is a fact about a provider's CATALOGUE at a moment, not about
# physics, and stills do get added -- but re-asking every thirty minutes turns a settled
# question into twenty lines of report that nobody can act on.
ART_NO_STILL_TTL_SEC = int(os.environ.get("MEDIA_DOCTOR_ART_NO_STILL_TTL_SEC",
                                          str(30 * 24 * 3600)))

# How many consecutive passes an `[auto]` problem may survive before the report stops
# calling it automatic. At a 30-minute cycle, four passes is two hours -- long enough that
# a genuinely transient condition (a mount settling, a scan in progress) has cleared, and
# short enough that a permanently-stuck one is named the same morning rather than weeks
# later. This is the check that would have surfaced the dead TMDB id.
STUCK_AFTER_PASSES = int(os.environ.get("MEDIA_DOCTOR_STUCK_AFTER_PASSES", "4"))


def _stuck_key(prob):
    """The identity of a report line across passes: kind + the exact detail text."""
    return f"{prob.get('kind')}|{prob.get('detail')}"


def _note_problem_pass(state, show, problems):
    """Count this pass's problems and forget any that are no longer reported.

    Keyed by show + problem KIND + detail, so a count that changes ("3 episode(s) blank"
    -> "1 episode(s) blank") reads as progress and resets, while a line that never moves
    accumulates. Returns the per-key count for the report.

    These counters ARE state, so the caller must persist `state` AFTER calling this --
    the save used to run before the report, which meant a genuinely stuck `[auto]` line
    could never be demoted to NEEDS REVIEW: the exact promise `STUCK_AFTER_PASSES`
    makes, silently unkept for every problem it existed for. An empty `problems` list
    forgets the show's counters, which is how a healthy show starts clean.
    """
    try:
        st = state.setdefault(show, {}).setdefault("stuck", {})
        keys = {_stuck_key(q) for q in problems}
        for q in problems:
            k = _stuck_key(q)
            st[k] = int(st.get(k, 0)) + 1
        for k in [k for k in st if k not in keys]:
            del st[k]
        return {k: st[k] for k in keys}
    except Exception:                                                 # noqa: BLE001
        return {}
# How many episodes must share one image before it reads as a smeared fallback rather
# than a coincidence. Two neighbouring episodes can legitimately reuse a frame.
ART_DUP_MIN = int(os.environ.get("MEDIA_DOCTOR_ART_DUP_MIN", "3"))

STATE_FILE = config.STATE_DIR / "doctor_state.json"
WORKLIST_FILE = config.STATE_DIR / "doctor_worklist.json"
# Per-show artwork verdict cache. Keyed by a signature over every episode image's
# (name, size, mtime), so a show whose art has not changed since the last pass is
# re-verified for FREE -- without this the check would re-read a JPEG header for
# ~14k files every cycle.
ART_CACHE_FILE = config.STATE_DIR / "doctor_art_cache.json"
# Phone-glanceable health report, alongside Media-Syncer's free-space report in
# the iCloud Torrents folder.
REPORT_FILE = config.TORRENTS_DIR / "library_health.txt"

VIDEO_EXTS = config.VIDEO_EXTENSIONS
POSTER_ON_DISK = ("folder.jpg", "folder.png", "poster.jpg", "poster.png",
                  "cover.jpg", "cover.png")

# A stored title that is release-group / encoding junk rather than a real title.
#
# Split into STRONG and WEAK markers, because the original single pattern matched ordinary
# English. `opus` and `batch` are audio-codec / release-scene terms AND real words: it flagged
# Marvel's "Magnum Opus" and Clone Wars' "The Bad Batch" as junk on every pass, forever, with
# nothing to fix. (Seen 2026-08-04.)
#
# STRONG markers are things that essentially never appear in a real episode title -- a bracketed
# group tag, a codec spec, a resolution, a source tag. Any one of them means junk.
JUNK_TITLE_STRONG_RE = re.compile(
    r"\[[^\]]+\]|x26[45]|\b(1080p|720p|480p|2160p|4k|webrip|web-?dl|"
    r"bluray|bdrip|brrip|hevc|h\.?26[45]|xvid|dual.?audio|multi-?subs?|remux)\b",
    re.I)
# WEAK markers are plausible inside a legitimate title, so one alone proves nothing. They count
# only alongside a second signal -- another marker, or a filename-ish shape (see _looks_like_filename).
#
# `10bit`/`8-bit` was STRONG and is now WEAK. It is a bit depth in a release tag AND an ordinary
# English adjective: Young Sheldon S02E08 is genuinely called "An 8-Bit Princess and a Flat Tire
# Genius", and it was the ONLY stored title in the whole library the STRONG rule flagged -- a
# detector whose entire live yield is one false positive, reported every pass with nothing to fix.
# Demoting costs nothing real: an actual junk title carrying a bit depth also carries a codec
# (`x265 10bit`), a group tag (`[Judas] x265 10b`) or a second weak marker (`10bit AAC`), each of
# which still convicts it. (Measured 2026-09-05 across every .nfo under ~/Media/Shows.)
JUNK_TITLE_WEAK_RE = re.compile(r"\b(aac|flac|opus|batch|uncensored|vid|(10|8).?bit)\b", re.I)
# Filename-ish shape: dot-separated words ("The.Promised.Neverland.S01E01"), underscores, or an
# embedded SxxExx tag. Real titles do not look like this.
FILENAME_SHAPE_RE = re.compile(r"\w\.\w|_|[Ss]\d{1,4}[Ee]\d{1,4}")


def _title_junk_markers(title: str) -> bool:
    """True if `title` carries release/encoding junk markers (see the STRONG/WEAK split above)."""
    if JUNK_TITLE_STRONG_RE.search(title):
        return True
    weak = len(JUNK_TITLE_WEAK_RE.findall(title))
    if weak >= 2:
        return True
    return bool(weak and FILENAME_SHAPE_RE.search(title))


GENERIC_TITLE_RE = re.compile(r"^(episode|ep|chapter)\s*\d+$", re.I)
# Real title trailing the SxxExx tag in a filename: "... - S01E02-The Real Title".
FILENAME_TITLE_RE = re.compile(r"[Ss]\d{1,4}[Ee]\d{1,4}(?:-[Ee]\d{1,4})?-(.+)$")
# The two guards after the episode number both matter, and the order they were got wrong once:
#   (?!\d)   -- no backtracking into a shorter digit run. Without it, the fraction guard below
#               just makes the engine retry `E1` out of `E17.5` and succeed, which is WORSE than
#               the bug it was fixing (E17.5 then collides with E01 instead of E17).
#   (?!\.\d) -- a FRACTIONAL episode (`S03E17.5`, the recap/digression numbering anime uses) is a
#               DIFFERENT episode from `S03E17`. Truncating it reports the two files as duplicates
#               covering one SxxExx: a false positive no reviewing can resolve, because nothing is
#               wrong. Such a file now matches nothing and is simply left out of span accounting.
# Note this regex is fed the filename WITH its extension, so it must still accept `S01E01.mkv`
# (dot followed by a letter, not a digit) -- which is why the guard is `\.\d` and not `\.`.
# (Seen 2026-08-04: That Time I Got Reincarnated as a Slime S03E17 vs S03E17.5-Digression.)
EP_SPAN_RE = re.compile(r"[Ss](\d{1,4})[Ee](\d{1,4})(?!\d)(?!\.\d)(?:-[Ee](\d{1,4}))?")


def _log(msg: str) -> None:
    print(f"[media_doctor] {msg}", flush=True)


# --- Jellyfin (urllib, api_key auth -- matches save_posters.py) --------------

class Jellyfin:
    def __init__(self):
        self.base = config.JELLYFIN_URL.rstrip("/")
        self.key = config.JELLYFIN_API_KEY
        if not self.base or not self.key:
            raise RuntimeError("JELLYFIN_URL / JELLYFIN_API_KEY not set")
        self._uid = None

    def _req(self, path, method="GET", params=None, data=None, ctype=None, timeout=60):
        params = dict(params or {})
        params["api_key"] = self.key
        url = f"{self.base}/{path.lstrip('/')}?{urllib.parse.urlencode(params, doseq=True)}"
        r = urllib.request.Request(url, method=method, data=data)
        r.add_header("X-Emby-Token", self.key)
        if ctype:
            r.add_header("Content-Type", ctype)
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            body = resp.read()
            if body and resp.headers.get("content-type", "").startswith("application/json"):
                return resp.status, json.loads(body)
            return resp.status, body

    def get(self, path, **params):
        return self._req(path, params=params)[1]

    def user_id(self):
        if self._uid is None:
            configured = getattr(config, "JELLYFIN_USER_ID", "")
            self._uid = configured or self.get("Users")[0]["Id"]
        return self._uid

    def series_index(self):
        """{absolute series folder path -> id}."""
        items = self.get("Items", Recursive="true", IncludeItemTypes="Series",
                         Fields="Path", enableImages="false").get("Items", [])
        return {it["Path"].rstrip("/"): it["Id"] for it in items if it.get("Path")}

    def series_pids(self):
        """{series id -> ProviderIds}, in ONE bulk query.

        Per-show `provider_ids()` calls would be 200+ requests a cycle just to notice
        that nothing changed.
        """
        items = self.get("Items", Recursive="true", IncludeItemTypes="Series",
                         Fields="ProviderIds", enableImages="false").get("Items", [])
        return {it["Id"]: (it.get("ProviderIds") or {}) for it in items}

    def episodes(self, series_id):
        """Episodes of a series, with a SeriesId fallback.

        `/Shows/{id}/Episodes` filters on `SeriesPresentationUniqueKey`, NOT on SeriesId, so it
        returns an empty list when a series' children are stranded under a stale key -- even
        though the episode rows exist and are correctly linked. Trusting it alone made this
        daemon report "resolved 0 of N" and fire library scan after library scan at a problem no
        scan can fix. So an empty result is re-checked the robust way before being believed.
        (db_guardian repairs the underlying key drift; see its reconcile_series_presentation_keys.)
        """
        data = self.get(f"Shows/{series_id}/Episodes", userId=self.user_id(),
                        Fields="Path,ParentIndexNumber,IndexNumber")
        items = (data or {}).get("Items", [])
        if items:
            return items
        alt = self.get("Items", userId=self.user_id(), Recursive="true",
                       IncludeItemTypes="Episode", Fields="Path,ParentIndexNumber,IndexNumber",
                       enableImages="false")
        return [e for e in (alt or {}).get("Items", []) if e.get("SeriesId") == series_id]

    def image_types(self, item_id):
        imgs = self.get(f"Items/{item_id}/Images")
        return [i.get("ImageType") for i in imgs] if isinstance(imgs, list) else []

    def refresh(self, item_id, meta="Default", img="Default",
                replace_meta=False, replace_img=False, recursive=True):
        self._req(f"Items/{item_id}/Refresh", method="POST", params={
            "Recursive": str(recursive).lower(),
            "metadataRefreshMode": meta, "imageRefreshMode": img,
            "replaceAllMetadata": str(replace_meta).lower(),
            "replaceAllImages": str(replace_img).lower(),
        })

    def push_primary(self, item_id, jpeg_bytes):
        self._req(f"Items/{item_id}/Images/Primary", method="POST",
                  data=base64.b64encode(jpeg_bytes), ctype="image/jpeg")

    def remote_primary_url(self, item_id):
        try:
            data = self.get(f"Items/{item_id}/RemoteImages", type="Primary", limit="1")
            imgs = (data or {}).get("Images", [])
            return imgs[0]["Url"] if imgs else None
        except Exception:                                             # noqa: BLE001
            return None

    def adopt_remote_image(self, item_id, image_type, url):
        self._req(f"Items/{item_id}/RemoteImages/Download", method="POST",
                  params={"Type": image_type, "ImageUrl": url})

    def best_remote_still(self, item_id):
        """URL of the best real 16:9 still for an episode, or None.

        Ranked landscape-first on purpose: TMDB will happily hand back a portrait
        season poster as an episode `Primary`, which is the very thing being repaired.
        Restricted to TheMovieDb because the OMDb entries carry no dimensions, so they
        cannot be aspect-checked and are as likely to be a poster as a still.

        `None` means the provider GENUINELY offers no landscape still. A lookup that
        could not be made RAISES, because callers remember a `None` as a refusal (the
        art check stops re-asking for `ART_NO_STILL_TTL_SEC`), and a transport blip must
        never be written down as a provider-catalogue fact.
        """
        data = self.get(f"Items/{item_id}/RemoteImages", type="Primary", limit="50")
        ims = [i for i in (data or {}).get("Images", [])
               if i.get("Type") == "Primary" and i.get("ProviderName") == "TheMovieDb"
               and i.get("Width") and i.get("Height")]
        if not ims:
            return None

        def rank(i):
            w, h = i["Width"], i["Height"]
            landscape = 1 if 1.5 <= w / h <= 2.1 else 0
            return (-landscape, -(i.get("CommunityRating") or 0), -(w * h))

        ims.sort(key=rank)
        best = ims[0]
        if not (1.5 <= best["Width"] / best["Height"] <= 2.1):
            return None            # provider genuinely has no still; leave it alone
        return best["Url"]

    def update_item(self, item_id, **fields):
        full = self.get(f"Items/{item_id}", userId=self.user_id())
        full.update(fields)
        self._req(f"Items/{item_id}", method="POST", data=json.dumps(full).encode(),
                  ctype="application/json")

    def library_scan(self):
        self._req("Library/Refresh", method="POST")

    def provider_ids(self, item_id):
        return (self.get(f"Items/{item_id}", userId=self.user_id()) or {}).get("ProviderIds") or {}

    def apply_identification(self, item_id, name, ids, year=None, replace_images=False):
        """Pin a series' identity via RemoteSearch/Apply.

        This is the cure for a series item carrying NO ProviderIds: without a TVDB/TMDB id
        Jellyfin cannot identify the show, so it resolves ZERO episodes and neither a recursive
        refresh nor a library scan ever fixes it -- both need an identity to match against.
        Applying the id makes the very next refresh resolve the children normally.

        `replace_images` is the difference between the two reasons to call this, and getting
        it wrong is what left a show wearing another show's face for five days:

          * FILLING IN a missing identity (the default) -- the existing artwork was supplied
            locally or scraped by hand and is the only artwork there is. KEEP it.
          * RE-MATCHING a wrong identity -- every existing image belongs to the show being
            matched away from, and `replaceAllImages=false` pins it there permanently, because
            no ordinary refresh ever replaces an image slot that is already filled. Pass
            `replace_images=True`, and `media_doctor`'s `artwork_identity_stale` check will
            catch it anyway if a re-match happens outside this method.
        """
        body = {"Name": name, "ProviderIds": ids, "SearchProviderName": "TheMovieDb"}
        if year:
            body["ProductionYear"] = year
        # A long timeout on purpose: Apply fetches the candidate's metadata AND, with
        # `replaceAllImages`, its images synchronously, and was measured taking over
        # 60 s on the 2026-09-29 Gumball repair -- the default timed the client out
        # while the server kept going (the repair landed, but the tool logged a false
        # failure and skipped its settle wait).
        self._req(f"Items/RemoteSearch/Apply/{item_id}", method="POST",
                  params={"replaceAllImages": str(bool(replace_images)).lower()},
                  data=json.dumps(body).encode(), ctype="application/json",
                  timeout=180)

    def movie_index(self):
        """{absolute movie file path -> (id, has_overview, has_primary)}."""
        items = self.get("Items", Recursive="true", IncludeItemTypes="Movie",
                         Fields="Path,Overview").get("Items", [])
        out = {}
        for it in items:
            p = it.get("Path")
            if p:
                img = it.get("ImageTags") or {}
                out[str(Path(p).resolve())] = (it["Id"], bool(it.get("Overview")),
                                               bool(img.get("Primary")), it.get("Name") or "")
        return out

    def remote_search_movie(self, item_id, tmdb=None, name=None, year=None):
        """Query TMDB for a movie candidate. The candidate carries `Overview`
        inline -- which is reliable even when Jellyfin's own refresh detail-fetch
        (the thing that fills a movie's plot) fails. Returns the best candidate
        dict or None."""
        info = {}
        if tmdb:
            info["ProviderIds"] = {"Tmdb": str(tmdb)}
        if name:
            info["Name"] = name
        if year:
            info["Year"] = int(year)
        try:
            _, res = self._req("Items/RemoteSearch/Movie", method="POST",
                               data=json.dumps({"ItemId": item_id, "SearchInfo": info}).encode(),
                               ctype="application/json", timeout=40)
        except Exception:                                                # noqa: BLE001
            return None
        if not isinstance(res, list) or not res:
            return None
        if tmdb:
            for c in res:
                if (c.get("ProviderIds") or {}).get("Tmdb") == str(tmdb):
                    return c
        return res[0]

    def boxsets(self):
        """{boxset id -> (name, has_primary, provider_ids)} for every collection in the
        library. A collection is a first-class Jellyfin item (a BoxSet) with its OWN
        image slots -- a movie can have a poster while its collection shows a blank
        cover, which is exactly the presentation failure this pass heals.

        `enableImages` is left at its default (true): it is what makes ImageTags --
        the "does this collection have a Primary image" signal -- come back at all.
        """
        items = self.get("Items", Recursive="true", IncludeItemTypes="BoxSet",
                         Fields="ProviderIds").get("Items", [])
        out = {}
        for it in items:
            img = it.get("ImageTags") or {}
            out[it["Id"]] = (it.get("Name") or "", bool(img.get("Primary")),
                             it.get("ProviderIds") or {})
        return out

    def item_children(self, item_id, item_type="Movie"):
        """Children of a collection/folder item, e.g. the member movies of a BoxSet.

        `Recursive=true` is required: a BoxSet links its member films through a
        collection-item table rather than a direct parent pointer, so a non-recursive
        `ParentId` query returns nothing."""
        data = self.get("Items", Recursive="true", ParentId=item_id,
                        IncludeItemTypes=item_type, Fields="Path,ProviderIds")
        return (data or {}).get("Items", [])

    def primary_bytes(self, item_id):
        """Raw bytes of an item's Primary image, or None. Used to backfill a BoxSet
        whose provider has no collection poster by adopting a member film's poster."""
        try:
            body = self.get(f"Items/{item_id}/Images/Primary")
            return body if isinstance(body, (bytes, bytearray)) and len(body) > 0 else None
        except Exception:                                                # noqa: BLE001
            return None

    def delete_item(self, item_id):
        """Remove a Jellyfin item (DB row only). The underlying files are already gone
        (deleted through the mount), so this just cleans the empty presentation."""
        self._req(f"Items/{item_id}", method="DELETE")


# --- state -------------------------------------------------------------------

def _load(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:                                             # noqa: BLE001
            return default
    return default


def _save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


# --- disk truth --------------------------------------------------------------

def _drive_media_roots():
    roots = []
    vol = Path("/Volumes")
    if vol.is_dir():
        for v in vol.iterdir():
            m = v / "Media"
            if m.is_dir():
                roots.append(m)
    return roots


def _show_dirs(shows_root, only=None):
    if not shows_root.is_dir():
        return []
    out = []
    for p in sorted(shows_root.iterdir()):
        if p.is_dir() and not p.name.startswith("."):
            if only and p.name != only:
                continue
            out.append(p)
    return out


# Jellyfin's EXTRAS folder conventions. A video in one of these is an extra, not an episode --
# Jellyfin attaches it to the parent as a special feature and deliberately does NOT create an
# episode item for it. Counting them as expected episodes makes every show that ships extras
# permanently report "resolved N of M", with a gap that no refresh can ever close. (Seen
# 2026-08-04: 15 of 54 "unresolved episodes" across the library were nothing but
# `featurettes/` content -- Loki 4, Samurai Jack 11.)
EXTRAS_DIRS = {
    "featurettes", "extras", "specials-extras", "behind the scenes", "deleted scenes",
    "interviews", "scenes", "samples", "shorts", "trailers", "other", "backdrops",
}

# A multi-part episode: `- part2`, `Part 2`, `(Part 2)`, `pt2`, for part >= 2. Jellyfin STACKS
# the parts into ONE episode item whose Path is part 1, so parts 2+ legitimately have no item of
# their own. Counting them as missing episodes -- and flagging the pair as a duplicate covering
# one SxxExx -- are two faces of the same mistake. (Seen 2026-08-04: Kaguya-sama S00E06,
# Star Wars Rebels S00E05 and S00E06.)
MULTIPART_CONTINUATION_RE = re.compile(r"[-(\s_.]p(?:ar)?t\.?\s*0*([2-9]|\d{2,})\b", re.I)


def _is_extra(path, show_dir):
    """True if `path` sits inside a Jellyfin extras folder beneath `show_dir`."""
    try:
        rel_parts = path.relative_to(show_dir).parts[:-1]
    except ValueError:
        return False
    return any(part.strip().lower() in EXTRAS_DIRS for part in rel_parts)


def _is_multipart_continuation(name):
    """True for part 2+ of a stacked multi-part episode (part 1 owns the Jellyfin item)."""
    return bool(MULTIPART_CONTINUATION_RE.search(name))


def _episode_videos(show_dir):
    """Every episode video under a show folder, via the mount (inventory-backed, so this is
    instant and never touches a remote).

    EXCLUDES two things Jellyfin deliberately does not turn into episode items -- extras-folder
    content and parts 2+ of a stacked multi-part episode -- because counting them inflates the
    expected-episode total and produces a permanent, unfixable "resolved N of M" gap.
    """
    out = []
    for p in show_dir.rglob("*"):
        if not (p.is_file() and p.suffix.lower() in VIDEO_EXTS and not p.name.startswith("._")):
            continue
        if _is_extra(p, show_dir) or _is_multipart_continuation(p.stem):
            continue
        out.append(p)
    return out


NFO_ID_RES = (("Tvdb", re.compile(r"<tvdbid>\s*(\d+)", re.I)),
              ("Tmdb", re.compile(r"<tmdbid>\s*(\d+)", re.I)),
              ("Tvdb", re.compile(r'<uniqueid[^>]*type="tvdb"[^>]*>\s*(\d+)', re.I)),
              ("Tmdb", re.compile(r'<uniqueid[^>]*type="tmdb"[^>]*>\s*(\d+)', re.I)))


def _nfo_provider_ids(show_path):
    """Provider ids from a show's `tvshow.nfo`, as Jellyfin's ProviderIds dict.

    Used to repair a series item that has no identity of its own -- the ids are written by the
    ingest/metadata side and are almost always present on disk even when Jellyfin's DB row has
    none. First match per provider wins (`<tvdbid>` before `<uniqueid type="tvdb">`).
    """
    if not show_path:
        return {}
    nfo = Path(show_path) / "tvshow.nfo"
    try:
        text = nfo.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}
    ids = {}
    for key, rx in NFO_ID_RES:
        if key in ids:
            continue
        m = rx.search(text)
        if m:
            ids[key] = m.group(1)
    return ids


# --- series identity: does the id name the show the nfo claims? (HANDOFF 10.3) --
#
# THE INCIDENT. The Twilight Zone (2019) carried `<tmdbid>83135</tmdbid>` (correct)
# but `<tvdbid>325542</tvdbid>`, `<premiered>2013-01-30</premiered>` and
# `<originaltitle>萌宠成长记（精编版）</originaltitle>` -- the remnant of a plan whose ids
# were the *Too Cute* ones. The local `folder.jpg` was byte-identical to TMDB 80979's
# poster, and local art outranks remote, so the wrong cover and the wrong S01 year
# survived every Jellyfin refresh. The doctor never looked at series identity fields
# or title-level art at all -- `_scan_episode_art` only inspects episode stills --
# which is why the fault could not self-heal.
#
# WHAT IS COMPUTED, AND WHY ONLY `premiered`. The provider is asked who the id is.
# A `<year>` that is stale against TMDB is NOT a defect: measured 2026-09-20 over 299
# live nfos, four shows carry a pilot/preview year (Assassination Classroom 2013 vs
# 2015, Hazbin Hotel 2019 vs 2024, Yamato 2205, Star vs.) with a `premiered` that
# matches TMDB exactly. A `<tvdbid>` that differs from TMDB's `external_ids` is not
# sufficient either (six live shows: Doctor Who (2005), Hunter x Hunter (1999), The
# Smurfs, The Venture Bros. -- all legitimate TVDB/TMDB divergences). The only signal
# that flagged exactly one show across the whole library was `premiered` against
# TMDB's `first_air_date`: TZ. So `premiered` is the trigger; the tvdb/originaltitle
# disagreement becomes a repair TARGET once the trigger has fired.
#
# FAIL OPEN. No key, no id, a transport error or a 404 -> no opinion and no rewrite.
# A dead pinned id is NOT flagged here: One Pace is intentionally identity-less and
# carries its own art, and §7 already says "no TMDB is only a defect when the poster
# is also missing", which `identity_missing` owns.

def _nfo_text(show_dir):
    try:
        return (Path(show_dir) / "tvshow.nfo").read_text(encoding="utf-8",
                                                         errors="ignore")
    except OSError:
        return ""


def _nfo_identity(show_dir):
    """The identity fields of a show's `tvshow.nfo` (missing keys are None)."""
    text = _nfo_text(show_dir)
    if not text:
        return {}
    out = {}
    for key in ("title", "originaltitle", "year", "premiered", "enddate"):
        m = re.search(rf"<{key}>\s*(.*?)\s*</{key}>", text, re.I | re.S)
        out[key] = m.group(1).strip() if m else None
    ids = _nfo_provider_ids(show_dir)
    out["tmdbid"] = ids.get("Tmdb")
    out["tvdbid"] = ids.get("Tvdb")
    return out


def _title_art_files(show_dir):
    """Title-level art on disk: the series cover/backdrop and every season poster."""
    sd = Path(show_dir)
    out = []
    for name in ("folder.jpg", "poster.jpg", "landscape.jpg", "backdrop.jpg"):
        p = sd / name
        if p.exists():
            out.append(p)
    out.extend(sorted(sd.glob("season*-poster.*")))
    return out


def _series_identity_problem(show_dir, pids, tvdb_owners=None):
    """`(detail, ident)` when TMDB contradicts the nfo's `premiered`, or when the
    nfo's tvdb id is pinned by a different library series and TMDB cannot
    corroborate it here; else None.

    `ident` is the verified TMDB record with a `tmdb_id` key added, for the repair.
    `tvdb_owners` maps a claimed tvdb id to every OTHER series that pins it
    (`{"show", "path", "tmdb"}`, built by `run_once` from Jellyfin's ProviderIds).
    """
    pids = pids or {}
    nfo = _nfo_identity(show_dir)
    tmdb_id = pids.get("Tmdb") or nfo.get("tmdbid")
    if not tmdb_id:
        return None
    try:
        import tmdbguide
        ident = tmdbguide.show_identity(tmdb_id)
    except Exception:                                            # noqa: BLE001
        return None
    if not ident or ident.get("dead"):
        return None
    ident = dict(ident)
    ident["tmdb_id"] = str(tmdb_id)
    first = str(ident.get("first_air_date") or "")[:4]
    prem = str(nfo.get("premiered") or "")[:4]
    why = []
    if first.isdigit() and prem.isdigit() and abs(int(first) - int(prem)) > 1:
        why.append(f"nfo premiered {nfo.get('premiered')} vs TMDB first air date "
                   f"{ident.get('first_air_date')}")
        if nfo.get("tvdbid") and ident.get("tvdb_id") \
                and str(nfo["tvdbid"]) != str(ident["tvdb_id"]):
            why.append(f"nfo tvdbid {nfo['tvdbid']} vs TMDB-records {ident['tvdb_id']}")
        # `enddate` is its own trap: Jellyfin's saver re-stamps it from its DB and it is
        # not a lockable field, so a stale value survives the rest of the repair (measured:
        # TZ held Too Cute's 2013-03-06 end date beside the corrected 2019 premiere, and a
        # premiered-only trigger never looked at it again).
        end = (nfo.get("enddate") or "")[:4]
        last = str(ident.get("last_air_date") or "")[:4]
        if end.isdigit() and last.isdigit() and abs(int(end) - int(last)) > 1:
            why.append(f"nfo enddate {nfo['enddate']} vs TMDB last air date "
                       f"{ident.get('last_air_date')}")
        if nfo.get("originaltitle") and ident.get("original_name") \
                and nfo["originaltitle"] != ident["original_name"] \
                and nfo["originaltitle"] != ident.get("name"):
            why.append(f"nfo originaltitle {nfo['originaltitle']!r} is neither name")
    # THE CROSS-SERIES COLLISION (2026-09-29, the two Gumball shows). Jellyfin
    # keys a series' presentation on its provider ids: two series pinning one
    # tvdb id share ONE `PresentationUniqueKey`, so each series lists the other's
    # seasons and episodes. The side to repair is the one TMDB cannot corroborate
    # (its own record maps a different id, or none); the other side is the id's
    # real owner and is left alone. A collision where BOTH sides corroborate
    # (TheTVDB merges two TMDB entries) is not auto-stripped -- there is no
    # computed authority; the `--scan`-style report owns that rare shape.
    #
    # The claimed id is read from the nfo AND from Jellyfin's ProviderIds: the
    # Jellyfin item is what the merge actually keys on, and if a re-match timed
    # out after the nfo was already cleaned, the trigger must survive so the next
    # pass finishes the repair rather than leaving the two series merged forever.
    claimed = str(nfo.get("tvdbid") or (pids or {}).get("Tvdb") or "")
    others = [o for o in (tvdb_owners or {}).get(claimed, [])
              if str(o.get("path") or "").rstrip("/") != str(show_dir).rstrip("/")]
    if claimed and others \
            and not (ident.get("tvdb_id")
                     and str(ident["tvdb_id"]) == claimed):
        names = ", ".join(sorted(str(o.get("show")) for o in others))
        why.append(
            f"nfo tvdbid {claimed} is also pinned by {names}; TMDB records "
            f"{ident.get('tvdb_id') or 'no TVDB id'} for tmdb_id {tmdb_id}, so "
            f"this series does not own the id and Jellyfin merges both series' "
            f"seasons and episodes under one presentation key")
    if not why:
        return None
    return (f"series identity is contaminated ({'; '.join(why)}) -- TMDB {tmdb_id} "
            f"is {ident.get('name')!r} ({ident.get('year')}); the season year and the "
            f"on-disk title art predate the correct match and will not be replaced "
            f"on their own", ident)


def _set_xml_tag(text, tag, value, root):
    """Set a simple tag, or insert it before the closing `root` tag. `value=None` skips."""
    if value is None:
        return text
    pat = re.compile(rf"<{tag}>.*?</{tag}>", re.S | re.I)
    repl = f"<{tag}>{value}</{tag}>"
    if pat.search(text):
        return pat.sub(repl, text, count=1)
    return text.replace(f"</{root}>", f"  {repl}\n</{root}>", 1)


def _drop_xml_tag(text, tag):
    """Remove a simple tag entirely. Used when the verified identity has no value
    for it -- a stale provider id must not survive a repair that disproved it."""
    return re.sub(rf"<{tag}>.*?</{tag}>\s*", "", text, flags=re.S | re.I)


def _set_uniqueid(text, kind, value, default=False):
    if not value:
        return text
    pat = re.compile(rf'<uniqueid[^>]*type="{kind}"[^>]*>.*?</uniqueid>', re.S | re.I)
    attrs = f'type="{kind}"' + (' default="true"' if default else '')
    repl = f"<uniqueid {attrs}>{value}</uniqueid>"
    if pat.search(text):
        return pat.sub(repl, text, count=1)
    return text.replace("</tvshow>", f"  {repl}\n</tvshow>", 1)


def _drop_uniqueid(text, kind):
    """Remove every `<uniqueid type="kind">` element (a disproved provider id)."""
    return re.sub(rf'<uniqueid[^>]*type="{kind}"[^>]*>.*?</uniqueid>\s*', "",
                  text, flags=re.S | re.I)


def _download_bytes(url, timeout=60):
    if not url:
        return None
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": getattr(config, "USER_AGENT", None)
                          or "Torrent-Ingest/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310
            return r.read()
    except Exception:                                            # noqa: BLE001
        return None


def _write_bytes_atomic(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _rewrite_series_identity(show_dir, ident):
    """Rewrite tvshow.nfo identity fields from a verified TMDB record.

    Surgical: plot, genres, actors, ratings and lockdata are untouched. The
    show-level `<season>`/`<episode>` keys go -- Jellyfin's saver leaves them at -1
    after a null-index scrape and they render the "Season Unknown" ghost row -- as do
    the legacy `<id>`/`<episodeguide>` TVDB elements. Returns 1 when written.
    """
    nfo = Path(show_dir) / "tvshow.nfo"
    try:
        text = nfo.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 0
    if not text:
        return 0
    name = ident.get("name") or None
    first = ident.get("first_air_date") or None
    text = _set_xml_tag(text, "title", name, "tvshow")
    text = _set_xml_tag(text, "originaltitle", ident.get("original_name") or name,
                        "tvshow")
    text = _set_xml_tag(text, "year", ident.get("year"), "tvshow")
    text = _set_xml_tag(text, "premiered", first, "tvshow")
    text = _set_xml_tag(text, "releasedate", first, "tvshow")
    # `enddate` carries the old identity's last air date (TZ held Too Cute's
    # 2013-03-06) and would otherwise survive every other field being corrected.
    text = _set_xml_tag(text, "enddate", ident.get("last_air_date") or None, "tvshow")
    if ident.get("tvdb_id"):
        text = _set_xml_tag(text, "tvdbid", ident.get("tvdb_id"), "tvshow")
        text = _set_uniqueid(text, "tvdb", ident.get("tvdb_id"), default=True)
    else:
        # The verified identity has NO TVDB id. A stale `<tvdbid>` must not
        # survive: Jellyfin keys a series' presentation on its provider ids, so
        # keeping another show's tvdb id here merges the two series' seasons and
        # episodes (the 2011/2025 Gumball mixup). Remove both spellings.
        text = _drop_xml_tag(text, "tvdbid")
        text = _drop_uniqueid(text, "tvdb")
    text = _set_xml_tag(text, "tmdbid", ident.get("tmdb_id"), "tvshow")
    text = _set_uniqueid(text, "tmdb", ident.get("tmdb_id"))
    text = re.sub(r"<season>-?\d+</season>\s*", "", text, flags=re.I)
    text = re.sub(r"<episode>-?\d+</episode>\s*", "", text, flags=re.I)
    text = re.sub(r"<id>\d+</id>\s*", "", text, flags=re.I)
    text = re.sub(r"<episodeguide>.*?</episodeguide>\s*", "", text, flags=re.S | re.I)
    library._atomic_write(nfo, text)
    return 1


def _rewrite_season_identity(show_dir, tmdb_id):
    """Recompute every `season.nfo` year/dates from TMDB and lock it.

    The season year is the season's OWN air date, not the series premiere. Locking is
    what stops Jellyfin re-stamping the old identity over the repair. Returns the
    number of season.nfo files written.
    """
    written = 0
    try:
        import tmdbguide
    except Exception:                                            # noqa: BLE001
        return 0
    for sd in sorted(Path(show_dir).glob("Season *")):
        m = re.search(r"(\d+)", sd.name)
        season_nfo = sd / "season.nfo"
        if not m or not season_nfo.exists():
            continue
        try:
            text = season_nfo.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if not text:
            continue
        info = tmdbguide.season_info(tmdb_id, int(m.group(1)))
        if not info:
            continue
        text = _set_xml_tag(text, "year", info.get("year"), "season")
        text = _set_xml_tag(text, "premiered", info.get("air_date"), "season")
        text = _set_xml_tag(text, "releasedate", info.get("air_date"), "season")
        if "<lockdata>" in text:
            text = re.sub(r"<lockdata>.*?</lockdata>", "<lockdata>true</lockdata>",
                          text, count=1, flags=re.S | re.I)
        library._atomic_write(season_nfo, text)
        written += 1
    return written


def _replace_title_art(show_dir, tmdb_id, art_paths, ensure=()):
    """Overwrite contaminated title-level art with the verified identity's own.

    `art_paths` are the files to REPLACE (they exist and carry the old identity's
    bytes). `ensure` names files to (re)create even when absent -- a repair that
    re-identifies a show also restores its cover/backdrop, because Jellyfin's own
    refresh may remove the local file and not save a replacement. A file the
    provider has no image for is left alone: counted as failed only when it was a
    replacement, skipped when it was merely being ensured, so a show whose provider
    lacks a backdrop is not retried forever.

    Returns `(fixed, failed)`.
    """
    try:
        import tmdbguide
        urls = tmdbguide.art_urls(tmdb_id)
    except Exception:                                            # noqa: BLE001
        urls = None
    sd = Path(show_dir)
    paths = [Path(p) for p in (art_paths or ())]
    ensured = [sd / n for n in (ensure or ()) if (sd / n) not in paths]
    if not urls:
        return 0, len(paths)
    fixed = failed = 0
    for p in paths + ensured:
        name = p.name.lower()
        url = None
        m = re.match(r"season0*(\d+)-poster", name)
        if m:
            info = tmdbguide.season_info(tmdb_id, int(m.group(1)))
            url = info.get("poster_url") if info else None
        elif "landscape" in name or "backdrop" in name:
            url = urls.get("backdrop")
        else:
            url = urls.get("poster")
        data = _download_bytes(url)
        if not data:
            if p in ensured:
                continue                     # nothing to restore from; not a failure
            failed += 1
            continue
        try:
            _write_bytes_atomic(p, data)
            fixed += 1
        except OSError:
            failed += 1
    return fixed, failed


# --- locally-generated series cover ------------------------------------------
#
# The LAST-RESORT guarantee that no series ever renders a blank cover in the app.
# The no-provider class (One Pace, the YouTube-playlist shows, fan re-cuts like
# "Initial D Kaï (2026)") deliberately carries no TMDB/TVDB id, so RemoteImages has
# nothing to search against and neither the poster ladder nor an AI re-identification
# can conjure a provider poster -- the show sat `poster_missing` + `identity_missing`
# cycle after cycle, escalated twice to an AI that correctly reported "no entry
# exists", and stayed blank. This heals it mechanically: derive a cover from the
# show's OWN artwork, exactly as `onepace_thumbs.py` derives episode stills.
POSTER_GEN_W, POSTER_GEN_H = 680, 1020


def _generate_series_poster(show_dir):
    """Write a `folder.jpg` series cover from the show's own content, or return False.

    Source preference, both deliberately LOCAL reads so a cold pool-only video is never
    streamed just to grab one frame:
      1. an episode still sidecar (`Season */*-thumb.jpg`) -- artwork is never
         virtualized, so it is always on the real disk;
      2. otherwise, a frame from a genuinely-local episode video (mapped to the SSD
         lower when `show_dir` is a mediafs-mount path, which can present cold files).

    The chosen frame is centre-cropped to a 2:3 poster and scaled to 680x1020 (the
    standard `scale=…:force_original_aspect_ratio=increase,crop=…` cover crop, which is
    valid for both landscape stills and any portrait source). Idempotent: an existing
    `folder.jpg` short-circuits the caller before this ever runs, and the tmp-file write
    is atomic.
    """
    folder = Path(show_dir)
    src = None
    thumbs = sorted(folder.glob("Season */*-thumb.jpg"))
    if thumbs:
        src = thumbs[0]
    if src is None:
        video_root = folder
        try:
            mount = config.MEDIAFS_MOUNT
            if mount and (folder == mount or folder.is_relative_to(mount)):
                candidate = config.MEDIA_ROOT / folder.relative_to(mount)
                if candidate.is_dir():
                    video_root = candidate
        except ValueError:
            pass
        for v in sorted(_episode_videos(video_root)):
            try:
                if v.stat().st_size > 0:
                    src = v
                    break
            except OSError:
                continue
    if src is None:
        return False

    is_video = src.suffix.lower() in VIDEO_EXTS
    tmp = folder / "folder.tmp.jpg"
    try:
        args = ["ffmpeg", "-v", "error", "-y"]
        if is_video:
            args += ["-ss", "60"]       # past the OP for a typical episode
        args += ["-i", str(src), "-frames:v", "1",
                 "-vf", (f"scale={POSTER_GEN_W}:{POSTER_GEN_H}:"
                         "force_original_aspect_ratio=increase,"
                         f"crop={POSTER_GEN_W}:{POSTER_GEN_H}"),
                 "-q:v", "3", str(tmp)]
        r = subprocess.run(args, capture_output=True, timeout=300)
        if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            # ffmpeg refused (or a short video seeked past the end): fall back to the
            # source itself when it is already an image.
            if not is_video:
                shutil.copyfile(src, tmp)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if not is_video:
                shutil.copyfile(src, tmp)
        except OSError:
            pass
    if tmp.exists() and tmp.stat().st_size > 0:
        try:
            os.replace(tmp, folder / "folder.jpg")
            return True
        except OSError:
            pass
    try:
        tmp.unlink()
    except OSError:
        pass
    return False


# --- artwork truth -----------------------------------------------------------
#
# The failure this catches: a series' artwork is fetched under one identity and
# then NEVER replaced when the identity changes. Jellyfin only fills EMPTY image
# slots on an ordinary refresh, and `RemoteSearch/Apply` is called with
# `replaceAllImages=false`, so re-identifying a mis-matched show corrects its
# plots and titles while leaving the wrong show's poster, backdrop and episode
# stills sitting on disk forever. Nothing in the library reports it: every file
# is present, well-formed and the right resolution -- it is simply the wrong show.
#
# (Seen 2026-08-04, "The Seven Deadly Sins (2014)": pinned to TMDB 27240, a
# History Channel DOCUMENTARY, on 2026-07-30. Re-identifying it to TMDB 62104 --
# the anime -- fixed all 103 plots and titles, but the poster stayed the
# documentary's DVD cover, `backdrop.jpg` and S01E01-E07's stills stayed medieval
# woodcuts, and season 4's 24 episodes each carried the same season poster in
# place of a still. 34 files, all invisible to every existing check.)
#
# Two signals are used, because they fail independently:
#   * `artwork_identity_stale` -- the CAUSE. A provider id that was set and then
#     CHANGED means every image predating the change is from the other show.
#     Catches wrong-show art that is otherwise structurally perfect.
#   * `artwork_bogus` -- the SHAPE. An episode image that is portrait, or shared
#     by several episodes, or byte-identical to a poster, is not a still no matter
#     what produced it. Catches the provider's own fallbacks and any cause we
#     never thought of.

_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _jpeg_dims(path, probe=32768):
    """(width, height) from a JPEG's SOF header, reading only the head of the file.
    None if it is not a parseable JPEG. Deliberately header-only: this runs over the
    whole library, so it must never read a multi-megabyte image in full."""
    try:
        with open(path, "rb") as fh:
            d = fh.read(probe)
    except OSError:
        return None
    if not d.startswith(b"\xff\xd8"):
        return None
    i = 2
    while i < len(d) - 9:
        if d[i] != 0xFF:
            i += 1
            continue
        m = d[i + 1]
        if m in _SOF_MARKERS:
            h, w = struct.unpack(">HH", d[i + 5:i + 9])
            return (w, h)
        if m in (0x01, 0xFF) or 0xD0 <= m <= 0xD9:      # standalone: no length field
            i += 2
            continue
        if i + 4 > len(d):
            break
        i += 2 + struct.unpack(">H", d[i + 2:i + 4])[0]
    return None


def _file_md5(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _episode_art(show_dir):
    """Every episode image sidecar in a show, as [(path, size, mtime)].

    Dotfiles are skipped, and that is load-bearing: macOS drops an AppleDouble
    `._<name>` beside every file the mount writes, so `*-thumb.jpg` also matches
    `._... -thumb.jpg`. Those are all exactly 4096 bytes and byte-identical, which
    reads as "one image smeared across the whole season" -- a false positive on
    every show Jellyfin has just written artwork for. (`sweep_junk` deletes them
    each pass, but the detector must not be fooled in the window before it runs,
    and must never try to re-adopt one.)
    """
    out = []
    for p in sorted(show_dir.glob("Season */*-thumb.jpg")):
        if p.name.startswith("."):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        out.append((p, st.st_size, int(st.st_mtime)))
    return out


def _art_signature(art):
    return hashlib.md5(repr([(p.name, s, m) for p, s, m in art]).encode()).hexdigest()


def _scan_episode_art(show_dir, art):
    """[(path, reason)] -- episode images that are structurally not stills.

    Hashes are computed ONLY for files whose size collides with another candidate, so
    the common (healthy) case costs one `stat` and one header read per image.
    """
    if not art:
        return []

    bad = {}

    # 1. Portrait: a poster (or season poster) standing in for a 16:9 still.
    for p, _s, _m in art:
        dims = _jpeg_dims(p)
        if dims and dims[1] and dims[0] / dims[1] < 1.0:
            bad[p] = f"portrait {dims[0]}x{dims[1]} -- a poster, not an episode still"

    by_size = {}
    for p, s, _m in art:
        by_size.setdefault(s, []).append(p)

    # 2. One image smeared across many episodes (the provider had no real stills).
    for group in by_size.values():
        if len(group) < ART_DUP_MIN:
            continue
        same = {}
        for p in group:
            try:
                same.setdefault(_file_md5(p), []).append(p)
            except OSError:
                continue
        for dupes in same.values():
            if len(dupes) >= ART_DUP_MIN:
                for p in dupes:
                    bad.setdefault(
                        p, f"identical to {len(dupes) - 1} other episode image(s)")

    # 3. Byte-identical to the series poster or a season poster.
    posters = []
    for name in ("folder.jpg", "poster.jpg", "cover.jpg"):
        f = show_dir / name
        if f.exists():
            posters.append(f)
    posters += sorted(show_dir.glob("season*-poster.jpg"))
    poster_by_size = {}
    for f in posters:
        try:
            poster_by_size.setdefault(f.stat().st_size, []).append(f)
        except OSError:
            continue
    for size, group in by_size.items():
        if size not in poster_by_size:
            continue
        try:
            pset = {_file_md5(f) for f in poster_by_size[size]}
        except OSError:
            continue
        for p in group:
            try:
                if _file_md5(p) in pset:
                    bad[p] = "byte-identical to the series/season poster"
            except OSError:
                continue

    return sorted((str(p), r) for p, r in bad.items())


def _art_verdict(show_dir, cache):
    """Cached `_scan_episode_art`. Re-reads images only when the signature moved."""
    key = show_dir.name
    art = _episode_art(show_dir)
    sig = _art_signature(art)
    hit = cache.get(key)
    if hit and hit.get("sig") == sig:
        return [tuple(x) for x in hit.get("bad", [])]
    bad = _scan_episode_art(show_dir, art)
    cache[key] = {"sig": sig, "bad": [list(x) for x in bad]}
    return bad


def _refused_ts(rec):
    """When an image's refusal was recorded; 0.0 when it was never refused.

    Entries written by the first deployment are bare timestamps; entries written since
    the repair learned to distinguish its two refusal reasons are `{"ts", "url",
    "reason"}` dicts. Both are understood, so an upgrade does not silently un-refuse
    every image the fleet already gave up on.
    """
    if isinstance(rec, dict):
        rec = rec.get("ts")
    try:
        return float(rec or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _drop_refused_art(art_bad, show_state, now=None):
    """Drop bad images whose provider-best answer is known not to fix them.

    The shape check is a fact -- this image is not a still -- but the repair asks the
    provider for a real one, and for some episodes the provider has nothing BETTER to
    give: no landscape still at all, or one shared image re-used for every missing
    episode (classic serials are the shape; the rule names no series). Re-adopting the
    same shared image rewrites the same bytes, the shape check re-fires, and the report
    lists a problem whose only repair has already been attempted. That is §5's lesson
    ("87 items ... they were not 87 problems"): a report that lists what cannot be acted
    on buries what can.

    So a refusal is remembered per IMAGE, for `ART_NO_STILL_TTL_SEC`, and an image is
    only dropped while its refusal is fresh. A different episode next to it is
    unaffected, and a still the provider adds later is picked up after the expiry.
    """
    seen = (show_state or {}).get("art_no_still") or {}
    if not art_bad or not seen:
        return list(art_bad)
    fresh = (time.time() if now is None else now) - ART_NO_STILL_TTL_SEC
    return [(pth, r) for pth, r in art_bad if _refused_ts(seen.get(pth)) < fresh]


def _art_next_action(best_url, tried_url):
    """What to do with one bogus episode image. Computed from the provider's own answer.

    `no-still`    -- the provider genuinely has no landscape still; nothing to fetch.
    `already-tried` -- the provider's current best answer was already adopted and the
                    shape persisted (measured: TMDB re-uses one still across every
                    missing episode of a serial). Fetching it again cannot change the
                    bytes; the caller remembers the refusal instead of looping.
    `adopt`       -- an answer not yet applied (first attempt, or a NEW best still the
                    provider gained since -- the retry is immediate, not TTL-bound).
    """
    if not best_url:
        return "no-still"
    if tried_url and best_url == tried_url:
        return "already-tried"
    return "adopt"


# Which provider ids identify a show. A key going from ABSENT to SET is the
# "series had no identity" case -- its artwork was supplied locally and must be
# KEPT. A key that was set and then CHANGED is a re-match: the old artwork is the
# other show's and must go.
IDENTITY_KEYS = ("Tmdb", "Tvdb", "Imdb")


def _identity_changed(old, new):
    """The provider key whose id was REPLACED (not merely filled in), or None."""
    for k in IDENTITY_KEYS:
        o, n = (old or {}).get(k), (new or {}).get(k)
        if o and n and str(o) != str(n):
            return f"{k} {o} -> {n}"
    return None


def _newest_mtime(paths):
    newest = 0.0
    for p in paths:
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            pass
    return newest


def _nfo_episode_slot(video_path):
    """(season, episode) an episode file's OWN `.nfo` claims, or None.

    The coverage check below reads the FILENAME to decide which episode a file is. The
    `.nfo` beside it is the fleet's own record of what the file actually CONTAINS, written
    by identify at placement time -- and when the two disagree, the filename is the one
    that is wrong. Reading only the filename is why a whole misfiled season reports as
    "duplicate episodes": on 2026-09-07, 82 of 86 NEEDS REVIEW items were two shifted file
    sets whose every `.nfo` named the correct episode, and acting on that report as
    written would have deleted real episodes (§4.4 -- an answer the check never asked for
    is not evidence).
    """
    nfo = Path(os.path.splitext(str(video_path))[0] + ".nfo")
    try:
        text = nfo.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    ms = re.search(r"<season>\s*(\d+)\s*</season>", text)
    me = re.search(r"<episode>\s*(\d+)\s*</episode>", text)
    if not ms or not me:
        return None
    return int(ms.group(1)), int(me.group(1))


# --- a sidecar carrying a DIFFERENT episode's identity -------------------------
#
# One Pace S13E05, 2026-09-10. The library held two files: `S13E05 - Inherited Will.mkv`
# and `S13E05 - Quack Doctor.mkv`. Both sidecars said "Quack Doctor", so the collision
# check reported two files covering one slot and could not say which was wrong.
#
# The cause, from the fleet's own decisions log: "Quack Doctor" is One Pace's OLDER cut of
# that arc position (ch. 141-145) and "Inherited Will" is the newer one (ch. 140-145). When
# the newer cut was filed into the slot it inherited the pre-seeded sidecar's title instead
# of being given its own. The filename was right; the sidecar named the episode it replaced.
#
# `_title_is_janky` cannot catch this -- "Quack Doctor" is a perfectly good-looking title,
# just the wrong one. What gives it away is that it contradicts the filename, and the
# filename was authored by the placement plan.
#
# WHY THIS DOES NOT INVERT §4.26. That lesson says believe the `.nfo` over the filename,
# and it stands: the sidecar is normally the fleet's own record of what a file contains.
# But a sidecar can be overwritten by a later metadata repair or inherited from a
# pre-seeded one, while the FILENAME is written once, by `apply_plan`, from the identify
# plan. So the tie is broken by a third witness rather than by preferring one guess: the
# ingest JOURNAL, which records the destination every plan actually filed. Filename and
# journal agreeing against the sidecar is evidence; filename alone is not, and is reported
# for review instead of repaired.

_JOURNAL_DSTS = None


def _journal_destinations():
    """Every `dst_rel` the ingest journal records as filed. Loaded once."""
    global _JOURNAL_DSTS
    if _JOURNAL_DSTS is not None:
        return _JOURNAL_DSTS
    out = set()
    jp = Path.home() / "Developer" / "Media-Orchestrator" / "Torrent-Ingest" / "state" / "journal.jsonl"
    try:
        for line in jp.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or "dst_rel" not in line:
                continue
            try:
                rec = json.loads(line)
            except Exception:                                        # noqa: BLE001
                continue
            for f in (rec.get("plan") or {}).get("files") or rec.get("applied") or []:
                d = f.get("dst_rel")
                if d:
                    out.add(str(d))
    except OSError:
        pass
    _JOURNAL_DSTS = out
    return out


def _rel_from_library(path):
    """A library-relative path for an absolute path under the mount or the SSD."""
    p = str(Path(path))
    for root in (config.MEDIAFS_MOUNT, config.MEDIA_ROOT):
        try:
            return str(Path(p).resolve().relative_to(Path(root).resolve()))
        except (ValueError, OSError):
            continue
    for marker in ("/MediaLibrary/", "/Media/"):
        if marker in p:
            return p.split(marker, 1)[-1]
    return None


def _journal_source_titles():
    """{library-relative dst: episode title the plan that filed it came from}.

    Thin wrapper over `journal.source_titles()` (the journal owns the cache and the
    parse). See `_classify_slot_collision` for why a same-stem pair needs this witness.
    """
    import journal                                                     # noqa: PLC0415
    return journal.source_titles()


def _norm_title(t):
    return re.sub(r"[^a-z0-9]+", "", str(t or "").lower())


# A "title" the filename extractor produced that is really just part of the episode
# marker. `Psych (2006) - S07E15-E16.mkv` carries no title at all, but the text after the
# last " - " is "E16", which reads as a title to anything looking for one -- and then
# contradicts the sidecar's real title, reporting a correctly-named multi-episode file as
# a fault. Found the first time this check ran over the live library.
_MARKER_ONLY_TITLE = re.compile(
    r"^(?:"
    r"[Ss]\d{1,4}[Ee]\d{1,4}(?:\s*[-–]\s*[Ee]?\d{1,4})?"   # S07E15, S07E15-E16
    r"|[Ee]\d{1,4}(?:\s*[-–]\s*[Ee]?\d{1,4})?"              # E16, E15-E16
    r"|\d{1,4}(?:\s*[-–]\s*\d{1,4})?"                       # 16, 15-16
    r"|[Pp]art\s*\d+|[Vv]ol\.?\s*\d+|[Cc]h(?:apter)?\.?\s*\d+"
    r")$")


def _title_contradicts_filename(fn_title, nfo_title):
    """True when both titles look real but name DIFFERENT episodes.

    Deliberately conservative. A sanitisation or truncation variant of the same title is
    NOT a contradiction -- the library rewrites `Ghostf**kers` to `Ghostf--kers` and
    shortens long names -- so an exact match, a prefix either way, or a close ratio all
    pass. Only a title that is plainly a different string is reported.
    """
    import difflib
    fn_title = str(fn_title or "").strip()
    nfo_title = str(nfo_title or "").strip()

    # A marker fragment is not a title, and comparing one against a real title reports a
    # contradiction that does not exist.
    if _MARKER_ONLY_TITLE.match(fn_title):
        return False

    # A MULTI-EPISODE filename leaves its second marker glued to the front of the title:
    # `... - S03E10-E11 - The Day of Black Sun` yields "E11 - The Day of Black Sun".
    # Strip that so the titles are actually comparable.
    fn_title = re.sub(r"^[Ss]?\d*[Ee]\d{1,4}\s*[-–]\s*", "", fn_title).strip()
    if not fn_title or _MARKER_ONLY_TITLE.match(fn_title):
        return False

    a, b = _norm_title(fn_title), _norm_title(nfo_title)
    if not a or not b or a == b:
        return False
    if a.startswith(b) or b.startswith(a):
        return False

    # A multi-episode sidecar carries BOTH episodes' titles, joined: "The Boiling Rock (1)
    # / The Boiling Rock (2)", while the filename carries the shared name once. Matching
    # either half is agreement, not contradiction -- and the filename often repeats the
    # name twice for the same reason ("The Day of Black Sun & The Day of Black Sun").
    parts = [x for x in re.split(r"\s*/\s*|\s+&\s+", nfo_title) if x.strip()]
    fn_parts = [x for x in re.split(r"\s*/\s*|\s+&\s+", fn_title) if x.strip()]
    for fp in (fn_parts or [fn_title]):
        fa = _norm_title(re.sub(r"\s*\(\d+\)\s*$", "", fp))
        for q in parts:
            qb = _norm_title(re.sub(r"\s*\(\d+\)\s*$", "", q))
            if not fa or not qb:
                continue
            if fa == qb or fa.startswith(qb) or qb.startswith(fa):
                return False
            if difflib.SequenceMatcher(None, fa, qb).ratio() >= 0.6:
                return False
    return difflib.SequenceMatcher(None, a, b).ratio() < 0.6


def _journal_confirms_filename(vpath):
    """Whether the ingest journal records this exact destination as one it filed."""
    try:
        rel = str(Path(vpath).resolve()).split("/MediaLibrary/", 1)[-1]
        rel = rel.split("/Media/", 1)[-1]
    except Exception:                                                # noqa: BLE001
        return False
    dsts = _journal_destinations()
    return rel in dsts or any(d.endswith("/" + Path(vpath).name) for d in dsts)


def _nfo_covered_span(video_path, name_span):
    """(season, first, last) that a file's own `.nfo` says it covers, or None.

    A MULTI-EPISODE file (`SxxE01-E02 - A & B.mkv`) carries ONE `.nfo`, and by Jellyfin's
    convention that sidecar names only the FIRST episode in the file. Its coverage is
    therefore the nfo's episode number widened by however many episodes the FILENAME says
    are inside -- not the single slot the nfo literally names.

    Reading the nfo's bare number as the file's whole claim is what made every second slot
    of every pair file report as a placement fault: `S01E01-E02`'s nfo says `<episode>1`,
    slot E02 asked "do you claim E02?", the nfo said "I am E01", and the check concluded
    the file was misfiled and told the owner to re-file a correctly-placed file. On The
    Powerpuff Girls alone that was ~37 of the 75 NEEDS REVIEW items, every one of them
    false, and acting on them as written would have moved good files off their slots.

    This is §4.26 one level deeper. That lesson fixed a check that read the FILENAME when
    it should have read the file's own record; this fixes the same check reading that
    record without understanding what it is a record OF. Believing the `.nfo` is only
    right when you have understood what it claims.
    """
    slot = _nfo_episode_slot(video_path)
    if not slot:
        return None
    season, first = slot
    width = (name_span[2] - name_span[1]) if name_span else 0
    return season, first, first + width


def _nfo_covers(nfo_span, season, episode):
    """Whether a file's own `.nfo` claims the given slot."""
    return (nfo_span is not None
            and nfo_span[0] == season
            and nfo_span[1] <= episode <= nfo_span[2])


def _slot_disagreement(video_path, name_span):
    """`(season, episode)` the sidecar claims when it disagrees with the filename.

    HANDOFF 15.5's guard: a filed episode's `.nfo` `<season>/<episode>` must agree with
    the slot its DESTINATION filename states. On a library with its own specials scheme
    the sidecar routinely carries a FOREIGN provider number (Doctor Who S00E04
    *Return Of Doctor Mysterio*: `<episode>149</episode>`), and a wrong-slot file carries
    the number of the episode it really is (*The End Of Time*: `<episode>16</episode>`).
    Reported, not auto-rewritten -- the repair must compute the true slot first.
    Multi-episode files are exempt: their sidecar names the FIRST slot by convention.
    """
    slot = _nfo_episode_slot(video_path)
    if not slot or not name_span:
        return None
    if (slot[0], slot[1]) == (name_span[0], name_span[1]):
        return None
    return slot


def _parse_span(name):
    m = EP_SPAN_RE.search(name)
    if not m:
        return None
    s = int(m.group(1)); a = int(m.group(2)); b = int(m.group(3)) if m.group(3) else a
    return s, a, b


def _classify_slot_collision(prev, v, season, episode):
    """`(kind, detail, auto)` for two files claiming one SxxExx.

    Three pieces of identity evidence, in order, and the verdict is only `auto` (a
    mechanical keep-one/delete-one) when they agree the two files are the SAME episode:

      1. each file's own `.nfo` span (`_nfo_covered_span`): different episodes is a
         `misfiled_episode`, never a duplicate;
      2. for SAME-STEM pairs there is one shared `.nfo`, which by definition cannot
         separate them -- so the journal's source title per destination decides
         (`_journal_source_titles`). A disagreement is a `misfiled_episode`;
      3. anything unproven stays `duplicate_episode` with `auto=False`: reported, not
         deleted.

    The Smurfs (1981) loss of 2026-09-20 is the case: `S01E01.mkv` (content *The
    Smurfette*) and `S01E01.mp4` (content *The Astrosmurf*, the plan's file) shared a
    stem and a sidecar, the old rule kept the higher-ranked container and deleted the
    plan's copy, and 38 episodes of the replacement pack were gone.
    """
    a_span = _nfo_covered_span(prev, _parse_span(Path(prev).name))
    b_span = _nfo_covered_span(v, _parse_span(Path(v).name))
    misfiled = [(pth, sp) for pth, sp in ((prev, a_span), (v, b_span))
                if sp is not None and not _nfo_covers(sp, season, episode)]
    if a_span and b_span and misfiled:
        detail = "; ".join(
            f"{Path(pth).name!r} is really S{sp[0]:02d}E{sp[1]:02d}"
            for pth, sp in misfiled)
        return ("misfiled_episode",
                f"S{season:02d}E{episode:02d} has two files, but their own .nfo sidecars "
                f"name DIFFERENT episodes -- {detail}. This is a placement fault, NOT a "
                f"duplicate: re-file it, do not delete it.", False)
    if Path(prev).stem != v.stem:
        return ("duplicate_episode",
                f"S{season:02d}E{episode:02d} covered by two files: "
                f"{Path(prev).name!r} and {Path(v).name!r} (different stems; this is a "
                f"judgment call, left for review)", False)
    book = _journal_source_titles()
    id_a = book.get(_rel_from_library(prev) or "")
    id_b = book.get(_rel_from_library(v) or "")
    if id_a and id_b and _norm_title(id_a) != _norm_title(id_b):
        return ("misfiled_episode",
                f"S{season:02d}E{episode:02d} has two same-stem files and one shared .nfo, "
                f"so the sidecar cannot separate them; the ingest journal says "
                f"{Path(prev).name!r} is {id_a!r} and {Path(v).name!r} is {id_b!r}. "
                f"This is a placement fault, NOT a duplicate: re-file it, do not delete it.",
                False)
    auto = bool(id_a and id_b)
    return ("duplicate_episode",
            f"S{season:02d}E{episode:02d} covered by two files: "
            f"{Path(prev).name!r} and {Path(v).name!r}"
            + ("" if auto else " (identity not proven; left for review)"),
            auto)


def _filename_real_title(stem):
    m = FILENAME_TITLE_RE.search(stem)
    if not m:
        return None
    t = m.group(1).strip(" -")
    if not t or _title_junk_markers(t) or GENERIC_TITLE_RE.match(t):
        return None
    return t


def _title_is_janky(title, stem, generic_ok=False):
    """Whether `title` is release-group/encoding junk rather than a real episode title.

    `generic_ok` suppresses the GENERIC_TITLE_RE rule ("Episode 7", "Chapter 3"). A generic
    title is only a defect when the provider HAS a real one; plenty of seasons are officially
    untitled, and for those "Episode 7" is the canonical answer, so flagging it produces a
    NEEDS REVIEW item that can never be resolved. See `_season_generic_is_canonical`.
    """
    if not title:
        return True
    if _title_junk_markers(title):
        return True
    if not generic_ok and GENERIC_TITLE_RE.match(title):
        return True
    if title.strip() == stem.strip():
        return True
    return False


def _season_generic_is_canonical(videos, jf_name):
    """Seasons whose rendered titles are UNIFORMLY generic -> that is the provider's real answer.

    The heuristic: if every episode of a season renders as "Episode N" (or has no title at all),
    the metadata provider genuinely has no titles for it and nothing is wrong. If only SOME are
    generic while siblings carry real titles, the generic ones really are missing and stay flagged.

    (Seen 2026-08-04: The Promised Neverland S2 and Marvel Zombies S1 came back from a full TMDB
    refresh, WITH overviews, still titled "Episode 1..N" -- because that is what TMDB has. They
    had been reported as "episode(s) with release-group/blank titles ... needs a lookup" on every
    pass. Note TPN S1 is the mirror-image trap: its canonical titles are bare numbers like
    "121045" -- real titles that merely look like junk.)
    """
    by_season = {}
    for v in videos:
        span = _parse_span(v.name)
        if not span:
            continue
        season = span[0]
        title = jf_name.get(str(v.resolve()))
        if title is None:
            continue                      # not in Jellyfin yet; says nothing about the season
        by_season.setdefault(season, []).append(title.strip())
    canonical = set()
    for season, titles in by_season.items():
        if len(titles) < 2:
            continue                      # one sample proves nothing
        if all(not t or GENERIC_TITLE_RE.match(t) for t in titles):
            canonical.add(season)
    return canonical


# --- junk sweep (always auto, library-wide) ----------------------------------

def sweep_junk(dry_run):
    """Delete macOS AppleDouble (`._*`) and `.DS_Store` litter across the SSD
    store and every attached drive. Pure junk; always safe."""
    removed = 0
    roots = [config.MEDIA_ROOT] + _drive_media_roots()
    for root in roots:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if p.name == ".DS_Store" or p.name.startswith("._"):
                if dry_run:
                    removed += 1
                    continue
                try:
                    p.unlink()
                    removed += 1
                except OSError:
                    pass
    if removed:
        _log(f"junk sweep: {'would remove' if dry_run else 'removed'} {removed} AppleDouble/.DS_Store file(s)")
    return removed


# --- the problem-dict contract -----------------------------------------------
#
# Every diagnosis pass -- shows, movies, and anything added later -- returns dicts
# of the same shape, because `run_once` pools them all into one `worklist` and the
# escalation block reads that pool without knowing which pass produced an entry.
# The required keys are:
#
#   show      display name, and the key under which `state` remembers this item
#   path      the thing on disk, for the escalation prompt
#   problems  list of {kind, detail, auto, sev}
#   sig       identity of what is on disk RIGHT NOW (see below)
#
# `sig` is the ladder-reset signal: while it holds steady the fix ladder and the
# per-item escalation count keep advancing, and when it moves the item is treated
# as new and both reset. Each pass picks the cheapest value that actually moves
# when the content does -- episode count for a show, (size, mtime) for a single
# film. A pass that omits `sig` breaks escalation for the WHOLE cycle, not just
# its own entries, so give every dict one at construction.

def _file_sig(path):
    """Signature of one file's content identity: size and mtime, never a read."""
    try:
        st = Path(path).stat()
    except OSError:
        return "missing"
    return f"{st.st_size}:{int(st.st_mtime)}"


# --- per-show diagnosis ------------------------------------------------------

def diagnose_show(show_dir, jf, series_by_path, pids_by_id=None,
                  state=None, art_cache=None, tvdb_owners=None):
    """Return a dict of problems for one show, comparing disk to Jellyfin."""
    videos = _episode_videos(show_dir)
    disk_count = len(videos)
    newest = _newest_mtime(videos)
    age = time.time() - newest if newest else 1e12
    sid = series_by_path.get(str(show_dir).rstrip("/"))

    probs = {"show": show_dir.name, "path": str(show_dir), "sid": sid,
             "disk_count": disk_count, "age": age, "sig": str(disk_count),
             "problems": []}

    def add(kind, detail, auto, sev=1, **extra):
        probs["problems"].append({"kind": kind, "detail": detail, "auto": auto,
                                  "sev": sev, **extra})

    if sid is None:
        # A folder with no Jellyfin item AND no episodes on disk is empty / metadata-only,
        # not a defect. A folder with episodes on disk but no item is the real series_missing.
        if disk_count == 0:
            return probs
        # ...UNLESS we told Jellyfin to skip it. `.ignore` is `purge_sweeper`'s marker for
        # "this title is blocklisted and the reaper is draining it" (§4.164), and Jellyfin
        # honours it, so the series legitimately has no item. Proposing a library scan here
        # is worse than useless: a scan CANNOT adopt a folder carrying `.ignore`, so the
        # remedy fires every cycle, never works, and reports the same fault forever while
        # looking like it is handling it -- a check that "fails soft" and therefore stops
        # checking (§4.113).
        #
        # And it is not always a purge. A blocklist key is a PREFIX matcher, so a bare
        # franchise word marks the ORIGINAL series when only the remake was purged: on
        # 2026-09-05 Ranma ½ (1989), Sailor Moon (1992) and Rurouni Kenshin (1996) were
        # carrying this marker with 479 intact episodes and no pool deletion queued
        # (§4.182). So say what the marker is and let a human read it, rather than
        # quietly proposing the wrong repair.
        if (show_dir / ".ignore").exists():
            add("series_ignored",
                f"{disk_count} episode(s) on disk and NO Jellyfin item because this folder "
                f"carries a `.ignore` marker (purge_sweeper writes it for a BLOCKLISTED "
                f"title). A library scan cannot adopt it. If this title is being kept, the "
                f"blocklist entry is matching it by prefix -- run "
                f"scripts/audit_blocklist_collisions.py",
                auto=False, sev=3)
            return probs
        add("series_missing", f"{disk_count} episode(s) on disk, no Jellyfin series item",
            auto=True, sev=3)
        return probs   # nothing else queryable until the item exists

    # A series Jellyfin presents is broken when its cover is blank -- EVEN when every
    # episode is pool-only (not hydrated to the local mount). The old `disk_count == 0`
    # early-return here skipped this check, so shows whose .nfo sidecars are local but
    # whose videos live only in the pool (Father Ted, That '90s Show, The Haunting of
    # Hill House / Bly Manor) rendered a blank cover in the app and the doctor never once
    # looked. The poster check must run regardless of how many episodes happen to be local.
    has_primary = "Primary" in jf.image_types(sid)
    if not has_primary:
        add("poster_missing", "Jellyfin has no Primary image for this series",
            auto=True, sev=2)

    # A series with NO scrapeable identity (no Tmdb/Tvdb in Jellyfin OR in its own
    # tvshow.nfo) AND no art is the ROOT CAUSE of a cluster of symptoms -- blank poster,
    # junk titles, missing plots -- because there is nothing to scrape against, and the
    # mechanical poster/title ladders cannot heal it (RemoteImages searches by identity).
    # This is a JUDGMENT case, so it is non-auto and escalates to the AI fixer to
    # (re-)identify the show. The `not has_primary` guard is what keeps this from
    # flagging a series that is INTENTIONALLY identity-less but fine: One Pace has no
    # TMDB yet serves its own art through the One Pace plugin, and the YouTube-playlist
    # shows (Star Wars ToR, Hazbin, ...) carry no TMDB by design. "No TMDB" is only a
    # defect when the poster is also missing. Youjo Shenki and Eureka Seven Hi-Evolution
    # Zero were the canaries: both sat "poster_missing" every cycle, un-escalated, forever
    # -- the missing poster was the visible symptom of a missing identity the doctor
    # never named.
    cur_pids = (pids_by_id or {}).get(sid) or {}
    if not (cur_pids.get("Tmdb") or cur_pids.get("Tvdb")) \
            and not _nfo_provider_ids(show_dir) \
            and not has_primary:
        add("identity_missing",
            "series has no TMDB/TVDB id in Jellyfin or its tvshow.nfo -- nothing to "
            "scrape poster/metadata against; needs (re-)identification",
            auto=False, sev=3)

    # Series-level identity and title art (HANDOFF 10.3). Runs BEFORE the pool-only
    # early return: a series whose videos are all evicted still shows the contaminated
    # nfo and cover in the app. The trigger is computed against TMDB (`premiered` vs
    # `first_air_date`); everything else in the nfo is a repair target, not a trigger.
    try:
        ident_hit = _series_identity_problem(show_dir, cur_pids, tvdb_owners)
    except Exception as e:                                        # noqa: BLE001
        _log(f"  {show_dir.name}: series identity check failed ({e})")
        ident_hit = None
    if ident_hit:
        detail, ident = ident_hit
        present_art = [str(p) for p in _title_art_files(show_dir)]
        add("series_identity_stale", detail, auto=True, sev=3, ident=ident)
        if present_art:
            add("series_art_stale",
                f"{len(present_art)} title-level image(s) (cover/backdrop/season "
                f"poster) were fetched under the contaminated identity and are the "
                f"other show's", auto=True, sev=3, art=present_art, ident=ident)
    else:
        # A previous pass corrected the identity but could not fetch every image
        # (provider blip). The identity trigger is gone, so without this the
        # contaminated bytes would stay forever -- keep the job alive.
        st_show = (state or {}).get(show_dir.name) or {}
        pending = st_show.get("art_pending") or {}
        pend_art = [p for p in (pending.get("art") or []) if Path(p).exists()]
        if pend_art:
            add("series_art_stale",
                f"{len(pend_art)} title-level image(s) still carry the contaminated "
                f"identity's art (a previous provider fetch failed)",
                auto=True, sev=3, art=pend_art, ident=pending.get("ident") or {})
        else:
            # The identity was repaired but a later Jellyfin image refresh removed
            # the local cover/backdrop without saving replacements (measured
            # 2026-09-20 on TZ: folder.jpg and landscape.jpg both vanished). The
            # repaired identity is the standing fact; restore the missing art.
            fixed_ident = (st_show.get("identity_fixed") or {}).get("ident") or {}
            if not fixed_ident.get("tmdb_id") and st_show.get("identity_fixed_ts"):
                fixed_ident = {"tmdb_id": _nfo_identity(show_dir).get("tmdbid")}
            if fixed_ident.get("tmdb_id"):
                missing = [n for n in ("folder.jpg", "landscape.jpg")
                           if not (Path(show_dir) / n).exists()]
                if missing:
                    add("series_art_stale",
                        f"title art missing after an identity repair ({', '.join(missing)}) "
                        f"-- the verified identity's own art is restored",
                        auto=True, sev=2, art=[], ident=fixed_ident)

    if disk_count == 0:
        return probs   # pool-only episodes; nothing further to reconcile on disk this pass

    eps = jf.episodes(sid)
    jf_with_path = [e for e in eps if e.get("Path")]
    jf_count = len(jf_with_path)

    if jf_count < disk_count:
        add("episodes_missing",
            f"Jellyfin resolved {jf_count} of {disk_count} on-disk episodes",
            auto=True, sev=3, jf_count=jf_count)

    # --- artwork: is it even this show's? (see the "artwork truth" block above) ---
    #
    # The CAUSE check. A provider id that was set and then changed means the series was
    # re-matched, and every image fetched under the old id is the other show's. Jellyfin
    # will not replace them on its own -- an ordinary refresh only fills empty slots --
    # so this is the one moment the drift is knowable, and it must be acted on.
    cur_pids = (pids_by_id or {}).get(sid) or {}
    seen_pids = ((state or {}).get(show_dir.name) or {}).get("pids") or {}
    changed = _identity_changed(seen_pids, cur_pids)
    if changed:
        add("artwork_identity_stale",
            f"series identity changed ({changed}) -- all artwork predating the change is "
            f"the previously-matched show's and will never be replaced on its own",
            auto=True, sev=3, pids=cur_pids)

    # The SHAPE check, independent of cause: an episode image that is a poster, is
    # shared by several episodes, or equals a season poster is not a still.
    if art_cache is not None:
        art_bad = _art_verdict(show_dir, art_cache)
        # DROP the ones whose provider-best answer is already known not to fix them.
        #
        # Detection and repair disagreed, and the report paid for it. The shape check is
        # a fact -- this image is not a still -- but the repair then asks the provider for
        # a real one, and for 19 of the 21 lines in `library_health.txt` on 2026-09-12 the
        # provider had nothing. So every cycle re-flagged a problem whose only possible
        # fix had already been attempted and refused, and the report read as twenty shows
        # in trouble when one movie was the only thing a human could act on.
        #
        # The refusal is REMEMBERED per image (see `_drop_refused_art` for both shapes:
        # no still at all, or the provider's own still re-used across episodes so that
        # adoption rewrites the same bytes and the shape persists), and it expires after
        # `ART_NO_STILL_TTL_SEC` because a provider's catalogue is a fact about a moment,
        # not about physics -- stills do get added. Keyed per IMAGE, never per show, so
        # one unfixable still cannot hide a fixable one that appears next to it later.
        art_bad = _drop_refused_art(art_bad, (state or {}).get(show_dir.name))
        if art_bad:
            reasons = sorted({r.split(" --")[0].split(" (")[0] for _p, r in art_bad})
            add("artwork_bogus",
                f"{len(art_bad)} episode image(s) are not real stills "
                f"({'; '.join(reasons)})",
                auto=True, sev=2, art_bad=art_bad)

    # Duplicate episodes on disk: two DIFFERENT files covering the same SxxExx.
    # When they share a stem (the same episode in two containers, e.g. `...E07.mkv` and
    # `...E07.mp4`), the "which to keep" decision is mechanical -- keep the higher-quality
    # copy, drop the lower -- so it is auto. Different stems (a mis-numbered file, a split
    # part) is a genuine judgment call and stays auto=False for escalation.
    span_owner = {}
    for v in videos:
        span = _parse_span(v.name)
        if not span:
            continue
        s, a, b = span
        for n in range(a, b + 1):
            key = (s, n)
            if key in span_owner and span_owner[key] != v:
                prev = span_owner[key]
                kind, detail, auto = _classify_slot_collision(prev, v, s, n)
                add(kind, detail, auto=auto, sev=3 if not auto else 2,
                    files=[str(prev), str(v)])
            else:
                span_owner.setdefault(key, v)

    # 0-byte / unreadable episode videos. A 0-byte file is pure garbage and is auto-deleted;
    # an unreadable one might just be a mount/permission blip, so it escalates.
    for v in videos:
        try:
            if v.stat().st_size == 0:
                add("inaccessible", f"{v.name} is 0 bytes", auto=True, sev=2, path=str(v))
        except OSError:
            add("inaccessible", f"{v.name} is unreadable", auto=False, sev=2, path=str(v))

    # Janky episode titles -- checked from BOTH the on-disk .nfo <title> AND the
    # title Jellyfin actually RENDERS (what the app shows), so junk is caught
    # whichever side carries it. The common case is release-group junk
    # ("[Judas] x265 10b") the identify step locked into the .nfo; a later refresh
    # then reads that junk over a good scraped title, which is how a title that
    # "was fine" silently reverts.
    jf_name = {}
    jf_slot_null = 0
    for e in jf_with_path:
        try:
            jf_name[str(Path(e["Path"]).resolve())] = e.get("Name") or ""
            # A filename that states SxxEyy but an item with a NULL index: Jellyfin
            # renders the series name for the episode and can hold a "Season Unknown"
            # ghost row. Setting the DTO indexes is the one cure that does not depend
            # on the nfo being re-read (measured 2026-09-20).
            _sp = _parse_span(Path(e["Path"]).name)
            if _sp and (e.get("ParentIndexNumber") is None
                        or e.get("IndexNumber") is None):
                jf_slot_null += 1
        except Exception:                                             # noqa: BLE001
            pass
    janky_fixable = janky_escalate = nfo_stale = jf_stale = blank = unreadable = 0
    contradicts_fixable = 0
    slot_missing = 0
    slot_mismatch = []
    contradicts_review = []
    # WHICH episodes, not just how many. The escalation used to hand the model a COUNT
    # ("6 episode(s) with release-group/blank titles") and a folder path, though this very
    # loop had just computed exactly which six. Instrumented against a working model, the
    # run spent its whole turn budget orienting -- ListDir, Read, ListDir, Read, Grep, Grep
    # -- and concluded "Wait, the task says '6 episode(s)'. Let me look more carefully."
    # At --max-turns 80 the same run returned EMPTY with exit 0, its context full of
    # directory listings. Naming the episodes costs a few hundred characters and removes
    # the entire search.
    janky_items, blank_items = [], []

    def _item(v, nfo_title, rendered, blank_plot):
        sp = _parse_span(v.name)
        return {"season": sp[0] if sp else None,
                "episode": sp[1] if sp else None,
                "file": v.name,
                "nfo_title": nfo_title or "",
                "jellyfin_title": "" if rendered is None else rendered,
                "plot_blank": bool(blank_plot)}

    generic_ok_seasons = _season_generic_is_canonical(videos, jf_name)
    for v in videos:
        nfo = library.episode_nfo_path(v)
        text = nfo.read_text("utf-8", "ignore") if nfo.exists() else ""
        nfo_title = library._xml_tag(text, "title") if text else ""
        rendered = jf_name.get(str(v.resolve()))
        stem = v.stem
        span = _parse_span(v.name)
        # A sidecar that exists but carries no <season>/<episode>: Jellyfin reads
        # IndexNumber null from it, shows the series name for the episode and can
        # invent a "Season Unknown" row (the TZ S02 fault, HANDOFF 10.3). Jellyfin's
        # own saver writes these when the item had null indexes at scrape time; the
        # fleet's writer must put them back.
        if text and span and _nfo_episode_slot(v) is None:
            slot_missing += 1
        elif text and span:
            # A sidecar that names a DIFFERENT slot than its own filename. For a
            # Season-00 special this is the HANDOFF 15.5 shape: the library's own
            # era-ordered shelf is the scheme and the sidecar carries a FOREIGN
            # provider number (Doctor Who's S00E04 *Return Of Doctor Mysterio* with
            # `<episode>149</episode>`). Reported, never auto-rewritten: rewriting the
            # tag to the filename would cement a wrong-slot file like *The End Of Time*
            # at S00E04. The repair computes the true slot first.
            nfo_slot = _slot_disagreement(v, span)
            if nfo_slot:
                slot_mismatch.append((v.name, span[0], span[1],
                                      nfo_slot[0], nfo_slot[1]))
        generic_ok = bool(span) and span[0] in generic_ok_seasons
        nfo_janky = _title_is_janky(nfo_title, stem, generic_ok)
        jf_janky = _title_is_janky(rendered, stem, generic_ok) if rendered is not None else False
        # A sidecar that names a DIFFERENT episode than its own filename. Not janky --
        # a perfectly good title, belonging to another episode (see
        # `_title_contradicts_filename`). Only auto-fixable when the ingest journal
        # confirms the filename; otherwise it is a review item, never a silent rewrite.
        # A LOCKED sidecar is exempt. `lockdata=true` means the pipeline (or a
        # deliberate repair) AUTHORED that title and told Jellyfin never to scrape
        # over it, so a disagreement with the filename is a naming choice, not a
        # fault -- most often a romaji filename against its English translation.
        #   Made in Abyss (2017) - S00E05-Papa to Issho.mkv  vs  "Together with Papa"
        # Same episode. No string comparison can tell a translation from a different
        # episode, and this was the THIRD false-positive class this check produced
        # (after bare episode markers and joined multi-episode titles). Every REAL
        # fault it has ever found was an UN-owned sidecar Jellyfin had scraped, which
        # is exactly where the check still fires.
        _fn_t = _filename_real_title(stem)
        if (_fn_t and nfo_title and not nfo_janky
                and not library.nfo_is_locked(text)
                and _title_contradicts_filename(_fn_t, nfo_title)):
            if _journal_confirms_filename(v):
                contradicts_fixable += 1
            else:
                contradicts_review.append(
                    f"{v.name!r} is filed as {_fn_t!r} but its .nfo names {nfo_title!r}")
        if nfo_janky or jf_janky:
            if _filename_real_title(stem):
                janky_fixable += 1                       # the real title is in the filename
            elif nfo_janky and rendered and not jf_janky:
                nfo_stale += 1                           # Jellyfin good, .nfo junk -> revert time-bomb
            elif jf_janky and nfo_title and not nfo_janky:
                jf_stale += 1                            # .nfo good, Jellyfin junk -> push it down
            else:
                janky_escalate += 1                      # junk on disk AND in Jellyfin -> needs a lookup
                janky_items.append(_item(v, nfo_title, rendered, False))
        state_nfo = library.episode_nfo_state(v)
        if state_nfo == "unreadable":
            # The sidecar is THERE and the mount would not give it to us. Counted on its
            # own, never as "blank": see `library.episode_nfo_state`. Suppressing it
            # entirely would be the opposite mistake -- a mount that cannot be read is a
            # real fault, it is just not a METADATA fault, and the two need different
            # answers from whoever reads the report.
            unreadable += 1
        elif age > 48 * 3600 and state_nfo in ("missing", "blank"):
            blank += 1
            blank_items.append(_item(v, nfo_title, rendered, True))
    if contradicts_fixable:
        add("title_contradicts_filename",
            f"{contradicts_fixable} episode(s) carry a .nfo <title> naming a DIFFERENT "
            f"episode than their filename; the ingest journal confirms the filename",
            auto=True, sev=3)
    if contradicts_review:
        add("title_contradicts_unconfirmed",
            f"{len(contradicts_review)} episode(s) have a .nfo <title> that contradicts "
            f"their filename, and the journal does not record the filing: "
            + "; ".join(contradicts_review[:6]),
            auto=False, sev=3)
    if janky_fixable:
        add("title_janky_fixable",
            f"{janky_fixable} episode(s) titled with release-group junk; real title is in the filename",
            auto=True, sev=2)
    if nfo_stale:
        add("title_nfo_stale",
            f"{nfo_stale} episode(s) render a good title but carry a junk .nfo <title> (a refresh would revert it)",
            auto=True, sev=2)
    if jf_stale:
        add("title_jf_stale",
            f"{jf_stale} episode(s) have a good .nfo title but Jellyfin renders junk",
            auto=True, sev=2)
    if janky_escalate:
        add("title_janky",
            f"{janky_escalate} episode(s) with release-group/blank titles and no real title on disk -- needs a lookup",
            auto=False, sev=2, items=janky_items)
    if unreadable:
        add("nfo_unreadable",
            f"{unreadable} episode sidecar(s) exist but could not be READ this pass -- "
            f"that is the MOUNT, not the metadata. Nothing here needs re-identifying or "
            f"re-filling; re-check after the mount settles",
            auto=False, sev=2)
    if slot_missing:
        add("episode_slot_missing",
            f"{slot_missing} episode sidecar(s) exist but carry no <season>/<episode>, "
            f"so Jellyfin shows a null index and a ghost season; the writer must emit "
            f"the slot the filename states",
            auto=True, sev=3)
    if slot_mismatch:
        detail = "; ".join(
            f"{name!r} sits at S{s:02d}E{e:02d} but its .nfo says S{ns:02d}E{ne:02d}"
            for name, s, e, ns, ne in slot_mismatch[:6])
        add("episode_slot_mismatch",
            f"{len(slot_mismatch)} episode sidecar(s) carry a <season>/<episode> that "
            f"disagrees with the slot their filename states: {detail}. A Season-00 "
            f"special's slot comes from the library's OWN locked scheme, never from the "
            f"sidecar's provider number; compute and apply the repair with "
            f"`scripts/repair_slots.py --specials` (it moves the file AND rewrites the "
            f"sidecar). Do not hand-edit the tag.",
            auto=False, sev=3)
    if jf_slot_null:
        add("episode_index_missing",
            f"{jf_slot_null} episode(s) resolve in Jellyfin with a NULL season/episode "
            f"index although the filename states one -- the app shows the series name "
            f"and a ghost season; the index is set on the item",
            auto=True, sev=2)
    if blank:
        add("plot_blank", f"{blank} episode(s) blank for >48h (no synopsis)", auto=False, sev=1,
            items=blank_items)
        # A show matched to the WRONG provider entry looks exactly like this: most episodes blank,
        # because the entry it is pinned to simply does not have them. Worth calling out separately,
        # because the remedy is completely different from "fill in a synopsis" -- you re-identify the
        # series, and every episode's title and plot corrects itself at once.
        #
        # (Seen 2026-08-04: "The Seven Deadly Sins (2014)" was pinned to TMDB 27240, a DOCUMENTARY
        # about the seven deadly sins. Its 7 episodes supplied plots to the anime's first 7 -- S01E01
        # read "Christianity says lust is a sin but the Greek and Roman empires celebrated..." -- and
        # the remaining 89 had nothing to draw from. Repointing it at TMDB 62104, the anime, took it
        # from 89 blank to 0 and fixed every title in the same pass. Nothing in the report hinted at
        # a wrong match; it just looked like a lot of missing synopses.)
        if disk_count >= 8 and blank >= max(6, int(disk_count * 0.6)):
            add("series_maybe_misidentified",
                f"{blank} of {disk_count} episode(s) have no synopsis -- that ratio usually means the "
                f"series is matched to the WRONG provider entry, not that metadata is merely missing. "
                f"Verify the identity (RemoteSearch/Series) before filling anything in by hand.",
                auto=False, sev=2)

    return probs


# --- mechanical auto-fixes ---------------------------------------------------

def _write_nfo_title(video, title, ep, season=None):
    """Rewrite the episode .nfo <title>, ENSURE <season>/<episode>, lock it.

    The slot keys are not decoration: Jellyfin reads IndexNumber from the sidecar, and
    a sidecar that lacks them renders a null index (the title falls back to the series
    name) and can create a ghost "Season Unknown" row. Jellyfin's own saver writes
    these sidecars without the keys when the item had null indexes at scrape time
    (TZ S02, HANDOFF 10.3), so the fleet's writer inserts them when missing.
    """
    nfo = library.episode_nfo_path(video)
    if not nfo.exists():
        return
    txt = nfo.read_text("utf-8-sig", "ignore")
    if title and "<title>" in txt:
        txt = re.sub(r"<title>.*?</title>", f"<title>{title}</title>", txt, flags=re.S)
    for tag, val in (("season", season), ("episode", ep)):
        if val is None:
            continue
        pat = re.compile(rf"<{tag}>.*?</{tag}>", re.S | re.I)
        repl = f"<{tag}>{int(val)}</{tag}>"
        if pat.search(txt):
            txt = pat.sub(repl, txt, count=1)
        elif "</episodedetails>" in txt:
            txt = txt.replace("</episodedetails>",
                              f"  {repl}\n</episodedetails>", 1)
    if "<lockdata>" in txt:
        txt = re.sub(r"<lockdata>.*?</lockdata>", "<lockdata>true</lockdata>", txt, flags=re.S)
    nfo.write_text(txt, encoding="utf-8")


def _video_res_tier(name: str) -> int:
    low = name.lower()
    if any(k in low for k in ("2160p", "4k", "uhd")):
        return 5
    if "1080p" in low or "1080i" in low:
        return 4
    if "720p" in low:
        return 3
    if any(k in low for k in ("480p", "576p", "dvd")):
        return 2
    return 0


def _dup_quality_key(path: str):
    """Higher is better: resolution tier, then mkv over mp4, then file size."""
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError:
        size = 0
    return (_video_res_tier(p.name), 1 if p.suffix.lower() == ".mkv" else 0, size)


def _delete_video_through_mount(path: str) -> bool:
    """Delete a library video THROUGH the mediafs mount, so mediafs unlinks it everywhere
    and queues the reaper to purge the MEGA copy. Returns True on success."""
    p = Path(path)
    try:
        p.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def apply_auto_fixes(probs, jf, state, dry_run, cycle_budget):
    """Apply the ladder of safe, mechanical fixes for one show. Records what it
    tried in `state[show]` so the ladder advances across cycles and never loops.
    `cycle_budget` caps the per-cycle count of hydrating refresh/rescan actions."""
    show = probs["show"]
    path = probs["path"]
    sid = probs["sid"]
    now = time.time()
    sig = probs["sig"]
    st = state.setdefault(show, {})
    if st.get("sig") != sig:                     # disk changed -> reset the ladder
        # `pids` is NOT part of the ladder -- it is the record of which identity the
        # artwork was fetched under. Clearing it on an episode drop would erase the
        # only evidence a later re-match ever happened.
        #
        # Neither are the art refusals (`art_no_still`, `art_still_tried`): they record
        # what the PROVIDER's catalogue holds for an image, a fact about the episode, not
        # an attempt tied to this show's episode count. Clearing them on one new episode
        # would re-open the adopt-and-reflag loop for every already-refused image. The
        # 30-day TTL is the only thing that expires a refusal, by design.
        keep = {k: st[k] for k in ("pids", "art_no_still", "art_still_tried") if k in st}
        st.clear(); st.update(keep); st["sig"] = sig
    kinds = {p["kind"] for p in probs["problems"]}
    acted = []

    # --- duplicate / 0-byte deletion (the "human decision" that isn't) ---
    # Two files for one SxxExx (same stem, different container), or a 0-byte stub, used
    # to be reported for a human to purge. The decision is mechanical -- keep the
    # higher-quality copy, drop the lower -- and the delete goes through the mount so the
    # reaper purges the MEGA copy too. Only the LOSER is touched; an ambiguous case
    # (different stems, or an unreadable file) is left auto=False and escalates.
    for p in probs["problems"]:
        if p["kind"] == "duplicate_episode" and p.get("auto"):
            files = [f for f in (p.get("files") or []) if f]
            if len(files) >= 2:
                loser = min(files, key=_dup_quality_key)
                acted.append(f"delete duplicate {Path(loser).name} "
                             f"(keep the higher-quality copy)")
                if not dry_run and _delete_video_through_mount(loser):
                    _log(f"  {show}: deleted duplicate {Path(loser).name}")
        elif p["kind"] == "inaccessible" and p.get("auto") and p.get("path"):
            stub = p["path"]
            acted.append(f"delete 0-byte stub {Path(stub).name}")
            if not dry_run and _delete_video_through_mount(stub):
                _log(f"  {show}: deleted 0-byte stub {Path(stub).name}")

    if probs["age"] < MIN_AGE_REFRESH_SEC:
        return acted                              # too fresh; give Jellyfin its turn

    # --- series identity + title art (HANDOFF 10.3) ---
    # Computed, not judged: `_series_identity_problem` only fires when the provider
    # contradicts the nfo's own `premiered`. The repair is then total -- every identity
    # field is rewritten from the verified record, the season years are recomputed and
    # locked, the contaminated art is overwritten with the identity's own, and one
    # recursive metadata+image refresh makes Jellyfin re-read the locked local files.
    if "series_identity_stale" in kinds and sid:
        payload = next(p for p in probs["problems"] if p["kind"] == "series_identity_stale")
        ident = dict(payload.get("ident") or {})
        tmdb_id = str(ident.get("tmdb_id") or "")
        ident["tmdb_id"] = tmdb_id
        art = next((p.get("art") for p in probs["problems"]
                    if p["kind"] == "series_art_stale"), []) or []
        acted.append(
            f"rewrite tvshow.nfo identity from TMDB ({ident.get('name')!r}, "
            f"{ident.get('year')})" + (f" and replace {len(art)} title image(s)"
                                       if art else ""))
        if not dry_run:
            # ORDER IS THE WHOLE REPAIR (measured 2026-09-20 on TZ). Everything
            # Jellyfin-side goes FIRST: the nfo files are written LAST, after
            # Jellyfin's own refresh has settled, because `RemoteSearch/Apply`
            # triggers an asynchronous metadata+image refresh whose NFO saver
            # otherwise overwrites the fresh files with the stale DB values --
            # an earlier attempt at this repair watched it revert tvshow.nfo,
            # season.nfo (year 2013) and the S02 slot tags within seconds.
            #
            # 1) RE-MATCH THE JELLYFIN ITEM. The existing cure for a wrong
            #    identity: sets ProviderIds and, with replaceAllImages, purges
            #    the old show's artwork.
            rematched = False
            ids = {}
            if tmdb_id:
                ids["Tmdb"] = tmdb_id
            if ident.get("tvdb_id"):
                ids["Tvdb"] = str(ident["tvdb_id"])
            if ids:
                try:
                    jf.apply_identification(sid, ident.get("name") or show, ids,
                                            year=ident.get("year"),
                                            replace_images=True)
                    acted.append("re-match the Jellyfin item to the verified identity")
                    rematched = True
                except Exception as exc:                          # noqa: BLE001
                    _log(f"  {show}: identity re-match failed: {exc}")
            # 2) Correct the season ITEMS too. Jellyfin's saver writes season.nfo
            #    from the season item, so rewriting the file alone is undone on the
            #    next save (TZ S1 answered 2013 from the Too Cute match).
            try:
                import tmdbguide
                for s in jf.get(f"Shows/{sid}/Seasons",
                                userId=jf.user_id()).get("Items", []):
                    n = s.get("IndexNumber")
                    if n is None:
                        continue
                    info = tmdbguide.season_info(tmdb_id, n)
                    if not info:
                        continue
                    fields = {"LockData": True}
                    if info.get("year"):
                        fields["ProductionYear"] = int(info["year"])
                    if info.get("air_date"):
                        fields["PremiereDate"] = f"{info['air_date']}T00:00:00.0000000Z"
                    jf.update_item(s["Id"], **fields)
            except Exception as exc:                              # noqa: BLE001
                _log(f"  {show}: season field sync failed: {exc}")
            # 3) Lock the corrected identity on the series item. `LockData` is the
            #    durable half of the sidecar lock (OPERATING §5b). Only these names
            #    are in Jellyfin's MetadataField enum; measured 2026-09-20,
            #    OriginalTitle/ProductionYear/PremiereDate/Taglines answer 400 --
            #    but those three are SETTABLE on the DTO, so they are written too.
            try:
                fields = {
                    "LockData": True,
                    "LockedFields": sorted({
                        "Name", "Overview", "Genres", "Studios", "OfficialRating",
                        "Tags", "Cast"}),
                }
                if ident.get("original_name"):
                    fields["OriginalTitle"] = ident["original_name"]
                if ident.get("year"):
                    fields["ProductionYear"] = int(ident["year"])
                if ident.get("first_air_date"):
                    fields["PremiereDate"] = f"{ident['first_air_date']}T00:00:00.0000000Z"
                if ident.get("last_air_date"):
                    # Settable but not lockable; without it Jellyfin's saver re-stamps
                    # the old `enddate` into the nfo on its next save.
                    fields["EndDate"] = f"{ident['last_air_date']}T00:00:00.0000000Z"
                jf.update_item(sid, **fields)
                acted.append("lock the corrected identity on the Jellyfin item")
                st["identity_fixed_ts"] = now
                st["identity_fixed"] = {"ident": ident, "ts": now}
            except Exception as exc:                              # noqa: BLE001
                _log(f"  {show}: identity lock failed: {exc}")
            # 4) Let the re-match's async refresh finish before writing files.
            if rematched:
                time.sleep(REIDENTIFY_SETTLE_SEC)
            # 5) Rewrite the on-disk records through the tool, LAST.
            try:
                written = _rewrite_series_identity(path, ident)
                seasons = _rewrite_season_identity(path, tmdb_id)
                if written or seasons:
                    _log(f"  {show}: identity rewritten from TMDB {tmdb_id} "
                         f"({seasons} season.nfo)")
            except Exception as exc:                              # noqa: BLE001
                _log(f"  {show}: tvshow.nfo rewrite failed: {exc}")
            # 6) Replace the contaminated title art with the identity's own bytes --
            #    and restore the cover/backdrop/season posters a Jellyfin refresh may
            #    have removed, so the owner-visible shelf is complete after a repair.
            ensure = ["folder.jpg", "landscape.jpg"]
            for sd_season in sorted(Path(path).glob("Season *")):
                m = re.search(r"(\d+)", sd_season.name)
                if m:
                    ensure.append(f"season{int(m.group(1)):02d}-poster.jpg")
            try:
                fixed, failed = _replace_title_art(path, tmdb_id, art,
                                                   ensure=ensure)
                if fixed:
                    acted.append(f"replaced/restored {fixed} title image(s) with the "
                                 f"verified identity's art")
                if failed:
                    # Not all art could be fetched. Keep the job alive for a
                    # later pass instead of dropping it: identity is now
                    # correct on disk, so the trigger would never fire again.
                    st["art_pending"] = {"ident": ident, "art": art}
                    acted.append(f"{failed} title image(s) left as-is (provider "
                                 f"had none this pass; retried next cycle)")
                else:
                    st.pop("art_pending", None)
            except Exception as exc:                              # noqa: BLE001
                st["art_pending"] = {"ident": ident, "art": art}
                _log(f"  {show}: title art replace failed: {exc}")
            folder = Path(path) / "folder.jpg"
            if folder.exists():
                try:
                    jf.push_primary(sid, folder.read_bytes())
                except Exception as exc:                          # noqa: BLE001
                    _log(f"  {show}: poster push failed: {exc}")
        else:
            st["identity_fixed_ts"] = now

    if "series_art_stale" in kinds and "series_identity_stale" not in kinds and sid:
        payload = next(p for p in probs["problems"] if p["kind"] == "series_art_stale")
        ident = dict(payload.get("ident") or {})
        tmdb_id = str(ident.get("tmdb_id") or "")
        art = payload.get("art") or []
        acted.append(f"retry {len(art)} title image(s) still carrying the old "
                     f"identity's art")
        if not dry_run and tmdb_id:
            ensure = ["folder.jpg", "landscape.jpg"]
            for sd_season in sorted(Path(path).glob("Season *")):
                m = re.search(r"(\d+)", sd_season.name)
                if m:
                    ensure.append(f"season{int(m.group(1)):02d}-poster.jpg")
            fixed, failed = _replace_title_art(path, tmdb_id, art, ensure=ensure)
            if fixed:
                acted.append(f"replaced {fixed} title image(s)")
            if failed:
                st["art_pending"] = {"ident": ident, "art": art}
                acted.append(f"{failed} still unavailable (retried next cycle)")
            else:
                st.pop("art_pending", None)
                folder = Path(path) / "folder.jpg"
                if folder.exists():
                    try:
                        jf.push_primary(sid, folder.read_bytes())
                    except Exception as exc:                      # noqa: BLE001
                        _log(f"  {show}: poster push failed: {exc}")

    if ("episode_slot_missing" in kinds or "episode_index_missing" in kinds) and sid:
        victims = []
        for v in _episode_videos(Path(path)):
            nfo = library.episode_nfo_path(v)
            if not nfo.exists():
                continue
            try:
                if _nfo_episode_slot(v) is not None:
                    continue
                span = _parse_span(v.name)
                if not span:
                    continue
                victims.append((v, span[0], span[1]))
            except OSError:
                continue
        if victims or "episode_index_missing" in kinds:
            acted.append(f"add <season>/<episode> to {len(victims)} episode sidecar(s) "
                         f"and the same index on the Jellyfin item(s)")
            if not dry_run:
                fixed = 0
                for v, season, episode in victims:
                    nfo = library.episode_nfo_path(v)
                    title = library._xml_tag(nfo.read_text("utf-8", "ignore"), "title")
                    try:
                        _write_nfo_title(v, title or v.stem, episode, season=season)
                        fixed += 1
                    except OSError as exc:
                        _log(f"  {show}: slot write failed for {v.name}: {exc}")
                # The sidecar alone is not enough: Jellyfin's DB copy is what the app
                # renders, and a scan does not necessarily re-read it. Set the item's
                # own ParentIndexNumber/IndexNumber, which also makes Jellyfin's own
                # saver emit the tags on its next write. LockData on the episode stops
                # a later refresh reverting to the null index.
                item_fixed = 0
                for e in jf.episodes(sid):
                    p = e.get("Path")
                    if not p:
                        continue
                    span = _parse_span(Path(p).name)
                    if not span:
                        continue
                    if e.get("ParentIndexNumber") == span[0] \
                            and e.get("IndexNumber") == span[1]:
                        continue
                    try:
                        jf.update_item(e["Id"], ParentIndexNumber=span[0],
                                       IndexNumber=span[1], LockData=True)
                        item_fixed += 1
                    except Exception as exc:                      # noqa: BLE001
                        _log(f"  {show}: index update failed for {Path(p).name}: {exc}")
                # Deliberately NO metadata refresh here: the item indexes are set
                # directly, and a `meta="Default"` refresh makes Jellyfin's NFO saver
                # rewrite the sidecars (dropping the slot tags we just wrote) --
                # measured 2026-09-20 on TZ. The app reads the DTO, not the file.

    # --- missing episodes ladder ---
    if "series_missing" in kinds:
        if (not st.get("scan_ts") or now - st["scan_ts"] > MIN_AGE_HEAVY_SEC) \
                and cycle_budget["rescan"] > 0:
            acted.append(f"library scan (series '{show}' absent from Jellyfin)")
            if not dry_run:
                jf.library_scan()
                st["scan_ts"] = now
                cycle_budget["rescan"] -= 1
    elif "episodes_missing" in kinds:
        jf_count = next(p["jf_count"] for p in probs["problems"] if p["kind"] == "episodes_missing")
        settled = not st.get("refresh_ts") or now - st["refresh_ts"] > REFRESH_SETTLE_SEC
        # FIRST rung, before any refresh: a series with NO ProviderIds cannot be identified, so
        # Jellyfin resolves zero episodes and neither a refresh nor a library scan can ever help
        # -- both match against an identity the item does not have. The ids are usually sitting
        # right there in the show's own tvshow.nfo, unread. Applying them makes the ordinary
        # refresh below work. (Seen 2026-08-04: Soul Eater NOT! (2014) sat at 0 of 12 through
        # repeated refreshes and scans; its item had ProviderIds {} while its tvshow.nfo carried
        # tmdb 60871 / tvdb 278371. Applying them took it to 12 of 12 immediately.)
        if jf_count == 0 and sid and not st.get("identified_ts"):
            nfo_ids = _nfo_provider_ids(probs.get("path"))
            if nfo_ids and not jf.provider_ids(sid):
                acted.append(f"applied identity from tvshow.nfo ({nfo_ids}) -- series had no "
                             f"provider ids, so nothing could resolve its episodes")
                if not dry_run:
                    try:
                        jf.apply_identification(sid, show, nfo_ids)
                        st["identified_ts"] = now
                    except Exception as exc:                            # noqa: BLE001
                        _log(f"  identify failed for {show!r}: {exc}")
        if st.get("refresh_n", 0) < 2 and settled and cycle_budget["refresh"] > 0:
            acted.append(f"recursive refresh (resolve {probs['disk_count'] - jf_count} missing episode(s))")
            if not dry_run:
                jf.refresh(sid, meta="Default", img="Default")
                st["refresh_ts"] = now
                st["refresh_n"] = st.get("refresh_n", 0) + 1
            cycle_budget["refresh"] -= 1
        elif jf_count == 0 and st.get("refresh_n", 0) >= 1 and settled \
                and probs["age"] > MIN_AGE_HEAVY_SEC and cycle_budget["rescan"] > 0 \
                and (not st.get("rescan_ts") or now - st["rescan_ts"] > MIN_AGE_HEAVY_SEC):
            # Fully broken and a per-item refresh didn't help: the item is orphaned
            # (its children never resolved). A library scan re-walks the folder and
            # ADDS the missing episodes -- it never deletes an existing media file.
            #
            # We do NOT delete the Jellyfin item here. `DELETE /Items/{id}` with
            # Jellyfin's file-management on deletes the underlying FILES through the
            # mount (mediafs then removes them from every drive), which permanently
            # destroys any content not yet uploaded to the pool. A scan is the safe
            # heal; if it still doesn't resolve, we escalate rather than delete.
            acted.append("library scan (orphaned series, refresh didn't resolve)")
            if not dry_run:
                jf.library_scan()
                st["rescan_ts"] = now
            cycle_budget["rescan"] -= 1

    # --- poster ladder ---
    if "poster_missing" in kinds and sid:
        folder = Path(path)
        disk_poster = next((folder / n for n in POSTER_ON_DISK if (folder / n).exists()), None)
        if disk_poster is None:
            disk_poster = next(iter(folder.glob("*-poster.*")), None)
        if disk_poster is not None:
            acted.append(f"push on-disk poster ({disk_poster.name}) into Jellyfin")
            if not dry_run:
                try:
                    jf.push_primary(sid, disk_poster.read_bytes())
                except Exception as e:                                # noqa: BLE001
                    _log(f"  {show}: poster push failed: {e}")
        else:
            # A series with NO provider ids cannot be asked for a poster -- RemoteImages
            # searches by identity, so an unidentified series has nothing to look up and
            # this ladder silently does nothing, cycle after cycle. Apply the ids from its
            # own tvshow.nfo first when Jellyfin's item is missing them (the same first-rung
            # heal the episodes ladder uses); the very same pass then fetches the poster.
            if not jf.provider_ids(sid) and not st.get("identified_ts"):
                nfo_ids = _nfo_provider_ids(path)
                if nfo_ids:
                    acted.append(f"apply identity from tvshow.nfo ({nfo_ids}) -- series had "
                                 f"no provider ids, so a poster could not be looked up")
                    if not dry_run:
                        try:
                            jf.apply_identification(sid, show, nfo_ids)
                            st["identified_ts"] = now
                        except Exception as exc:                      # noqa: BLE001
                            _log(f"  {show}: identify failed: {exc}")
            url = jf.remote_primary_url(sid)
            if url:
                acted.append("adopt remote poster + write folder.jpg")
                if not dry_run:
                    try:
                        jf.adopt_remote_image(sid, "Primary", url)
                        with urllib.request.urlopen(url, timeout=60) as r:
                            (folder / "folder.jpg").write_bytes(r.read())
                    except Exception as e:                            # noqa: BLE001
                        _log(f"  {show}: remote poster adopt failed: {e}")
            else:
                # FINAL rung -- no on-disk poster and no provider poster (no identity to
                # search against, or the provider simply has no Primary). Derive a cover
                # from the show's own artwork so the series is NEVER left with a blank
                # tile: this is the local-art route the episode stills already use,
                # extended to the series level. Guaranteed idempotent -- the next pass
                # sees the Primary we just pushed and stops flagging `poster_missing`.
                acted.append("generate series cover from its own artwork "
                             "(no provider poster available)")
                if not dry_run:
                    if _generate_series_poster(folder):
                        try:
                            jf.push_primary(sid, (folder / "folder.jpg").read_bytes())
                        except Exception as e:                        # noqa: BLE001
                            _log(f"  {show}: generated cover push failed: {e}")
                    else:
                        _log(f"  {show}: no local artwork to derive a cover from; "
                             f"left for a later pass")

    # --- artwork ladder ---------------------------------------------------------
    #
    # Two rungs, in order of bluntness.
    #
    # 1. Identity changed -> force a recursive image refresh with replaceAllImages.
    #    This is the ONLY thing that makes Jellyfin re-fetch an image slot that is
    #    already filled, and it is exactly right here: every existing image is the
    #    other show's. `metadataRefreshMode=None` keeps it images-only, so the good
    #    plots/titles the re-identification just produced are not disturbed and no
    #    ffprobe is provoked.
    #
    # 2. Structurally bogus episode images -> re-adopt each one individually from
    #    the provider. Needed as well as rung 1 because a full-replace refresh
    #    happily re-installs the provider's OWN fallback (a season poster where it
    #    has no still), which is how season 4 ended up with 24 copies of one poster.
    #    Per-episode adoption is aspect-checked, so a poster is refused rather than
    #    written.
    if "artwork_identity_stale" in kinds and sid:
        settled = not st.get("art_refresh_ts") or now - st["art_refresh_ts"] > REFRESH_SETTLE_SEC
        if settled:
            acted.append("recursive image refresh with replaceAllImages "
                         "(series was re-identified; old artwork is the other show's)")
            if not dry_run:
                try:
                    jf.refresh(sid, meta="None", img="FullRefresh",
                               replace_meta=False, replace_img=True, recursive=True)
                    st["art_refresh_ts"] = now
                    # Record the identity ONLY once its artwork has been re-fetched, so a
                    # refresh that fails is retried next cycle instead of being forgotten.
                    st["pids"] = next(p["pids"] for p in probs["problems"]
                                      if p["kind"] == "artwork_identity_stale")
                except Exception as exc:                              # noqa: BLE001
                    _log(f"  {show}: artwork refresh failed: {exc}")

    if "artwork_bogus" in kinds and sid and cycle_budget["art"] > 0:
        bad = dict(next(p["art_bad"] for p in probs["problems"] if p["kind"] == "artwork_bogus"))
        ep_by_path = {}
        for e in jf.episodes(sid):
            if e.get("Path"):
                try:
                    ep_by_path[str(Path(e["Path"]).resolve())] = e["Id"]
                except Exception:                                     # noqa: BLE001
                    pass
        # Which provider URL each image has already had adopted (per image, survives a
        # signature reset): the memory that turns "adopted, shape persisted" into a
        # refusal instead of an every-pass re-adopt of the same bytes.
        tried_map = st.setdefault("art_still_tried", {})
        would = adopted = refused_absent = refused_tried = 0
        for bad_path in sorted(bad):
            if cycle_budget["art"] <= 0:
                break
            # The image sidecar sits next to its video; map back to the video to find the item.
            stem = Path(bad_path).name[:-len("-thumb.jpg")]
            eid = next((i for p, i in ep_by_path.items() if Path(p).stem == stem), None)
            if eid is None:
                continue
            if dry_run:
                would += 1
                cycle_budget["art"] -= 1
                continue
            # A failed LOOKUP must not become a remembered refusal. `best_remote_still`
            # raises on transport errors and returns None only when the provider
            # genuinely offers no landscape still, so a TMDB blip cannot hide an image
            # from the report for `ART_NO_STILL_TTL_SEC`.
            try:
                url = jf.best_remote_still(eid)
            except Exception as exc:                                  # noqa: BLE001
                _log(f"  {show}: still lookup failed for {stem} (not remembered): {exc}")
                continue
            action = _art_next_action(url, tried_map.get(bad_path))
            if action != "adopt":
                # Leave the image AND remember the provider's answer, so the shape check
                # stops re-raising a problem whose only fix has been asked for and
                # refused. `no-still`: the provider offers nothing. `already-tried`: the
                # offered still is already what is on disk (the provider re-uses one
                # image across these episodes), so fetching it again cannot change the
                # bytes -- this is the loop the `already-tried` state exists to break.
                if action == "no-still":
                    refused_absent += 1
                else:
                    refused_tried += 1
                st.setdefault("art_no_still", {})[bad_path] = {
                    "ts": now, "url": url or "", "reason": action}
                continue
            cycle_budget["art"] -= 1
            try:
                jf.adopt_remote_image(eid, "Primary", url)
                tried_map[bad_path] = url
                # A NEW provider answer starts a fresh attempt: drop any stale refusal
                # so the next shape verdict is a real one, not a remembered one.
                st.get("art_no_still", {}).pop(bad_path, None)
                adopted += 1
            except Exception as exc:                                  # noqa: BLE001
                _log(f"  {show}: still adopt failed for {stem}: {exc}")
        if would:
            acted.append(f"re-examine {would} bogus episode image(s)")
        if adopted:
            acted.append(f"re-adopt {adopted} bogus episode image(s) from the provider")
        if refused_absent:
            acted.append(f"{refused_absent} episode image(s) left as-is "
                         f"(provider has no real still)")
        if refused_tried:
            acted.append(f"{refused_tried} episode image(s) left as-is (the provider's "
                         f"own still is already applied and still shared; re-checked "
                         f"after {ART_NO_STILL_TTL_SEC // 86400} days)")

    # --- deterministic title fixes (no AI run): three cases, each idempotent ---
    #   1. the real title is in the FILENAME            -> write it to both sides
    #   2. Jellyfin renders a good title, .nfo is junk  -> copy it DOWN into the
    #      locked .nfo so a refresh can never revert it (the exact revert trap)
    #   3. .nfo has a good title, Jellyfin renders junk -> push the .nfo title UP
    # Everything left (junk on BOTH sides, no title anywhere) is a "title_janky"
    # escalation -- it needs a real per-episode lookup, which the AI run does.
    if kinds & {"title_janky_fixable", "title_nfo_stale", "title_jf_stale",
               "title_contradicts_filename"} and sid:
        from_filename = revert_guarded = pushed_up = 0
        eps_for_titles = jf.episodes(sid)
        # Same season-uniformity exemption the detector uses, so the FIXER cannot "correct" a
        # title that is already canonical (an officially untitled season rendering "Episode N").
        _jfname = {}
        for _e in eps_for_titles:
            if _e.get("Path"):
                try:
                    _jfname[str(Path(_e["Path"]).resolve())] = _e.get("Name") or ""
                except Exception:                                     # noqa: BLE001
                    pass
        generic_ok_seasons = _season_generic_is_canonical(
            [Path(x["Path"]) for x in eps_for_titles if x.get("Path")], _jfname)
        for e in eps_for_titles:
            p = e.get("Path", "")
            if not p:
                continue
            vpath = Path(p)
            stem = vpath.stem
            rendered = e.get("Name", "")
            nfo = library.episode_nfo_path(vpath)
            nfo_text = nfo.read_text("utf-8", "ignore") if nfo.exists() else ""
            nfo_title = library._xml_tag(nfo_text, "title") if nfo_text else ""
            _span0 = _parse_span(vpath.name)
            generic_ok = bool(_span0) and _span0[0] in generic_ok_seasons
            rendered_janky = _title_is_janky(rendered, stem, generic_ok)
            nfo_janky = _title_is_janky(nfo_title, stem, generic_ok)
            fn_title = _filename_real_title(stem)
            span = _parse_span(vpath.name)
            ep = span[1] if span else e.get("IndexNumber")
            good = None
            # Same exemption as the detector: a LOCKED sidecar was authored on
            # purpose, so its title is never "contradicted" into a rewrite here.
            contradicts = (fn_title and nfo_title and not nfo_janky
                           and not library.nfo_is_locked(nfo_text)
                           and _title_contradicts_filename(fn_title, nfo_title)
                           and _journal_confirms_filename(vpath))
            if fn_title and (rendered_janky or nfo_janky or contradicts):
                good = fn_title; from_filename += 1
            elif nfo_janky and rendered and not rendered_janky:
                good = rendered; revert_guarded += 1        # copy Jellyfin's good title into the .nfo
            elif rendered_janky and nfo_title and not nfo_janky:
                good = nfo_title; pushed_up += 1            # push the .nfo's good title to Jellyfin
            if good is None:
                continue
            if not dry_run:
                _write_nfo_title(vpath, good, ep)
                try:
                    lf = sorted(set((e.get("LockedFields") or []) + ["Name"]))
                    jf.update_item(e["Id"], Name=good, LockedFields=lf)
                except Exception as ex:                               # noqa: BLE001
                    _log(f"  {show}: title update failed for {stem}: {ex}")
        if from_filename:
            acted.append(f"rewrite {from_filename} janky title(s) from the filename")
        if revert_guarded:
            acted.append(f"lock {revert_guarded} good title(s) into the .nfo (revert-guard)")
        if pushed_up:
            acted.append(f"push {pushed_up} good .nfo title(s) into Jellyfin")

    if acted and st.get("healthy_ts"):
        st.pop("healthy_ts", None)
    return acted


# --- escalation to a headless AI fixer ---------------------------------------

def _ai_env():
    """The headless-run environment (PATH for the agent's own Bash); see config.ai_env."""
    return config.ai_env()


def budget_available():
    """Tiny ping. A clean exit means the API will answer; exit 2 (or spend wording in
    the reply) means it will not, and the cycle skips its escalation rather than
    spending a full run to discover the same thing."""
    cmd = [*config.AI_BIN, "-p", "--max-turns", "1", "--tools", "",
           "--output-format", "text"]
    if config.AI_MODEL:
        cmd += ["--model", config.AI_MODEL]
    try:
        proc = subprocess.run(cmd, input="Reply with exactly: OK", capture_output=True,
                              text=True, timeout=90, env=_ai_env(),
                              cwd=str(config.PROJECT_ROOT))
    except subprocess.TimeoutExpired:
        return False
    blob = (proc.stdout + proc.stderr).lower()
    if config.identify_unavailable(blob):
        return False
    return proc.returncode == 0


ESCALATION_PROMPT = """\
You are the healer for a self-hosted anime/TV library served by Jellyfin. One show
has problems the mechanical daemon could not fix on its own; fix them properly.

SHOW FOLDER (browse it on disk -- this is the mediafs mount, full library view):
  {path}

PROBLEMS DETECTED:
{problems}

THE EXACT EPISODES INVOLVED (do not go looking for them -- this list IS the work):
{episodes}

GROUND RULES (the whole fleet's ethos -- durable file is truth, Jellyfin is a
projection):
- The sidecar `.nfo` next to each video is the source of truth Jellyfin reads.
  When you write a correct title/synopsis, write it into that `.nfo` and set
  `<lockdata>true</lockdata>` so the scraper never clobbers it. Keep the exact
  Jellyfin/Kodi `.nfo` schema already in the neighbouring files -- copy their
  shape.
- The real episode TITLE, when present, is in the filename after the `SxxExx-`
  segment. Trust it over any web guess for numbering.
- For a blank/missing SYNOPSIS, research the specific episode (its `.nfo` title,
  the filename, the show + episode number) on the Fandom wiki / episode guides and
  write a real 1-3 sentence `<plot>`. Never invent; if you genuinely cannot find
  it, leave it and say so.
- For a MISSING IDENTITY (a series whose Jellyfin item and tvshow.nfo carry no
  TMDB/TVDB id -- the doctor flags it `identity_missing`): find the right TMDB/TVDB
  entry and pin it. Search by the on-disk title AND the episode contents; for an
  anime known under a Japanese and an English title, search BOTH (e.g. "Youjo Senki"
  is "Saga of Tanya the Evil"). Write the correct `<tvdbid>`/`<tmdbid>` (and
  `<imdbid>`, `<title>` if wrong) into the show's `tvshow.nfo`, apply the identity to
  the Jellyfin item, then force an images-only replace refresh so the poster fills
  in. Do NOT delete or move media yourself -- if the id you pin matches a series
  already in the library, the harness auto-merges the redundant folder. If no
  TMDB/TVDB entry exists at all (a niche release), say so explicitly rather than
  guessing an id.
- For a WRONG IDENTITY (episode/series scraped as the wrong title/franchise),
  correct the `.nfo` ids/title to the right TMDB/TVDB entry -- and then REPLACE THE
  ARTWORK, which is the step that gets forgotten. Every poster, backdrop, season
  poster and episode still on disk was fetched under the WRONG identity and is the
  other show's; Jellyfin will never replace them on its own, because an ordinary
  refresh only fills image slots that are EMPTY. After fixing the identity, either
  apply it with `?replaceAllImages=true` or force an images-only replace:
    POST /Items/<id>/Refresh?Recursive=true&metadataRefreshMode=None&imageRefreshMode=FullRefresh&replaceAllImages=true
  Then VERIFY by eye -- open the resulting `folder.jpg` and a couple of episode
  `-thumb.jpg` files and confirm they depict this show. A same-resolution,
  well-formed JPEG of the wrong series passes every mechanical check there is.
  (Seen 2026-08-04: "The Seven Deadly Sins (2014)" was re-identified off a History
  Channel documentary; its plots and titles all corrected, and it kept the
  documentary's DVD cover and woodcut episode stills for five more days.)
- For a RELEASE-GROUP / JUNK episode TITLE (e.g. `[Judas] x265 10b`, `x265 10bit`,
  a bare `Episode N`, or the raw filename): find each episode's REAL title. Do NOT
  trust the raw episode NUMBER to index a guide -- these files can be mis-numbered
  vs a guide's absolute order. MATCH BY THE EPISODE'S `<plot>` (already correct in
  the `.nfo`) and by the `<tvdbid>`/`<imdbid>` in that same `.nfo`; look the title
  up (Fandom/Wikipedia episode list, TheTVDB) and confirm it describes the same
  events as the plot before writing it. Write the correct `<title>` into the `.nfo`
  with `<lockdata>true</lockdata>` AND set the Jellyfin item's `Name` (add "Name" to
  its `LockedFields` via the POST below) -- BOTH, and locked, or a future refresh
  will revert it to the junk. A blank plot is not required to fix a junk title.
- NEVER DELETE A MEDIA FILE. Deleting through the mount removes it from every
  drive and can permanently destroy content not yet uploaded to the MEGA pool.
  For a DUPLICATE episode (two files, same SxxExx) or a 0-byte / unreadable file,
  do NOT remove anything -- describe exactly which files are involved in your
  summary so a human can decide via the proper purge runbook. You may fix
  sidecars and Jellyfin metadata freely; you may not touch video/comic files.
  This is ENFORCED, not requested: you have no shell, and Write/Edit refuse any
  media path inside the library. Report the paths and move on.

After editing sidecars, tell Jellyfin to pick them up. Use the `Jellyfin` tool --
it authenticates for you, so never put a token or api_key in the path:
  Per-item refresh (re-read locked .nfo):
    POST /Items/<id>/Refresh?Recursive=true&metadataRefreshMode=Default&imageRefreshMode=Default
  Direct title fix (when a refresh won't overwrite a populated-but-wrong field):
    GET /Items/<id>  (full DTO) -> set Name/IndexNumber/LockedFields -> POST /Items/<id>
  Find a series' id:
    GET /Items?Recursive=true&IncludeItemTypes=Series&Fields=Path  (match Path)
  Find its episodes (with Path + ids):
    GET /Shows/<seriesId>/Episodes?userId=<uid>&Fields=Path
  First user id:  GET /Users  -> [0].Id

Work only inside this one show. Be surgical and conservative. When done, print a
short summary of exactly what you changed.
"""


# How many episodes the prompt names outright. A show with hundreds of blank plots is a
# different problem (a wrong series identity -- see `series_maybe_misidentified`), and
# dumping all of them back into the prompt would recreate the context flood this list
# exists to prevent. The tail is summarised, never silently dropped.
MAX_ESCALATION_EPISODES = 40


def _episode_block(escalate_probs):
    """Render the exact episodes behind the escalated problems, for the prompt.

    Returns a placeholder line when a problem carries no per-episode detail, so the model
    is told plainly that it must look rather than left to guess whether a blank section
    means "none" or "not computed"."""
    rows, seen = [], set()
    for p in escalate_probs:
        for it in p.get("items") or []:
            key = it["file"]
            if key in seen:
                continue
            seen.add(key)
            sxe = ("S%02dE%02d" % (it["season"], it["episode"])
                   if it["season"] is not None and it["episode"] is not None else "S??E??")
            bits = [f"  - {sxe}  file: {it['file']}"]
            bits.append(f"      .nfo <title>: {it['nfo_title'] or '(empty)'}")
            if it["jellyfin_title"] != it["nfo_title"]:
                bits.append(f"      Jellyfin renders: {it['jellyfin_title'] or '(empty)'}")
            bits.append(f"      <plot>: {'BLANK -- needs one' if it['plot_blank'] else 'present'}")
            rows.append("\n".join(bits))
    if not rows:
        return ("  (this problem class carries no per-episode list; inspect the folder to "
                "find the affected files)")
    if len(rows) > MAX_ESCALATION_EPISODES:
        extra = len(rows) - MAX_ESCALATION_EPISODES
        rows = rows[:MAX_ESCALATION_EPISODES]
        rows.append(f"  ... and {extra} more of the same kind in this folder. Fix these "
                    f"{MAX_ESCALATION_EPISODES} first; the next cycle re-detects the rest.")
    return "\n".join(rows)


def _metadata_repair_state(show_path, only_names=None):
    """`(blank_plots, junk_titles)` on disk -- the postcondition an escalation must move.

    HANDOFF 10.4 reason 3: `escalate()` counted any non-empty closing sentence as
    success, so two timed-out/empty Toriko runs burned `escalate_n` and
    `MAX_ESCALATIONS_PER_SIG` retired the show permanently. The budget is charged only
    when the artifact actually changed.

    `only_names` restricts the count to the exact episode files the escalation was
    asked to fix. The whole-show count was measured against a LIVE ingest: a pack
    filing new blank episodes mid-run turned a real repair into "no change" and a
    no-op into a decrease (measured: BoJack `(0, 0) -> (2, 2)` while the run did
    nothing). The postcondition is a statement about the work requested, so it is
    measured on the work requested.
    """
    blank = junk = 0
    try:
        videos = _episode_videos(Path(show_path))
    except Exception:                                            # noqa: BLE001
        return (0, 0)
    for v in videos:
        if only_names is not None and v.name not in only_names:
            continue
        nfo = library.episode_nfo_path(v)
        try:
            text = nfo.read_text("utf-8", "ignore") if nfo.exists() else ""
        except OSError:
            continue
        if not library._xml_tag(text, "plot"):
            blank += 1
        if _title_is_janky(library._xml_tag(text, "title") or "", v.stem):
            junk += 1
    return (blank, junk)


def escalate(probs, dry_run):
    """Spawn one headless AI run to fix a show's judgment-level problems.

    Returns True only when a run actually happened, said something, AND the on-disk
    artifact changed. An empty run is NOT success: `escalate_n` is spent on the return
    value, and a show that burns MAX_ESCALATIONS_PER_SIG on silent runs is never retried
    again. Every show repaired by hand on 2026-09-03 was sitting at `escalate_n: 2` for
    exactly that reason. The postcondition check is the second half of that lesson
    (HANDOFF 10.4): a run that talks but writes nothing must not retire the show either.
    """
    escalate_probs = [p for p in probs["problems"] if not p["auto"]]
    if not escalate_probs:
        return False
    lines = "\n".join(f"  - [{p['kind']}] {p['detail']}" for p in escalate_probs)
    # The Jellyfin credential is deliberately NOT interpolated here any more. The
    # `Jellyfin` tool authenticates each call itself, so the key never enters the prompt
    # -- which means it never reaches the provider, and a run cannot quote it back into
    # its own summary and out into a log.
    prompt = ESCALATION_PROMPT.format(
        path=probs["path"], problems=lines, mount=config.MEDIAFS_MOUNT,
        episodes=_episode_block(escalate_probs))
    if dry_run:
        _log(f"  DRY-RUN: would escalate '{probs['show']}' to the AI "
             f"({len(escalate_probs)} problem(s))")
        return True
    cmd = [*config.AI_BIN, "-p", "--output-format", "json",
           "--tools", "Read,Glob,Grep,Edit,Write,ListDir,Probe,Jellyfin,WebSearch,WebFetch",
           "--max-turns", "80", "--timeout", "1740"]
    if config.AI_MODEL:
        cmd += ["--model", config.AI_MODEL]
    # The postcondition is measured on the exact episodes the run was handed, never on
    # the whole show: a live ingest keeps filing while the run works, and those new
    # sidecars must not move the verdict either way (see `_metadata_repair_state`).
    targets = {it["file"] for p in escalate_probs for it in (p.get("items") or [])
               if it.get("file")}
    before = _metadata_repair_state(probs.get("path"), targets or None)
    _log(f"  escalating '{probs['show']}' to a headless AI run "
         f"({len(escalate_probs)} problem(s); before blank/junk={before})...")
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              timeout=1800, env=_ai_env(), cwd=str(config.PROJECT_ROOT))
    except subprocess.TimeoutExpired:
        _log(f"  {probs['show']}: escalation timed out; retry next cycle")
        return False

    # Read the envelope ai_runner writes. `is_error` true, or empty closing text, means
    # the run did not do the work -- charging it against MAX_ESCALATIONS_PER_SIG would
    # retire the show permanently over a provider outage or a context flood.
    result, is_error = "", False
    try:
        env = json.loads((proc.stdout or "").strip().splitlines()[-1])
        result = (env.get("result") or "").strip()
        is_error = bool(env.get("is_error"))
    except (ValueError, IndexError, AttributeError):
        result = (proc.stdout or "").strip()
    if proc.returncode != 0 or is_error or not result:
        why = (result[:200] if result else
               (proc.stderr or "").strip()[-200:] or "no output")
        _log(f"  {probs['show']}: escalation produced nothing "
             f"(exit {proc.returncode}); NOT counted against its retry budget: {why}")
        return False
    # VERIFY THE POSTCONDITION BEFORE CHARGING THE BUDGET (HANDOFF 10.4). A run that
    # returns a confident paragraph but leaves the .nfo files unchanged has not
    # repaired anything, and counting it is how Toriko reached escalate_n=2 and was
    # retired for good. Only a real reduction in blank plots / junk titles among the
    # episodes the run was given counts.
    after = _metadata_repair_state(probs.get("path"), targets or None)
    if after >= before:
        _log(f"  {probs['show']}: escalation finished but the sidecars did not change "
             f"(blank/junk {before} -> {after}); NOT counted against its retry budget "
             f"-- it will be retried when providers recover")
        return False
    _log(f"  {probs['show']}: escalation repaired metadata on disk "
         f"(blank/junk {before} -> {after}) -- {result[:200]}")
    return True


# --- report ------------------------------------------------------------------

def write_report(all_probs, acted_map, scoped=False, state=None):
    """Write the phone-glanceable health report.

    NEVER called under `--dry-run`: the report is the one file the human actually
    reads, and a dry run must not touch it. It also must not be written by a
    `--show`-scoped run, which sees ONE title and would otherwise replace a
    whole-library report with a single-show view that reads as "everything else is
    fine". `scoped` exists so that stays a deliberate refusal rather than a
    fresh mistake.
    """
    if scoped:
        return
    lines = ["LIBRARY HEALTH  " + time.strftime("%Y-%m-%d %H:%M:%S"), ""]
    unhealthy = [p for p in all_probs if p["problems"]]
    if not unhealthy:
        lines.append("All shows healthy. Nothing to fix.")
    else:
        lines.append(f"{len(unhealthy)} show(s) with issues:")
        lines.append("")
        for p in sorted(unhealthy, key=lambda x: -max(q["sev"] for q in x["problems"])):
            lines.append(f"• {p['show']}")
            # Count this pass's problems as one unit: increments every line seen now and
            # forgets the ones this pass did not report. These counts are state and are
            # persisted by the caller AFTER this function runs (see `run_once`) -- when
            # the save came first, the counters never survived and a genuinely stuck
            # `[auto]` line stayed `[auto]` forever.
            counts = (_note_problem_pass(state, p["show"], p["problems"])
                      if state is not None else {})
            for q in p["problems"]:
                # A problem marked `[auto]` claims the doctor is handling it. When the
                # same one comes back pass after pass, that claim is false and the report
                # is actively misleading -- `Ghost in the Shell - Arise - Another Mission`
                # sat as two `[auto]` lines for WEEKS while the repair searched against a
                # TMDB id that returns 404, so it could never have worked. Nothing said so.
                #
                # So the report now says how long it has been stuck, and stops calling a
                # problem automatic once the automatic path has demonstrably failed at it.
                seen = counts.get(_stuck_key(q), 0)
                stuck = seen >= STUCK_AFTER_PASSES
                mark = "NEEDS REVIEW" if (not q["auto"] or stuck) else "auto"
                lines.append(f"    [{mark}] {q['detail']}")
                if stuck:
                    lines.append(f"             ^ unchanged for {seen} passes -- the "
                                 f"automatic repair cannot fix this one; it needs a human "
                                 f"or an AI run")
            if acted_map.get(p["show"]):
                for a in acted_map[p["show"]]:
                    lines.append(f"    -> fixed: {a}")
            lines.append("")
    try:
        REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        REPORT_FILE.write_text("\n".join(lines) + "\n")
    except OSError:
        pass


# --- movies (loose films under Movies/) --------------------------------------
#
# The doctor's show pass never looked at Movies/, so a film with a seed .nfo whose
# plot/poster Jellyfin never scraped (Jellyfin's TMDB *detail* fetch during a
# refresh is unreliable here even when the id is right) slipped through -- the
# "Blue Exorcist - The Movie had no metadata" case. This pass covers them, using
# the reliable route: a RemoteSearch *candidate* carries the TMDB `Overview`
# inline, so we write that plot straight into the locked .nfo + the Jellyfin item,
# and pull the poster via RemoteImages -- neither depends on the flaky refresh.

_MOVIE_TMDB_RE = re.compile(r'<tmdbid>(\d+)</tmdbid>|<uniqueid[^>]*type="tmdb"[^>]*>(\d+)<', re.I)
_MOVIE_STEM_RE = re.compile(r"^(.*?)\s*\((\d{4})\)\s*$")


def _movie_videos(movies_root):
    """Loose films live directly under Movies/ (a video + sidecars), so scan the top
    level only -- a recursive rglob over the mount stats thousands of sidecars and is
    far too slow to run every cycle. One level of per-film subfolders is also checked.

    Parts 2+ of a stacked film are skipped for the same reason the episode walk skips
    them: Jellyfin stacks the parts into ONE movie item whose Path is part 1, so a
    continuation has no item of its own by design. Counting one would report
    `movie_missing` against a film that is perfectly healthy, forever."""
    out = []
    if not movies_root.is_dir():
        return out
    try:
        entries = list(movies_root.iterdir())
    except OSError:
        return out
    for p in entries:
        try:
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS and not p.name.startswith("._") \
                    and not _is_multipart_continuation(p.stem):
                out.append(p)
            elif p.is_dir() and not p.name.startswith("."):
                for c in p.iterdir():
                    if c.is_file() and c.suffix.lower() in VIDEO_EXTS \
                            and not c.name.startswith("._") \
                            and not _is_multipart_continuation(c.stem):
                        out.append(c)
        except OSError:
            continue
    return out


def _movie_nfo_tmdb(video):
    nfo = video.with_suffix(".nfo")
    if not nfo.exists():
        return None
    m = _MOVIE_TMDB_RE.search(nfo.read_text("utf-8", "ignore"))
    return (m.group(1) or m.group(2)) if m else None


def _write_nfo_plot(video, plot):
    import html
    nfo = video.with_suffix(".nfo")
    if not nfo.exists():
        return
    px = html.escape(plot)
    txt = nfo.read_text("utf-8-sig", "ignore")
    if re.search(r"<plot\s*/>", txt):
        txt = re.sub(r"<plot\s*/>", f"<plot>{px}</plot>", txt, 1)
    elif "<plot>" in txt:
        txt = re.sub(r"<plot>.*?</plot>", f"<plot>{px}</plot>", txt, 1, flags=re.S)
    else:
        txt = txt.replace("<title>", f"<plot>{px}</plot>\n  <title>", 1)
    nfo.write_text(txt, encoding="utf-8")


def diagnose_and_fix_movies(jf, dry_run, only, all_probs, acted_map, worklist):
    """Reconcile loose films under Movies/ against Jellyfin: fill a missing plot
    (from the TMDB RemoteSearch candidate's inline Overview -> locked .nfo + item)
    and a missing poster (RemoteImages). Escalates a film with no Jellyfin item or
    no TMDB match."""
    try:
        idx = jf.movie_index()
    except Exception as e:                                            # noqa: BLE001
        _log(f"movie index query failed ({e}); skipping movies this pass")
        return
    for v in _movie_videos(config.MEDIAFS_MOUNT / "Movies"):
        if only and only not in v.name:
            continue
        stem = v.stem
        # A film is one file, so its ladder resets when that file is replaced or
        # re-encoded -- not on a count, which is always 1 and would never move.
        probs = {"show": stem, "path": str(v), "sig": _file_sig(v), "problems": []}

        def add(kind, detail, auto, sev=1):
            probs["problems"].append({"kind": kind, "detail": detail, "auto": auto, "sev": sev})

        entry = idx.get(str(v.resolve()))
        if entry is None:
            add("movie_missing", "film on disk has no Jellyfin item (needs a library scan)",
                auto=False, sev=2)
            all_probs.append(probs); worklist.append(probs)
            continue
        mid, has_plot, has_poster, _name = entry
        # A DEAD pinned id is why a film can sit here for weeks marked `[auto]` -- the
        # doctor believing it is fixing something it cannot possibly fix. Checked FIRST,
        # because it changes what every line below means: with a dead id there is no
        # candidate to fetch a poster or a plot from, and the answer is to re-identify the
        # film, not to retry the fetch. See `tmdbguide.movie_exists`.
        pinned = _movie_nfo_tmdb(v)
        dead_id = False
        if pinned and (not has_plot or not has_poster):
            try:
                import tmdbguide
                dead_id = tmdbguide.movie_exists(pinned) is False
            except Exception:                                         # noqa: BLE001
                dead_id = False
        if dead_id:
            add("movie_tmdb_id_dead",
                f"pinned TMDB id {pinned} does not exist (404) -- every automatic repair "
                f"has been searching against a dead id and finding nothing. This needs "
                f"RE-IDENTIFYING, not re-fetching",
                auto=False, sev=3)
        if not has_poster:
            add("poster_missing", "movie has no Primary image", auto=True, sev=2)
        if not has_plot:
            add("plot_blank", "movie has no plot/overview", auto=True, sev=2)
        if not probs["problems"]:
            continue

        all_probs.append(probs)
        _log(f"FLAGGED movie {stem}: " + "; ".join(p["detail"] for p in probs["problems"]))
        acted = []
        if not dry_run:
            mm = _MOVIE_STEM_RE.match(stem)
            # With a dead pinned id, passing it to the search is what has been failing all
            # along -- so drop it and search by TITLE instead. That is the one thing that
            # can still work, and nothing was doing it.
            cand = jf.remote_search_movie(mid, tmdb=(None if dead_id else pinned),
                                          name=(mm.group(1) if mm else stem),
                                          year=(mm.group(2) if mm else None))
            if dead_id and not (cand or {}).get("Overview"):
                try:
                    import tmdbguide
                    alt = tmdbguide.find_movie(mm.group(1) if mm else stem,
                                               mm.group(2) if mm else None)
                except Exception:                                     # noqa: BLE001
                    alt = None
                if alt:
                    cand = {"Overview": alt["overview"], "ImageUrl": None}
                    _log(f"  {stem}: pinned id {pinned} is dead; TMDB title search offers "
                         f"{alt['id']} {alt['title']!r} ({alt['year']}) -- filling the plot "
                         f"from it, but the .nfo id still needs correcting by hand or by "
                         f"an AI run")
            if not has_poster:
                url = jf.remote_primary_url(mid) or (cand or {}).get("ImageUrl")
                if url:
                    try:
                        jf.adopt_remote_image(mid, "Primary", url)
                        acted.append("adopt movie poster")
                    except Exception as e:                            # noqa: BLE001
                        _log(f"  {stem}: poster adopt failed: {e}")
            if not has_plot:
                plot = ((cand or {}).get("Overview") or "").strip()
                if plot:
                    _write_nfo_plot(v, plot)
                    try:
                        jf.update_item(mid, Overview=plot)
                        acted.append("fill movie plot from TMDB")
                    except Exception as e:                            # noqa: BLE001
                        _log(f"  {stem}: plot update failed: {e}")
        if acted:
            acted_map[stem] = acted
            for a in acted:
                _log(f"  -> movie {stem}: {a}")
        # Anything not resolved (no TMDB match / dry-run) stays flagged for review.
        if any(not p["auto"] for p in probs["problems"]) or (not acted and not dry_run):
            worklist.append(probs)


def diagnose_and_fix_collections(jf, dry_run, all_probs, acted_map):
    """Reconcile Jellyfin's movie COLLECTIONS (BoxSets) against their images.

    A collection is its own Jellyfin item with its own Primary slot, separate from the
    films inside it -- so a movie can have a poster while its collection cover is blank.
    The show and loose-movie passes never looked at BoxSets, which is how "some movie
    collection displays have no image" slipped through. The heal is always mechanical
    (no AI): pull the TMDB collection poster via RemoteImages, and when the provider has
    none (the two Billy & Mandy / KND collections), adopt the first member film's poster
    bytes instead.
    """
    try:
        boxes = jf.boxsets()
    except Exception as e:                                            # noqa: BLE001
        _log(f"boxset index query failed ({e}); skipping collections this pass")
        return
    for bid, (name, has_primary, pids) in boxes.items():
        if has_primary:
            continue
        probs = {"show": name, "path": f"BoxSet:{bid}",
                 "sig": (name, sorted((pids or {}).items())), "problems": []}
        probs["problems"].append(
            {"kind": "poster_missing", "detail": "collection has no Primary image",
             "auto": True, "sev": 2})
        all_probs.append(probs)
        _log(f"FLAGGED collection {name}: no Primary image")
        acted = []
        if dry_run:
            continue
        url = jf.remote_primary_url(bid)
        if url:
            try:
                jf.adopt_remote_image(bid, "Primary", url)
                acted.append("adopt collection poster (TMDB)")
            except Exception as e:                                    # noqa: BLE001
                _log(f"  collection {name}: poster adopt failed: {e}")
        if not acted:
            # No provider poster: reuse the first member film's poster as the collection
            # cover, so the collection never shows a blank tile.
            try:
                for child in jf.item_children(bid, "Movie"):
                    img = child.get("ImageTags") or {}
                    if not img.get("Primary"):
                        continue
                    data = jf.primary_bytes(child["Id"])
                    if data:
                        jf.push_primary(bid, data)
                        acted.append("adopt member movie poster")
                        break
            except Exception as e:                                    # noqa: BLE001
                _log(f"  collection {name}: member-poster fallback failed: {e}")
        if acted:
             acted_map[name] = acted
             for a in acted:
                 _log(f"  -> collection {name}: {a}")


_EP_SPAN = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,4})(?!\d)")


def diagnose_and_fix_duplicate_series(jf, dry_run, all_probs, acted_map):
    """Merge duplicate SERIES: two Jellyfin series items that share a Tmdb/Tvdb id.

    A re-identified show (e.g. Youjo Shenki -> Saga of Tanya the Evil) can leave behind a
    second series folder for the same show. When EVERY episode of the smaller one is
    already present in the larger one (same SxxExx), the smaller is a redundant duplicate:
    delete its videos through the mount (the reaper purges the MEGA copies), drop the
    empty folder and the Jellyfin item. An episode NOT present in the canonical is left
    alone -- moving it is a separate, riskier relocation this pass deliberately does not
    do, so the duplicate is only retired when nothing would be lost.
    """
    try:
        pids = jf.series_pids()          # {series id -> {Tmdb, Tvdb, ...}}
        series_index = jf.series_index()  # {folder path -> id}
    except Exception as exc:                                            # noqa: BLE001
        _log(f"duplicate-series pass skipped ({exc})")
        return
    id_to_path = {v: k for k, v in series_index.items()}

    by_key: dict = {}
    for sid, p in pids.items():
        tmdb = p.get("Tmdb")
        tvdb = p.get("Tvdb")
        if tmdb and tvdb:
            # Require BOTH ids to match, not one. Jellyfin's ProviderIds can carry a
            # mis-scraped value (Shield Hero once held Goblin Slayer's Tmdb and Dr. STONE's
            # Tvdb), and a single shared id would falsely merge two different shows. Two
            # different ids both wrong in the same way is effectively never the case.
            by_key.setdefault((tmdb, tvdb), []).append(sid)

    for key, sids in by_key.items():
        if len(sids) < 2:
            continue
        eps_by_sid = {}
        spans_by_sid = {}
        for sid in sids:
            eps = [e for e in jf.episodes(sid) if e.get("Path")]
            eps_by_sid[sid] = eps
            spans = set()
            for e in eps:
                m = _EP_SPAN.search(Path(e["Path"]).name)
                if m:
                    spans.add((int(m.group(1)), int(m.group(2))))
            spans_by_sid[sid] = spans
        canonical = max(sids, key=lambda s: len(eps_by_sid[s]))
        for dup in sids:
            if dup == canonical or not eps_by_sid[dup]:
                continue
            if not spans_by_sid[dup] or not (spans_by_sid[dup] <= spans_by_sid[canonical]):
                continue          # some episodes are unique -> leave for a move pass
            name = (next((n for n, s in series_index.items() if s == canonical), "?"))
            probs = {"show": Path(id_to_path.get(dup, "?")).name, "path": id_to_path.get(dup, ""),
                     "problems": [], "sig": str(len(eps_by_sid[dup]))}
            probs["problems"].append(
                {"kind": "duplicate_series",
                 "detail": f"every episode is already in '{Path(name).name}' "
                           f"(Tmdb {key[0]} / Tvdb {key[1]})",
                 "auto": True, "sev": 3})
            all_probs.append(probs)
            _log(f"FLAGGED duplicate series {probs['show']}: all {len(eps_by_sid[dup])} "
                 f"episode(s) already in {Path(name).name}")
            acted = []
            if not dry_run:
                for e in eps_by_sid[dup]:
                    p = e["Path"]
                    if _delete_video_through_mount(p):
                        acted.append(f"delete {Path(p).name}")
                # Drop the Jellyfin item (files already gone) and the now-empty folder.
                try:
                    jf.delete_item(dup)
                except Exception as exc:                                # noqa: BLE001
                    _log(f"  could not remove duplicate series item: {exc}")
                folder = Path(id_to_path.get(dup, ""))
                try:
                    if folder.is_dir():
                        shutil.rmtree(folder, ignore_errors=True)
                except OSError:
                    pass
            if acted:
                acted_map[probs["show"]] = acted
                for a in acted:
                    _log(f"  -> {probs['show']}: {a}")


# --- one pass ----------------------------------------------------------------

def run_once(dry_run=False, only=None, no_escalate=False):
    """One audit pass. Returns False when it could not reach Jellyfin and did no audit.

    The caller uses that to retry SOON rather than sleeping a full cycle -- see the loop
    in `main`.
    """
    sweep_junk(dry_run)
    try:
        jf = Jellyfin()
    except Exception as e:                                            # noqa: BLE001
        _log(f"Jellyfin unavailable ({e}); mechanical sidecar work only this pass")
        return False
    try:
        series_by_path = jf.series_index()
    except Exception as e:                                            # noqa: BLE001
        _log(f"could not query Jellyfin series list: {e}")
        return False
    try:
        pids_by_id = jf.series_pids()
    except Exception as e:                                            # noqa: BLE001
        _log(f"could not read series provider ids ({e}); identity drift unchecked this pass")
        pids_by_id = {}

    # `tvdb id -> other series pinning it`, the evidence `_series_identity_problem`
    # needs to break a cross-series merge. Jellyfin keys a series' presentation on
    # its provider ids, so TWO series sharing a tvdb id share one presentation key
    # and list each other's seasons/episodes (the 2011/2025 Gumball mixup). Built
    # from Jellyfin's own ProviderIds -- the field the merge actually uses -- not
    # from the nfos, which may lag the item.
    tvdb_owners: dict = {}
    for owner_sid, owner_pids in (pids_by_id or {}).items():
        tv = str((owner_pids or {}).get("Tvdb") or "")
        if not tv:
            continue
        owner_path = next((k for k, v in series_by_path.items() if v == owner_sid), "")
        tvdb_owners.setdefault(tv, []).append({
            "show": Path(owner_path).name if owner_path else owner_sid,
            "path": owner_path,
            "tmdb": (owner_pids or {}).get("Tmdb"),
        })

    state = _load(STATE_FILE, {})
    art_cache = _load(ART_CACHE_FILE, {})
    shows_root = config.MEDIAFS_MOUNT / "Shows"
    all_probs, acted_map, worklist = [], {}, []
    cycle_budget = {"refresh": MAX_REFRESH_PER_CYCLE, "rescan": MAX_RESCAN_PER_CYCLE,
                    "art": MAX_ART_FIX_PER_CYCLE}

    def remember_identity(probs):
        """Baseline the identity artwork was fetched under -- but never while an
        identity-drift repair is still pending, or the evidence is erased unfixed."""
        if any(p["kind"] == "artwork_identity_stale" for p in probs["problems"]):
            return
        cur = pids_by_id.get(probs["sid"]) or {}
        if cur:
            state.setdefault(probs["show"], {})["pids"] = {
                k: str(v) for k, v in cur.items() if k in IDENTITY_KEYS}

    for show_dir in _show_dirs(shows_root, only):
        try:
            probs = diagnose_show(show_dir, jf, series_by_path, pids_by_id,
                                  state, art_cache, tvdb_owners)
        except Exception as e:                                        # noqa: BLE001
            _log(f"  {show_dir.name}: diagnose failed ({e}); skipping")
            continue
        if not probs["problems"]:
            state.setdefault(probs["show"], {})["healthy_ts"] = time.time()
            # A healthy pass forgets the show's stuck counters: a problem that returns
            # later starts its count from this clean pass, not from history.
            _note_problem_pass(state, probs["show"], [])
            remember_identity(probs)
            continue
        remember_identity(probs)
        all_probs.append(probs)
        # Be LOUD about what we see -- one line per flagged show listing every
        # detected problem, so the log makes it obvious the daemon caught it (even
        # when the fix is a slow escalation queued for a later cycle).
        _log(f"FLAGGED {probs['show']}: " + "; ".join(p["detail"] for p in probs["problems"]))
        acted = apply_auto_fixes(probs, jf, state, dry_run, cycle_budget)
        if acted:
            acted_map[probs["show"]] = acted
            for a in acted:
                # `apply_auto_fixes` appends to `acted` BEFORE checking dry_run (so a dry
                # run can report the whole ladder it would climb). That makes the list a
                # plan, not a receipt -- say so, or a dry run reads as a repair that
                # happened.
                _log(f"  -> {probs['show']}: {'WOULD ' if dry_run else ''}{a}")
        if any(not p["auto"] for p in probs["problems"]):
            worklist.append(probs)

    # Loose films under Movies/ (missing plot/poster) -- the show pass never saw them.
    try:
        diagnose_and_fix_movies(jf, dry_run, only, all_probs, acted_map, worklist)
    except Exception as e:                                            # noqa: BLE001
        _log(f"movie pass failed (non-fatal): {e}")

    # Movie COLLECTIONS (BoxSets) with a missing Primary image -- a separate item type
    # neither the show nor the loose-movie pass looked at, so a blank collection cover
    # slipped through until now. Always mechanical (adopt TMDB or a member film's poster).
    try:
        diagnose_and_fix_collections(jf, dry_run, all_probs, acted_map)
    except Exception as e:                                            # noqa: BLE001
        _log(f"collection pass failed (non-fatal): {e}")

    # Duplicate SERIES -- two items sharing a Tmdb/Tvdb id (a re-identified show left a
    # stray second folder). A fully-redundant one is deleted through the mount, not
    # escalated, since nothing would be lost.
    try:
        diagnose_and_fix_duplicate_series(jf, dry_run, all_probs, acted_map)
    except Exception as e:                                            # noqa: BLE001
        _log(f"duplicate-series pass failed (non-fatal): {e}")

    # Escalate at most ONE item per cycle -> bounds API spend, lets budget recover.
    # The worklist mixes every pass's entries, so this reads only the keys the
    # problem-dict contract guarantees -- never anything show- or movie-specific.
    # Escalation is a NON-ingestion AI run, so it fires only inside the off-peak
    # window (config.is_off_peak); outside it the worklist is kept and re-tried at night.
    if worklist and not no_escalate and not dry_run:
        if not config.ai_budget_healthy():
            _log(f"{len(worklist)} item(s) need an AI run; deferred to the off-peak window")
        else:
            now = time.time()
            def eligible(p):
                st = state.get(p["show"], {})
                if st.get("sig") != p["sig"]:
                    return True
                return st.get("escalate_n", 0) < MAX_ESCALATIONS_PER_SIG
            # Only items with a non-auto problem are escalation-worthy; a movie the pass
            # parked because it could not auto-fix (no TMDB match) carries only auto
            # problems, and sorting it by max(sev of non-auto) over an empty list would
            # crash the whole cycle.
            cand = [p for p in worklist
                    if eligible(p) and any(not q["auto"] for q in p["problems"])]
            cand.sort(key=lambda x: -max(q["sev"] for q in x["problems"] if not q["auto"]))
            if cand and budget_available():
                target = cand[0]
                if escalate(target, dry_run):
                    st = state.setdefault(target["show"], {})
                    st["sig"] = target["sig"]
                    st["escalate_ts"] = now
                    st["escalate_n"] = st.get("escalate_n", 0) + 1
            elif cand:
                _log(f"{len(cand)} show(s) need an AI run, but the API is unavailable "
                     f"(no balance or no credential); retry next cycle")

    # `--dry-run` means CHANGE NOTHING, and that has to include our own bookkeeping.
    # Writing state here would advance the fix ladder, baseline show identities, and
    # cache artwork verdicts for repairs that never happened; writing the report would
    # replace the file the human reads with a list of things that were not done. (Both
    # did happen: a scoped dry run overwrote a whole-library `library_health.txt` with
    # one show, every entry labelled `-> fixed:`.)
    if dry_run:
        _log("dry run: state, worklist and library_health.txt left untouched")
    else:
        # The report is written FIRST because it advances the stuck counters in `state`
        # (`_note_problem_pass`). Saving before it persisted the PREVIOUS pass's counts,
        # so a stuck `[auto]` line could never reach `STUCK_AFTER_PASSES` and the report
        # kept calling an unfixable problem automatic forever -- the DW (1963) art loop
        # ran 32 passes and was still `[auto]`.
        try:
            write_report(all_probs, acted_map, scoped=bool(only), state=state)
        except Exception as e:                                        # noqa: BLE001
            _log(f"report write failed (non-fatal): {e}")
        _save(STATE_FILE, state)
        # Invalidated by the (name, size, mtime) signature, so re-adopted artwork is
        # re-verified next pass rather than trusted from this pass's verdict.
        _save(ART_CACHE_FILE, art_cache)
        _save(WORKLIST_FILE, [{"show": p["show"], "problems": p["problems"]} for p in worklist])
        if only:
            _log(f"scoped to {only!r}: library_health.txt left untouched "
                 f"(a one-show report would read as a whole-library all-clear)")
    verb = "would auto-fix" if dry_run else "auto-fixed"
    _log(f"pass done: {len(all_probs)} show(s) flagged, {len(acted_map)} {verb}, "
         f"{len(worklist)} pending human/AI review")


def main():
    ap = argparse.ArgumentParser(description="Continuous Jellyfin<->disk library health daemon.")
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    ap.add_argument("--dry-run", action="store_true", help="report only; change nothing")
    ap.add_argument("--show", help="limit to one show folder name")
    ap.add_argument("--no-escalate", action="store_true",
                    help="mechanical fixes only; never spawn an AI run")
    args = ap.parse_args()

    if args.once or args.show:
        run_once(args.dry_run, args.show, args.no_escalate)
        return 0
    _log(f"media_doctor daemon up (cycle {CYCLE_SEC}s)")
    while True:
        ran = False
        try:
            ran = run_once(args.dry_run, None, args.no_escalate) is not False
        except Exception as e:                                        # noqa: BLE001
            _log(f"cycle error (non-fatal): {e}")
        # A pass that could not reach Jellyfin did NO audit, and sleeping the full cycle
        # after one costs half an hour of staleness for a condition that usually clears in
        # seconds. It is also not a rare accident: `ship-fleet.sh` restarts the mediafs
        # mount, which takes Jellyfin down, and restarts THIS daemon in the same breath --
        # so the doctor's startup pass lands squarely on a Jellyfin that is still coming
        # up. Measured 2026-09-12: two consecutive passes died on `Connection refused`
        # seconds after a deploy, and `library_health.txt` sat 33 minutes stale showing
        # two review items that had already been repaired. Nothing said it was stale;
        # the file carries a timestamp and no one reads it as a freshness claim.
        if ran:
            time.sleep(CYCLE_SEC)
            continue
        _log(f"no audit this pass (Jellyfin unreachable); retrying in {RETRY_SEC}s "
             f"instead of waiting out the {CYCLE_SEC}s cycle")
        time.sleep(RETRY_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
