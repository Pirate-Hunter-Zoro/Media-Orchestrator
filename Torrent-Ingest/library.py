"""Library-side logic: inspect the existing Jellyfin library, validate a
placement plan, apply it atomically, verify it, and (for owned shows) write
locked .nfo so Jellyfin serves our layout instead of re-scraping TMDB.

The existing library on disk is authoritative. guessit/TMDB read a filename and
a database; only the library records the numbering decisions already committed
(e.g. Jujutsu Kaisen as one continuous season). So the digest built here is fed
to the identify step, and validation refuses any plan that would overwrite a
pre-existing file.
"""

import difflib
import json
import os
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import config
import dbhook
import journal

# Pulls the season/episode out of a standard `- SxxExx` episode filename.
_EP_RE = re.compile(r"[Ss](\d+)[Ee](\d+)")

# The full episode designator, including multi-episode spans (S06E07E08). Used to
# anchor src auto-heal so a healed match can never be a *different* episode — only
# the same episode under a differently-transcribed release name.
_EP_TAG_RE = re.compile(r"(?i)s\d{1,3}(?:e\d{1,3})+")

# Manga/comic volumes carry no SxxExx designator; the volume marker is the anchor
# that keeps a src auto-heal from crossing to a *different* volume (`v04` can never
# heal to `v05`). Consulted only when the episode designator above is absent, so a
# video file's `v2` re-release marker never overrides its real `SxxExx` anchor.
_VOL_TAG_RE = re.compile(r"(?i)\bv(\d{1,4})\b")

# The per-show summary scan reads every episode .nfo off the library root, which is far
# too expensive to redo on every identify run. Cache each show's summary keyed by a
# cheap directory-mtime signature; a show is rescanned
# only when its folder or a season folder changes (a new episode or a repaired
# .nfo bumps the dir mtime). Warm-cache digest builds are ~O(one changed show).
_SUMMARY_CACHE_FILE = config.STATE_DIR / "library_summary.json"
# Bump when `show_metadata_summary` changes WHAT it counts. A cached summary is a
# reading of the library and is served without any way to tell it is stale, so the
# 2026-09-24 change that folded the Media-Syncer inventory's EVICTED episodes into the
# counts would otherwise keep serving the SSD-only subset for any show whose directory
# mtime had not moved since its entry was written.
_SUMMARY_CACHE_VERSION = "v2"


# --- folder-name normalization / resolution (shared by the digest + fast-path) ----
#
# The deterministic fast-path (§ diagnosis 6.4) and the scoped digest both need to
# resolve a series name to its actual library folder. The key is the same normalization
# the searcher's `parse.normalize` produces for a folder name: lowercase, fold accents
# to ASCII, strip a trailing `(YYYY)`, collapse non-alphanumerics to single spaces.

# Format/edition labels that must never become part of a folder name. A series is one
# folder; how a particular volume was printed is not a different series.
_EDITION_LABEL_RE = re.compile(
    r"\s*[-\u2013:]?\s*("
    r"\d+(?:st|nd|rd|th)\s+Anniversary\s+Edition|Full\s+Colou?r\s+Collection|"
    r"Minimalist\s+Colou?r|Collector'?s\s+Edition|Eternal\s+Edition|VIZBIG\s+Edition|"
    r"Omnibus\s+Edition|Complete\s+Edition|Deluxe\s+Edition|Resurrected\s+Edition|"
    r"2[-\s]?in[-\s]?1\s+Edition|Colou?red|Omnibus|VIZBIG|Box\s+Set"
    r")\s*$", re.I)


def strip_edition_label(name: str) -> str:
    """`Vinland Saga 2-in-1 Edition` -> `Vinland Saga`. Idempotent, and never empties a name."""
    out = (name or "").strip()
    prev = None
    while prev != out:
        prev = out
        out = _EDITION_LABEL_RE.sub("", out).strip(" -\u2013:")
    return out or (name or "").strip()


def normalize_folder_name(name):
    import unicodedata
    t = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii")
    t = t.lower()
    t = t.replace("'", "")                      # "it's" == "its" == "it’s"
    # A TRAILING '+' is part of the title, not punctuation. `Citrus+` is the sequel of
    # `Citrus`; the general cleanup below deletes the sign, which mapped both to "citrus"
    # -- the sequel became unrepresentable in the franchise table and `Citrus+` resolved
    # onto the original's folder. Keep it as the word "plus". Only a trailing sign:
    # `3.0+1.0` and `Undead + Unluck` carry theirs mid-title and are untouched.
    t = re.sub(r"\+\s*$", " plus", t)
    t = re.sub(r"\(\s*(?:19|20)\d{2}\s*\)", " ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def resolve_show_folder(name):
    """The unique show folder under SHOWS_ROOT whose normalized name matches `name`, or
    None when there is no match or the match is ambiguous (gate (a) of the fast-path)."""
    if not config.SHOWS_ROOT.is_dir():
        return None
    key = normalize_folder_name(name)
    if not key:
        return None
    try:
        hits = [d for d in config.SHOWS_ROOT.iterdir()
                if d.is_dir() and not d.name.startswith(".")
                and normalize_folder_name(d.name) == key]
    except OSError:
        return None
    return hits[0] if len(hits) == 1 else None


def _member_dirs(base):
    """`{normalized folder name: actual folder Path}` for directories under a master.

    A name with more than one folder maps to None (ambiguous -- MEGA has duplicated
    sibling directories before, and a guess between them is worse than no answer).

    One walk per master, shared by `resolve_comic_folder` and `_franchise_block`. It
    exists because a table member VALUE is a canonical default while the live folder may
    be named for the whole series: the Akame ga KILL! row says sub-folder `ZERO` and the
    shelf holds `Akame ga KILL! ZERO` (the layout the owner approved). Resolution must
    see the folder that is really there, or the next volume creates `ZERO/` beside it
    and splits the series -- the 2026-10-03 Citrus+ class.
    """
    dirs: dict = {}
    dup: set = set()
    try:
        for d in Path(base).rglob("*"):
            try:
                if not d.is_dir() or d.name.startswith("."):
                    continue
            except OSError:
                continue
            key = normalize_folder_name(d.name)
            if key in dirs:
                dup.add(key)
            else:
                dirs[key] = d
    except OSError:
        pass
    for key in dup:
        dirs[key] = None
    return dirs


def resolve_comic_folder(name, kind, colored=False):
    """The unique comic series folder for `name`, or None.

    Manga lives under `Comics/Manga/`; a western comic directly under `Comics/`. A franchise
    member (see `config.COMIC_FRANCHISES`) lives under its franchise's master folder. Returns
    None on no match or ambiguity (gate (a)).

    **A FOLDER IS NAMED FOR THE SERIES, NEVER FOR THE EDITION** (owner rule, 2026-09-05).
    This function used to file a colored edition as `<Series> Colored`, and any release whose
    own title carried a format label ("Vinland Saga 2-in-1 Edition", "Ranma 1/2 2-in-1
    Edition") kept that label as its folder. Both split one series across two sibling folders,
    and the split is not cosmetic: `Vinland Saga v29.cbz` arrived under the plain name, sat
    alone next to a 14-volume `Vinland Saga 2-in-1 Edition/`, was taken for a redundant
    duplicate and deleted -- and it was the one volume the 2-in-1 edition did not contain.
    Colour is a property of the FILE (tracked on the `media` row), not of the series."""
    if not config.COMICS_ROOT.is_dir():
        return None
    series = strip_edition_label(name)
    key = normalize_folder_name(series)
    if not key:
        return None
    # `colored=True` no longer BYPASSES the franchise table. It used to, which kept a
    # future coloured One Piece volume out of the master folder and recreated the exact
    # split the owner asked to end (10.5e); colour is handled by the file/DB, not by the
    # path.
    fr = comic_franchise(name, kind)
    if fr is not None:
        # A franchise hit is DETERMINISTIC KNOWLEDGE from `config.COMIC_FRANCHISES`, not an
        # inference from what happens to be on disk, so it resolves to the member's folder
        # whether or not that folder exists yet; `apply_plan` creates the parents. Requiring
        # the folder to exist first made the table useless for exactly the case it is for --
        # the FIRST volume of a member. ElfQuest's "Stargazer's Hunt" resolved to None on its
        # own arrival, the AI was asked instead, and it landed as a bare "ElfQuest v02.cbr"
        # at the franchise root, which is the flattening the table exists to prevent.
        root_rel = ("Manga/" if fr[0]["kind"] == "manga" else "") + fr[0]["name"]
        base = config.COMICS_ROOT / root_rel
        if not fr[1]:
            # An unrecognised "<Franchise> something" -- the prefix fallback in
            # `comic_franchise` knows it belongs to this franchise but not WHICH series it
            # is. It used to resolve to the master root, and that is precisely how a
            # standalone ElfQuest story landed as "ElfQuest v06" in the main run's
            # namespace. We do not know the answer, so we do not invent one: return None
            # and let identify name the series. `validate_plan` refuses a master-root
            # filing outright, so a wrong guess fails closed instead of colliding.
            return None
        leaf_key = normalize_folder_name(fr[1])
        if leaf_key == normalize_folder_name(fr[0]["name"]) and base.is_dir():
            # A FLAT master (the pre-franchise layout): the master series' own files sit
            # directly in the master folder, as One Piece's 322 files do today. Keep
            # resolving to it -- inventing `<Master>/<Master>/` would file the master's
            # next volume into a second folder and split the series in two, which is the
            # fault the franchise table exists to prevent. A member ("Ace's Story") never
            # matches this branch and still gets its own nested folder.
            try:
                if any(p.is_file() and p.suffix.lower() in config.COMIC_EXTENSIONS
                       for p in base.iterdir()):
                    return base
            except OSError:
                pass
        if base.is_dir():
            # Canonical sub-folder first, then the full member name. A live folder is
            # often named for the whole series (`Akame ga KILL! ZERO`, `Citrus+`) while
            # the table's value is the short suffix; matching only the suffix returned
            # the canonical path and split the series on the next arrival.
            dirs = _member_dirs(base)
            for alias in (leaf_key, normalize_folder_name(name),
                          normalize_folder_name(series)):
                if alias and alias in dirs:
                    return dirs[alias]  # a Path, or None when the name is ambiguous
        return base / fr[1]             # the member's canonical home, to be created
    base = config.COMICS_ROOT / ("Manga" if kind == "manga" else "")
    if not base.is_dir():
        return None
    try:
        hits = [d for d in base.iterdir()
                if d.is_dir() and not d.name.startswith(".")
                and normalize_folder_name(d.name) == key]
    except OSError:
        return None
    return hits[0] if len(hits) == 1 else None


def comic_franchise(name, kind):
    """`(franchise_def, subfolder)` for a comic series name that belongs to a known
    franchise, else None. `kind` is "manga" or "comic"; `subfolder` is the member's
    sub-folder under the master (None = the franchise's own main series)."""
    key = normalize_folder_name(name or "")
    if not key:
        return None
    wanted = "manga" if kind == "manga" else "western"
    defs = [fr for fr in config.COMIC_FRANCHISES if fr.get("kind") == wanted]
    for fr in defs:                       # exact member first (carries its sub-folder)
        members = {normalize_folder_name(k): v for k, v in (fr.get("members") or {}).items()}
        if key in members:
            return fr, members[key]
        # The canonical member NAME is itself an alias. The table's values are what the
        # folders are called (`Ace's Story`), and a drop or a prompt may use that name
        # rather than the owner's longer listing — matching only the keys sent
        # `resolve_comic_folder("Ace's Story")` to None and would have let the model
        # file a new member at the top level, splitting the franchise again.
        values = {normalize_folder_name(v): v for v in members.values() if v}
        if key in values:
            return fr, values[key]
    for fr in defs:                       # then prefix fallback (nests under the master)
        members = {normalize_folder_name(k): v for k, v in (fr.get("members") or {}).items()}
        for member in members:
            if member and key.startswith(member + " "):
                return fr, None
    return None


def resolve_novel_folder(name):
    """The unique light-novel series folder under NOVELS_ROOT, or None."""
    try:
        if not config.NOVELS_ROOT.is_dir():
            return None
    except OSError:
        return None
    key = normalize_folder_name(name)
    if not key:
        return None
    try:
        hits = [d for d in config.NOVELS_ROOT.iterdir()
                if d.is_dir() and not d.name.startswith(".")
                and normalize_folder_name(d.name) == key]
    except OSError:
        return None
    return hits[0] if len(hits) == 1 else None


# --- inspection: ground truth for the identify prompt -----------------------

def _franchise_block():
    """An explicit 'which series nest under which master folder' block for the identify
    prompt. Deterministic (from config.COMIC_FRANCHISES) so the AI does not have to infer
    a franchise from folder names — it just reads the mapping."""
    lines = ["COMIC/MANGA FRANCHISES (related series nest under ONE master folder):"]
    for fr in config.COMIC_FRANCHISES:
        root = ("Comics/Manga/" if fr["kind"] == "manga" else "Comics/") + fr["name"]
        # Show the folder that is ACTUALLY on the shelf when one exists, not just the
        # table's canonical default. The shelf sometimes names a member folder for the
        # whole series (`Akame ga KILL! ZERO`, `Fairy Tail - 100 Years Quest`); telling
        # the model the short suffix there would have it create a second folder and
        # split the series -- the fault this block exists to prevent.
        base = config.COMICS_ROOT / (("Manga/" if fr["kind"] == "manga" else "")
                                     + fr["name"])
        dirs = _member_dirs(base) if base.is_dir() else {}
        parts = []
        for member, sub in (fr["members"] or {}).items():
            if not sub:
                continue
            shown = sub
            for alias in (normalize_folder_name(sub), normalize_folder_name(member)):
                if alias and dirs.get(alias):
                    shown = dirs[alias].name
                    break
            if shown not in parts:
                parts.append(shown)
        lines.append(f"  - {root}/  <- {', '.join(parts)}")
    lines.append("  A drop that matches one of these series MUST be filed under its master "
                 "folder AND inside the named sub-folder shown, NEVER as a top-level "
                 "Comics/ entry and NEVER as a file sitting directly in the master folder "
                 "-- the master root holds sub-folders only. The franchise's own main "
                 "series has a sub-folder too, so it sits as a sibling of the spin-offs.")
    return "\n".join(lines)


# The digest's four sections, and the media a download must contain for one to be able
# to inform its placement. A comic pack cannot be filed into Shows/, so 26.6K characters
# of show folders is pure prompt weight for it -- and prompt weight is the whole reason a
# provider refuses a run (see `identify_token_limit`).
DIGEST_SECTIONS = ("shows", "movies", "comics", "novels")


def build_library_digest(series_hint=None, kind=None, sections=None,
                         title_hint=None):
    """Compact text digest of the existing library for the identify prompt.

    Lists show folders (with their season subfolders), movie titles, and comic/novel
    coverage so the identify run can anchor to what already exists before consulting
    TMDB. With a `series_hint` (and its `kind`), the digest is SCOPED to just that one
    series — the other sections (comics/novels/movies for a show, and vice versa) are
    dropped — which removes ~60% of the per-turn input tokens for the runs that still
    use the AI (§ diagnosis 6.3.2).

    `sections` narrows the WHOLE-library digest to the named sections (see
    `DIGEST_SECTIONS`). Unlike `series_hint` this needs no guess about WHICH series the
    drop is — only which KINDS of media it holds, which its file extensions state
    outright — so it is safe on the un-settled path where `series_hint` is unavailable.
    None keeps every section, which is the historical behaviour.
    """
    if series_hint:
        return _build_scoped_digest(series_hint, kind)
    want = set(sections) if sections else set(DIGEST_SECTIONS)
    lines = []
    if "shows" in want:
        lines.append(_digest_shows(title_hint))
    if "movies" in want:
        lines.append(_digest_movies(title_hint))
    if "comics" in want:
        lines.append(_digest_comics())
    if "novels" in want:
        lines.append(_digest_novels())
    lines.append(_franchise_block())
    return "\n".join(lines)


# --- relevance scoping for the shows digest ------------------------------------
#
# The whole-library shows digest is ~32,000 characters -- roughly 300 folders, each with
# its season list and episode counts. Every identify run paid for all of it, so placing a
# Monogatari pack shipped The Office's season breakdown to a provider on a daily budget,
# and buried the handful of lines that actually mattered in three hundred that did not.
#
# Scoping is by TOKEN OVERLAP with the release name, and it is deliberately generous: a
# show gets its full line if it shares any distinctive word with the download. Everything
# else still appears BY NAME, so nothing in the library becomes invisible -- the model can
# always see a folder exists and `ListDir`/`Read` it if the name looks relevant. What it
# loses for those is the season/episode breakdown, which only matters for the show being
# placed.
#
# It FAILS OPEN in both directions that could hurt: no hint, or a hint that matches
# nothing, returns the full digest exactly as before. A wrong guess therefore costs tokens,
# never correctness.

_DIGEST_STOPWORDS = {
    "the", "a", "an", "and", "of", "to", "in", "on", "season", "seasons", "series",
    "complete", "collection", "batch", "bd", "bdrip", "bluray", "web", "webrip", "dl",
    "dual", "audio", "subs", "subbed", "dubbed", "eng", "english", "multi", "hevc",
    "x264", "x265", "h264", "h265", "aac", "flac", "opus", "1080p", "720p", "2160p",
    "4k", "uhd", "remux", "repack", "part", "vol", "volume", "movie", "movies", "ova",
    "special", "specials", "tv", "anime", "raw", "final", "new", "full",
    # Generic nouns that appear in dozens of unrelated titles. Matching on one of these
    # pulls in half the library and defeats the scoping -- "show" alone matched
    # "That '70s Show" against a nonsense hint while this test was being written.
    "show", "shows", "story", "stories", "adventure", "adventures", "chronicles",
    "legend", "legends", "tales", "saga", "world", "war", "wars", "life", "man",
    "girl", "girls", "boy", "boys", "kids", "team", "club", "project", "first",
    "last", "second", "third", "great", "little", "big", "real", "true",
}


def _digest_tokens(text):
    """Distinctive lowercase words in a title, release tags and stopwords removed."""
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", str(text or ""))
    words = re.findall(r"[A-Za-z0-9']+", text.lower())
    return {w for w in words if len(w) >= 4 and w not in _DIGEST_STOPWORDS
            and not w.isdigit()}


def _digest_relevant(title_hint, names):
    """The subset of `names` that plausibly refers to the same work as `title_hint`."""
    hint = _digest_tokens(title_hint)
    if not hint:
        return set()
    out = set()
    for n in names:
        toks = _digest_tokens(n)
        if hint & toks:
            out.add(n)
            continue
        # Catch the spacing/punctuation variants token overlap misses -- "Re:Zero" vs
        # "ReZero", "Mob Psycho 100" vs "MobPsycho100". Compare the letters of the whole
        # title in ORDER, not a sorted bag: sorting turns this into an anagram test that
        # matches almost anything, which is how a nonsense hint first matched a real show.
        flat_h = "".join(sorted(hint, key=str))
        flat_n = "".join(sorted(toks, key=str))
        if len(flat_n) >= 6 and len(flat_h) >= 6 and (flat_n in flat_h or flat_h in flat_n):
            out.add(n)
    return out


def _digest_shows(title_hint=None):
    lines = []
    lines.append("EXISTING SHOWS (folder [numbering, ownership, blank-metadata] -> seasons):")
    lines.append("  Read the bracket before choosing owned/numbering for a matching drop:")
    lines.append("  * numbering=continuous-absolute -> the show keeps ONE Season 01 in absolute")
    lines.append("    order; continue that. numbering=seasoned -> it uses real season splits.")
    lines.append("  * OWNED -> extend the hand-built locked scheme (set owned:true, supply")
    lines.append("    episode_title+plot). scraped -> Jellyfin fills metadata itself.")
    lines.append("  * blank>0 -> that many episodes currently have NO description in the")
    lines.append("    library (the scraper could not resolve them). If you add to such a show,")
    lines.append("    own it and supply metadata; do not repeat the un-owned scheme that blanked.")
    lines.append("  * non-contiguous seasons -> the show's season numbers SKIP (e.g. Seasons 1-9")
    lines.append("    then 11-14, no 10). No provider matches that layout, so the scraper can")
    lines.append("    never resolve it: you MUST own it (owned:true) and supply episode_title+plot")
    lines.append("  * The (N eps) after each season is its episode count. USE IT to place a new")
    lines.append("    drop: the library's season numbers are ground truth, NOT the torrent's")
    lines.append("    'Season N'/'Part N' label. If existing seasons hold ~20 eps each but the")
    lines.append("    drop calls its ~10 eps 'Season 5', the library follows the provider's")
    lines.append("    aired-order scheme (two streaming 'Parts' per season), so the drop is the")
    lines.append("    NEXT season here, not Season 5 — never skip a season number (§ identify.md).")
    if config.SHOWS_ROOT.exists():
        cache = _summary_cache_load()
        dirty = False
        all_shows = sorted(p for p in config.SHOWS_ROOT.iterdir() if p.is_dir())
        relevant = _digest_relevant(title_hint, [p.name for p in all_shows]) \
            if title_hint else set()
        # NO HINT -> the full digest, unchanged. There is nothing to scope by.
        #
        # A HINT THAT MATCHES NOTHING is different, and used to fall back to the full
        # digest too. That is the most expensive case for the least reason: it means no
        # show in the library shares a distinctive word with this release, so no show's
        # season breakdown can be the one being extended -- and it is exactly the case a
        # NEW show hits, where the prompt is already at its largest. The folder NAMES are
        # still all listed, the run is told plainly that nothing matched, and it keeps
        # `ListDir`/`Read` for any name that looks like the match. Season boundaries now
        # come from the provider block anyway, which is authoritative where this never was.
        # A hint that tokenises to NOTHING ("[Group] 1080p BluRay x265") carries no
        # information at all, so it is the same as having no hint: fail open to the full
        # digest. That is a different thing from a REAL hint that matches nothing, which
        # is positive evidence that no existing show is this one.
        if not title_hint or not _digest_tokens(title_hint):
            detailed = all_shows
        elif relevant:
            detailed = [p for p in all_shows if p.name in relevant]
        else:
            detailed = []
        if title_hint and _digest_tokens(title_hint) and not relevant:
            lines.append(f"  NOTE: no show folder in this library shares a distinctive word "
                         f"with this release's name, so none is shown in detail -- this "
                         f"looks like a NEW show. All {len(all_shows)} existing folders are "
                         f"listed by NAME below. If one of them IS this show under a "
                         f"different spelling, say so and read it with ListDir/Read rather "
                         f"than filing this as new.")
        if relevant:
            others = [p.name for p in all_shows if p.name not in relevant]
            lines.append(f"  NOTE: full season detail below is scoped to the {len(detailed)} "
                         f"show(s) whose name matches this download. The library's other "
                         f"{len(others)} show folders are listed by NAME at the end of this "
                         f"section -- if one of them is the real match, say so and read it "
                         f"with ListDir/Read rather than assuming this drop is new.")
        for show in detailed:
            season_dirs = sorted(
                s.name for s in show.iterdir()
                if s.is_dir() and not s.name.startswith(".")
            )
            s, changed = _cached_show_summary(show, cache)
            dirty = dirty or changed
            # Annotate each season folder with its episode count (from the summary),
            # so a "Season 01 (20 eps)" tells the run this library combines Parts.
            counts = s.get("season_counts") or {}
            def _annot(name, _counts=counts):
                m = re.search(r"(\d+)", name)
                c = _counts.get(str(int(m.group(1)))) if m else None
                return f"{name} ({c} eps)" if c else name
            seasons = [_annot(name) for name in season_dirs]
            bits = [s["numbering"]]
            bits.append(f"{s['locked']}/{s['episodes']} locked" if s["locked"] else "scraped")
            if s["blank"]:
                bits.append(f"{s['blank']} blank")
            if s.get("season_gap"):
                bits.append("non-contiguous seasons -> MUST own")
            lines.append(
                f"  - {show.name}  [{', '.join(bits)}] :: "
                f"{', '.join(seasons) or '(no seasons yet)'}"
            )
        if dirty:
            _summary_cache_save(cache)
        if title_hint and _digest_tokens(title_hint):
            others = [p.name for p in all_shows if p.name not in relevant]
            if others:
                lines.append("")
                lines.append(f"  ALL OTHER SHOW FOLDERS ({len(others)}, names only -- "
                             f"ListDir one if you think it is the match):")
                lines.append("    " + "; ".join(others))
    else:
        lines.append("  (Shows root not mounted)")
    lines.append("")
    return "\n".join(lines)


def _digest_movies(title_hint=None):
    """The films the library HOLDS -- read through the MOUNT, not the SSD.

    THE BUG THIS FIXES, measured 2026-09-12. This read `config.MOVIES_ROOT`, which is
    `~/Media/Movies` -- the SSD. A film that has been evicted to the MEGA pool is not there,
    and eviction is the normal state for anything not recently added. So the digest listed
    **6 movies while the library held 334**, and every identify run was told the library was
    all but empty of films.

    That is §3.1 of OPERATING.md turned into a prompt: *"a file missing on `~/Media` has
    been EVICTED to the pool, not lost."* The digest was reading absence as non-existence.

    It is not academic. On 2026-09-12 a `Ghost in the Shell Arise - Border(s) [Movies 1-5]`
    drop was planned into a brand-new SHOW folder -- because Borders 1-4, which the library
    already held as films, were invisible to the run. The harness rejected it (a new
    un-owned show folder with no provider id), so nothing was mis-filed; but the run had no
    way to get it right.

    SHOWS do not have this problem, which is why it hid: a show is a DIRECTORY, and the
    folder plus its sidecars survive eviction even when the videos are gone. A movie is a
    single file, so eviction removes it from the listing entirely.

    Falls back to the SSD when the mount is not up -- a degraded digest beats none, and
    `library_ready()` already gates the ingest on the mount separately.
    """
    lines = ["EXISTING MOVIES (titles):"]
    root = getattr(config, "MEDIAFS_MOUNT", None)
    root = (root / "Movies") if root else None
    if root is None or not root.is_dir():
        root = config.MOVIES_ROOT
    try:
        entries = list(root.iterdir()) if root.exists() else []
    except OSError:
        entries = []
    titles = sorted({
        _movie_base(p.name) for p in entries
        if p.suffix.lower() in config.VIDEO_EXTENSIONS and not p.name.startswith("._")
    })
    if not titles:
        lines.append("  (Movies root not mounted)")
        lines.append("")
        return "\n".join(lines)

    # SCOPED BY RELEVANCE, the way `_digest_shows` scopes seasons by `title_hint`. The
    # whole list is 475 titles / 24,000 characters, which would land in EVERY video prompt
    # -- and prompt weight is exactly what a provider refuses on. Only a film sharing a
    # real word with this drop can possibly be the same film, so those are listed and the
    # rest are counted. The COUNT matters as much as the list: without it a narrowed digest
    # reads as "the library has no movies", which is the very error this function was just
    # fixed for.
    keep = titles
    if title_hint:
        # `_digest_tokens` is the SHOWS tokeniser and is reused deliberately: it already
        # strips release tags and a curated list of generic nouns, and a second, divergent
        # copy is exactly the kind of drift this fleet keeps paying for. (An earlier draft
        # of this function defined its own `_DIGEST_STOPWORDS` and silently SHADOWED that
        # one, breaking the shows scoping -- caught by `test_digest_scoping.py`.)
        want = _digest_tokens(title_hint)
        if want:
            keep = [t for t in titles if want & _digest_tokens(t)]
    for t in keep:
        lines.append(f"  - {t}")
    hidden = len(titles) - len(keep)
    if hidden:
        lines.append(f"  ... and {hidden} other film(s) not matching this release's name. "
                     f"The library holds {len(titles)} films in total; only the ones that "
                     f"could be THIS release are listed above.")
    lines.append("")
    return "\n".join(lines)


def _manga_ceiling_bit(series):
    """A computed volume-ceiling fact for the digest, or "" when unknown (HANDOFF 10.5a).

    Stated as FACT so the free model does not have to guess whether a bare number is
    the next volume: past the ceiling it is a chapter, and a `v` marker past it is a
    mislabel the validator refuses.
    """
    try:
        import comicfacts
        ceil = comicfacts.ceiling_for(series)
    except Exception:                                            # noqa: BLE001
        ceil = None
    if not ceil:
        return ""
    return (f"series has {ceil} volume(s) -- a bare number above {ceil} is a CHAPTER, "
            f"and a `v` marker above {ceil} is a mislabel")


def _digest_comics():
    lines = ["EXISTING COMICS/MANGA (series -> volumes + chapters held):"]
    cov = _comics_coverage()
    if cov:
        for series in sorted(cov):
            vols = sorted(cov[series]["volumes"])
            chaps = sorted(cov[series]["chapters"])
            bits = []
            if vols:
                bits.append(f"volumes v{min(vols):02d}-v{max(vols):02d} ({len(vols)} total)")
            if chaps:
                bits.append(f"chapters c{min(chaps):04d}-c{max(chaps):04d} ({len(chaps)} total)")
            ceil = _manga_ceiling_bit(series)
            if ceil:
                bits.append(ceil)
            lines.append(f"  - {series}  [{', '.join(bits) or 'no volumes/chapters yet'}]")
    else:
        lines.append("  (Comics root not mounted and no remote inventory)")
    lines.append("")
    return "\n".join(lines)


def _digest_novels():
    lines = ["EXISTING LIGHT NOVELS / E-BOOKS (series -> volumes held; live in Google Drive Novels/):"]
    ncov = _novels_coverage()
    if ncov:
        for series in sorted(ncov):
            vols = sorted(ncov[series]["volumes"])
            bits = []
            if vols:
                bits.append(f"volumes v{min(vols):02d}-v{max(vols):02d} ({len(vols)} total)")
            lines.append(f"  - {series}  [{', '.join(bits) or 'no numbered volumes yet'}]")
    else:
        lines.append("  (Novels root not mounted)")
    lines.append("")
    return "\n".join(lines)


def _build_scoped_digest(series_name, kind):
    """A minimal digest scoped to the one series a stored plan names (§ diagnosis 6.3.2).

    Only the section relevant to the drop's `kind` is emitted (and only the matching
    series within it), so the identify run is handed hundreds of tokens instead of the
    ~17K-token whole-library digest. This is what the deterministic fast-path makes
    unnecessary for the common case, and what keeps the residual AI runs cheap."""
    kind = (kind or "").lower()
    lines = []
    if kind in ("anime", "tv"):
        lines.append("EXISTING SHOWS (only the series this drop matches):")
        lines.append("  (the library's season numbers are ground truth, NOT the torrent's "
                     "'Season N'/'Part N' label; continue the existing numbering.)")
        folder = resolve_show_folder(series_name)
        if folder is not None:
            cache = _summary_cache_load()
            s, _ = _cached_show_summary(folder, cache)
            _summary_cache_save(cache)
            season_dirs = sorted(
                d.name for d in folder.iterdir()
                if d.is_dir() and not d.name.startswith(".")
            )
            counts = s.get("season_counts") or {}

            def _annot(name, _counts=counts):
                m = re.search(r"(\d+)", name)
                c = _counts.get(str(int(m.group(1)))) if m else None
                return f"{name} ({c} eps)" if c else name

            seasons = [_annot(name) for name in season_dirs]
            bits = [s["numbering"]]
            bits.append(f"{s['locked']}/{s['episodes']} locked" if s["locked"] else "scraped")
            if s["blank"]:
                bits.append(f"{s['blank']} blank")
            if s.get("season_gap"):
                bits.append("non-contiguous seasons -> MUST own")
            lines.append(f"  - {folder.name}  [{', '.join(bits)}] :: "
                         f"{', '.join(seasons) or '(no seasons yet)'}")
        else:
            lines.append(f"  (no show folder resolves for '{series_name}')")
    elif kind in ("manga", "comic"):
        lines.append("EXISTING COMICS/MANGA (only the series this drop matches):")
        cov = _comics_coverage()
        key = normalize_folder_name(series_name)
        matched = [k for k in cov if normalize_folder_name(k) == key]
        for series in sorted(matched):
            vols = sorted(cov[series]["volumes"])
            chaps = sorted(cov[series]["chapters"])
            bits = []
            if vols:
                bits.append(f"volumes v{min(vols):02d}-v{max(vols):02d} ({len(vols)} total)")
            if chaps:
                bits.append(f"chapters c{min(chaps):04d}-c{max(chaps):04d} ({len(chaps)} total)")
            ceil = _manga_ceiling_bit(series)
            if ceil:
                bits.append(ceil)
            lines.append(f"  - {series}  [{', '.join(bits) or 'no volumes/chapters yet'}]")
        if not matched:
            lines.append(f"  (no comic series resolves for '{series_name}')")
        lines.append("")
        lines.append(_franchise_block())
    elif kind == "lightnovel":
        lines.append("EXISTING LIGHT NOVELS (only the series this drop matches):")
        ncov = _novels_coverage()
        key = normalize_folder_name(series_name)
        matched = [k for k in ncov if normalize_folder_name(k) == key]
        for series in sorted(matched):
            vols = sorted(ncov[series]["volumes"])
            lines.append(f"  - {series}  [volumes v{min(vols):02d}-v{max(vols):02d} "
                         f"({len(vols)} total)]")
        if not matched:
            lines.append(f"  (no novel series resolves for '{series_name}')")
    elif kind == "movie":
        lines.append("EXISTING MOVIES (titles):")
        if config.MOVIES_ROOT.exists():
            titles = sorted({
                _movie_base(p.name) for p in config.MOVIES_ROOT.iterdir()
                if p.is_file() and p.suffix.lower() in config.VIDEO_EXTENSIONS
            })
            for t in titles:
                lines.append(f"  - {t}")
        else:
            lines.append("  (Movies root not mounted)")
    else:
        lines.append("(library digest unavailable for this drop)")
    return "\n".join(lines)


def _comics_coverage():
    """{series_label: {'volumes': set[int], 'chapters': set[int]}} for the digest.

    The authoritative list is Media-Syncer's remote inventory (the pool holds every
    comic even when the local copy is evicted); the local tree is a fallback for
    freshly-ingested files not yet uploaded. Series labels match the library folder
    under Comics/ — `Comics/Manga/<Series>` is labelled `<Series>` (a colored edition
    lives under its own ` Colored` folder and is labelled separately, matching the
    existing `Slam Dunk Colored` convention)."""
    cov = {}

    def _add(label, filename):
        m = re.search(r"\bv(\d{1,4})(?:\.\d+)?\b", filename, re.IGNORECASE)
        if m:
            cov.setdefault(label, {"volumes": set(), "chapters": set()})["volumes"].add(int(m.group(1)))
            return
        m = re.search(r"\bc\.?\s*(\d{2,5})\b", filename, re.IGNORECASE)
        if m:
            cov.setdefault(label, {"volumes": set(), "chapters": set()})["chapters"].add(int(m.group(1)))

    try:
        data = json.loads(config.MEDIA_SYNCER_INVENTORY.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        data = {}
    if isinstance(data, dict):
        for rel in data:
            parts = rel.split("/")
            if len(parts) >= 3 and parts[0] == "Comics":
                label = parts[-2]          # series folder is the file's parent, at any depth
                _add(label, parts[-1])
    if config.COMICS_ROOT.exists():
        for p in config.COMICS_ROOT.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in config.COMIC_EXTENSIONS:
                continue
            parts = p.relative_to(config.COMICS_ROOT).parts
            if len(parts) >= 2:
                label = parts[-2]          # series folder is the file's parent, at any depth
                _add(label, p.name)
    return cov


def _novels_coverage():
    """{series_label: {'volumes': set[int]}} for the digest, read straight off the
    Google Drive Novels tree (novels are not in the media library or Media-Syncer's
    inventory). Each top-level folder under Novels/ is a series; a book number is read
    from a leading "NN - " or a "Volume NN"/"vNN" marker in the filename."""
    cov = {}

    def _add(label, filename):
        num = _novel_number(filename)
        if num is not None:
            cov.setdefault(label, {"volumes": set()})["volumes"].add(num)

    try:
        if config.NOVELS_ROOT.exists():
            for series_dir in config.NOVELS_ROOT.iterdir():
                if not series_dir.is_dir() or series_dir.name.startswith("."):
                    continue
                for p in series_dir.rglob("*"):
                    if p.is_file() and p.suffix.lower() in config.NOVEL_EXTENSIONS:
                        _add(series_dir.name, p.name)
    except OSError:
        pass
    return cov


def _novel_number(filename):
    """Book/volume number from a novel filename: a leading "NN - ", "Volume NN"/"Vol NN",
    or a "vNN" marker. None when the name carries no readable number."""
    m = re.search(r"^\s*(\d{1,4})\s*[-–—]", filename)
    if m:
        return int(m.group(1))
    m = re.search(r"\bvol(?:ume)?\.?\s*(\d{1,4})\b", filename, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"\bv\.?\s*(\d{1,4})\b", filename, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def _movie_base(filename):
    stem = Path(filename).stem
    return stem


# --- episode metadata inspection (shared by the digest, audit, and repair) ---

def episode_nfo_path(video_path):
    """The sidecar .nfo Jellyfin reads/writes for an episode video."""
    return Path(video_path).with_suffix(".nfo")


def _read_text(path):
    try:
        return Path(path).read_text("utf-8", "ignore")
    except OSError:
        return None


def _xml_tag(text, tag):
    """Stripped inner text of the first <tag>…</tag>, or "" (BOM/attr tolerant)."""
    if not text:
        return ""
    m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", text, re.DOTALL | re.IGNORECASE)
    return (m.group(1).strip() if m else "")


def nfo_is_locked(text):
    return bool(text) and "<lockdata>true</lockdata>" in text.lower()


def iter_episode_videos(show_dir):
    """Yield every episode video file under a show folder (recurses seasons)."""
    show_dir = Path(show_dir)
    for p in sorted(show_dir.rglob("*")):
        if p.is_file() and p.suffix.lower() in config.VIDEO_EXTENSIONS:
            yield p


def episode_nfo_state(video_path):
    """`"missing"` | `"unreadable"` | `"blank"` | `"ok"` for an episode's sidecar.

    WHY THIS EXISTS, measured 2026-09-12. `_read_text` returns None for ANY OSError, so a
    sidecar that is absent and a sidecar the mount could not read at that instant were the
    same answer -- and `episode_is_blank` called both of them blank. The library lives on a
    FUSE mount that goes away and comes back whenever `mediafs` restarts, so a deploy is
    enough to make a perfectly healthy show look gutted.

    It did: `library_health.txt` reported "The Powerpuff Girls: 66 of 78 episode(s) have no
    synopsis -- that ratio usually means the series is matched to the WRONG provider entry".
    The ratio heuristic fired, the report told a human to go re-identify the series, and on
    disk 78 of 79 sidecars had a real plot and all 79 were locked. Nothing was wrong with
    the show at all; the mount had simply been restarted under the reader.

    An unreadable sidecar and an empty one are different facts and must not be reported as
    one. "Unreadable" is a statement about the MOUNT, and the doctor raises it as that.
    """
    path = episode_nfo_path(video_path)
    try:
        exists = Path(path).exists()
    except OSError:
        return "unreadable"                 # even stat() failed: the mount, not the file
    if not exists:
        return "missing"
    text = _read_text(path)
    if text is None:
        return "unreadable"                 # it is THERE and we could not read it
    return "ok" if _xml_tag(text, "plot") else "blank"


def episode_is_blank(video_path, show_title=None):
    """True when this episode has no description in Jellyfin — i.e. its .nfo is
    missing or carries no non-empty <plot>. This is the exact bug the user hits
    ("generic episode label with no description"), detected purely from the
    on-disk sidecar (valid because the Nfo metadata saver is required, so the
    .nfo mirrors what Jellyfin holds).

    Detection is deliberately PLOT-centric, not title-centric. A `<title>` of
    "Episode N" or the series name is Jellyfin's unresolved fallback ONLY when it
    also lacks a plot (which the plot check already catches). But plenty of shows
    have episodes that genuinely have no distinct title (numbered-only seasons)
    yet DO carry a real synopsis — those are correctly described and must not be
    treated as blank, or the nightly repair would rewrite them forever. `show_title`
    is accepted for API compatibility but no longer needed.

    A sidecar that EXISTS but cannot be read is deliberately NOT called blank -- see
    `episode_nfo_state`. Inventing a problem out of a transient mount error sends a human
    to re-identify a series that was never mis-identified.
    """
    return episode_nfo_state(video_path) in ("missing", "blank")


_INVENTORY_EPISODES_CACHE: dict = {}


def _inventory_episodes_by_show():
    """`{show folder name: {library-relative episode path}}`, cached by the inventory stat.

    Media-Syncer's remote inventory is the COMPLETE view of the library: a video evicted
    to the pool is gone from `~/Media` but still listed here (HANDOFF §2.1). The comics
    digest already reads it for exactly that reason (`_comics_coverage`).
    """
    try:
        st = config.MEDIA_SYNCER_INVENTORY.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    if _INVENTORY_EPISODES_CACHE.get("key") == key:
        return _INVENTORY_EPISODES_CACHE.get("map") or {}
    by_show: dict = {}
    try:
        data = json.loads(config.MEDIA_SYNCER_INVENTORY.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        data = None
    if isinstance(data, dict):
        for rel in data:
            parts = str(rel).split("/")
            if (len(parts) >= 4 and parts[0] == "Shows"
                    and Path(parts[-1]).suffix.lower() in config.VIDEO_EXTENSIONS):
                by_show.setdefault(parts[1], set()).add(str(rel))
    _INVENTORY_EPISODES_CACHE["key"] = key
    _INVENTORY_EPISODES_CACHE["map"] = by_show
    return by_show


def _episode_videos_complete(show_dir):
    """Every episode video the library holds under one show, as absolute paths.

    THE DISK WALK IS THE SUBSET, THE INVENTORY IS THE WHOLE. Measured 2026-09-24, the
    day two season packs parked as FAILED: The Simpsons held 40 episodes on the mount
    and 4 in Season 03, while the SSD the digest read had zero videos left (all
    evicted) and its cached summary said "Season 03 (2 eps)". The identify run is told
    the library's season counts are ground truth, so it renumbered the next wave's
    S03E05-S03E24 down by two to "fill the gap", onto slots S03E03/E04 already hold --
    the collision guard dropped those files and the coverage contract parked the whole
    700 GB pack. American Dad! was parked the same hour by the same stale picture (the
    SSD said no seasons at all; the inventory holds 34 episodes).

    The walk still runs and is still authoritative for files too new to appear in the
    inventory (it lags one upload cycle) and for fixture directories outside the
    library roots. Inventory paths are returned rooted at `MEDIA_ROOT`, where the
    `.nfo` sidecar lives: sidecars survive eviction even when the video does not, so
    the metadata halves of the summary stay local and cheap. When the local sidecar is
    gone, the mount is the fallback path -- never a mount round-trip per file.
    """
    show_dir = Path(show_dir)
    found: dict = {}
    for p in iter_episode_videos(show_dir):
        rel = None
        for base in (config.MEDIAFS_MOUNT, config.MEDIA_ROOT):
            try:
                rel = p.relative_to(base)
                break
            except (ValueError, OSError):
                continue
        if rel is None:
            found.setdefault(str(p), p)          # fixture dir: keep the walk's path
            continue
        local = config.MEDIA_ROOT / rel
        if not episode_nfo_path(local).exists():
            alt = config.MEDIAFS_MOUNT / rel
            if episode_nfo_path(alt).exists():
                local = alt
        found.setdefault(str(rel), local)
    try:
        show_rel = None
        for base in (config.MEDIAFS_MOUNT, config.MEDIA_ROOT):
            try:
                show_rel = show_dir.relative_to(base)
                break
            except (ValueError, OSError):
                continue
        if show_rel is not None and show_rel.parts[:1] == ("Shows",):
            for rel in _inventory_episodes_by_show().get(show_dir.name, ()):
                found.setdefault(str(rel), config.MEDIA_ROOT / rel)
    except OSError:
        pass
    return list(found.values())


def show_metadata_summary(show_dir):
    """Cheap-ish one-pass summary of a show folder for the digest and tooling:
    episode count, how many episode .nfo are locked, how many episodes are blank,
    and the numbering style (one continuous absolute Season 01 vs real seasons).

    Counts every episode the library HOLDS, evicted ones included -- see
    `_episode_videos_complete` for the 2026-09-24 parking this prevents.
    """
    show_dir = Path(show_dir)
    show_title = re.sub(r"\s*\(\d{4}\)\s*$", "", show_dir.name).strip()
    episodes = locked = blank = 0
    seasons_seen = set()
    season_counts = {}                               # season number -> episode count
    max_ep_s1 = 0
    for video in _episode_videos_complete(show_dir):
        episodes += 1
        m = _EP_RE.search(video.name)
        if m:
            season = int(m.group(1))
            seasons_seen.add(season)
            season_counts[season] = season_counts.get(season, 0) + 1
            if season == 1:
                max_ep_s1 = max(max_ep_s1, int(m.group(2)))
        text = _read_text(episode_nfo_path(video))
        if nfo_is_locked(text):
            locked += 1
        if episode_is_blank(video, show_title):
            blank += 1

    content_seasons = seasons_seen - {0}             # ignore Season 00 (specials)
    # A show whose season numbers are non-contiguous (Monogatari skips Season 10, filed
    # as Seasons 1-9 + 11-14) has NO provider whose aired-order matches this layout, so
    # Jellyfin's scraper cannot resolve it and every un-owned episode goes blank
    # ("Monogatari" with an empty plot). This flag is a deterministic nudge: the identify
    # run and the fast-path treat such a show as MUST-OWN. The gap is read from the FOLDER
    # layout (the "Season NN" dirs), NOT the video files: an evicted season leaves only
    # .nfo sidecars locally, so counting videos would miss the hole entirely.
    dir_seasons: set[int] = set()
    try:
        for sub in show_dir.iterdir():
            if sub.is_dir() and not sub.name.startswith("."):
                m = re.search(r"(\d+)", sub.name)
                if m:
                    dir_seasons.add(int(m.group(1)))
    except OSError:
        pass
    dir_seasons -= {0}
    season_gap = len(dir_seasons) > 1 and \
        set(range(min(dir_seasons), max(dir_seasons) + 1)) != dir_seasons
    if content_seasons == {1} and max_ep_s1 > 60:
        numbering = f"continuous-absolute (S01E1..{max_ep_s1})"
    elif len(content_seasons) > 1:
        numbering = f"seasoned ({len(content_seasons)} seasons)"
    else:
        numbering = "seasoned"
    return {
        "episodes": episodes,
        "locked": locked,
        "blank": blank,
        "numbering": numbering,
        "seasons": sorted(seasons_seen),
        "season_gap": season_gap,
        # season -> episode count. JSON-cached, so keys round-trip as strings;
        # consumers must int() them. Fed to the digest so the identify run sees
        # e.g. "Season 01 (20 eps)" and can tell a provider's aired-order scheme
        # (where two streaming "Parts" collapse into one 20-ep season) from a
        # part-per-season one — the signal that stops a "Part 5" drop being filed
        # as Season 05 over a library whose Seasons 01-02 already hold 20 each.
        "season_counts": {str(k): v for k, v in sorted(season_counts.items())},
    }


def _show_signature(show_dir):
    """Cheap change-detector: the show dir's mtime plus each season dir's mtime.
    Adding an episode or writing a sidecar changes a directory entry, which bumps
    the containing dir's mtime — so a stale cache entry is always invalidated."""
    sig = []
    try:
        sig.append(int(show_dir.stat().st_mtime))
        for sub in sorted(show_dir.iterdir()):
            if sub.is_dir():
                sig.append(f"{sub.name}:{int(sub.stat().st_mtime)}")
    except OSError:
        pass
    return sig


def _summary_cache_load():
    try:
        cache = json.loads(_SUMMARY_CACHE_FILE.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    # Keys carry the version; a reading of a subset is not a reading of the library.
    return {k: v for k, v in cache.items()
            if k.startswith(_SUMMARY_CACHE_VERSION + ":")}


def _summary_cache_save(cache):
    try:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _SUMMARY_CACHE_FILE.with_name(f".{_SUMMARY_CACHE_FILE.name}.tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        os.replace(tmp, _SUMMARY_CACHE_FILE)
    except OSError:
        pass


def _cached_show_summary(show_dir, cache):
    """Return (summary, changed). Reuses the cached summary when the show's
    directory signature is unchanged; otherwise rescans and updates `cache`."""
    key = f"{_SUMMARY_CACHE_VERSION}:{show_dir.name}"
    sig = _show_signature(show_dir)
    hit = cache.get(key)
    if hit and hit.get("sig") == sig:
        return hit["summary"], False
    summary = show_metadata_summary(show_dir)
    cache[key] = {"sig": sig, "summary": summary}
    return summary, True


# --- validation --------------------------------------------------------------

class PlanError(Exception):
    pass


class CollisionPark(PlanError):
    """A same-slot collision whose identity the harness can PROVE is a different episode.

    A subclass so the callers that own the bytes can tell it from an ordinary rejection:
    `_identify_wave` retries on a rejection (the next provider may produce a correct
    plan), but the chunked per-file fallback must PARK on this one, never free it --
    the bytes are the only copy and the collision is a question for a human/tool
    (HANDOFF 10.2). `validate_plan` raises it where `_collision_parked` cannot reach the
    caller.
    """


# Placement is decided PER FILE from its destination top-dir, so a single torrent
# may drop a season into Shows/ and its movie into Movies/ (e.g. Steins;Gate).
def _allowed_ext_for(top):
    if top == "Comics":
        # `.zip` is accepted as a comic source and renamed to `.cbz` on apply.
        return config.COMIC_EXTENSIONS | config.COMIC_CONVERT_EXTENSIONS
    if top in ("Shows", "Movies"):
        return config.VIDEO_EXTENSIONS | config.SUBTITLE_EXTENSIONS
    if top == "Novels":
        return config.NOVEL_EXTENSIONS
    return None


def _media_rel(dst_abs):
    """`dst_abs` relative to MEDIA_ROOT, or None when the destination lives outside it
    (a Novels/ e-book routed to the Google Drive tree). Used to keep the .nfo writers —
    which are show/movie-only concepts — from touching e-book destinations."""
    try:
        return Path(dst_abs).relative_to(config.MEDIA_ROOT.resolve())
    except ValueError:
        return None


def _heal_missing_src(src_p, content_root):
    """Resolve a plan `src` that doesn't exist on disk to the real file it *meant*.

    The identify run occasionally transcribes a source filename with one token
    wrong — most often an audio-codec tag (`AAC2.0` where the real file is
    `DDP2.0`) — because it retypes the name instead of copying it byte-for-byte.
    The file it points at then doesn't exist, and the whole torrent fails the
    src-existence check even though the correct file is sitting right there. This
    heals that specific, safe class of slip: it finds the one real file under the
    downloaded content that is unmistakably the same file under a differently-typed
    name, and returns it. Returns None when there is no unambiguous match (so the
    caller fails closed exactly as before).

    Safety rails — a heal must never silently grab the WRONG file:
      * the candidate must live inside the downloaded content (`content_root`);
      * if the planned name carries an episode designator (SxxExx[Eyy...]), the
        candidate must carry the IDENTICAL one — so a heal can never cross to a
        different episode, only a differently-named copy of the same episode;
      * a manga/comic volume (no episode designator) is anchored on its `vNN`
        volume marker the same way, so a heal can never cross volumes;
      * the candidate must be the same file extension;
      * the name similarity must clear a high threshold, and the best match must
        beat the runner-up by a clear margin, so an ambiguous field of near-ties
        heals to nothing rather than guessing.
    """
    name = src_p.name
    suffix = src_p.suffix.lower()
    tag_m = _EP_TAG_RE.search(name)
    tag = tag_m.group(0).lower() if tag_m else None
    if tag is None:
        # Comics/manga have no episode designator; anchor on the volume marker so a
        # mis-typed volume name (a release token like `(F)` hallucinated into the
        # name) still heals to the same volume instead of failing closed.
        vol_m = _VOL_TAG_RE.search(name)
        tag = vol_m.group(0).lower() if vol_m else None

    # Candidate pool: same-extension files under the downloaded content. Prefer the
    # planned parent dir (the usual case: only the filename token is wrong) but fall
    # back to the whole tree if that dir is itself wrong or absent.
    def _pool(root):
        try:
            return [p for p in root.rglob("*")
                    if p.is_file() and p.suffix.lower() == suffix]
        except OSError:
            return []
    pool = []
    if src_p.parent.is_dir() and (content_root == src_p.parent
                                  or content_root in src_p.parent.parents):
        pool = [p for p in _pool(src_p.parent)]
    if not pool:
        pool = _pool(content_root if content_root.is_dir() else content_root.parent)

    # Episode-anchor filter: same episode designator, exactly.
    if tag is not None:
        pool = [p for p in pool if tag in p.name.lower()]
    if not pool:
        return None

    scored = sorted(
        ((difflib.SequenceMatcher(None, name, p.name).ratio(), p) for p in pool),
        key=lambda t: t[0], reverse=True,
    )
    best_ratio, best = scored[0]
    # With an episode/volume anchor a token swap is a very high-ratio match; without
    # one (movies, or a comic missing a volume marker) demand a stricter score. Either
    # way require a clear margin over any runner-up so a genuinely ambiguous set heals
    # to nothing.
    threshold = 0.80 if tag is not None else 0.92
    if best_ratio < threshold:
        return None
    if len(scored) > 1 and (best_ratio - scored[1][0]) < 0.05:
        return None
    return best.resolve()


def _heal_missing_dir(src_p, content_root):
    """Resolve a plan `src` that names a DIRECTORY (a loose-page story folder) to
    the real folder it meant — the directory analogue of `_heal_missing_src`, for
    the packaging case where the run retypes a story-folder name (an accent, an
    apostrophe). Matches on NFC-normalized names; fail-closed unless one candidate
    clearly wins. Returns None when there is no unambiguous match.
    """
    import unicodedata

    root = content_root if content_root.is_dir() else content_root.parent
    name = unicodedata.normalize("NFC", src_p.name).lower()
    try:
        pool = [d for d in root.rglob("*") if d.is_dir()]
    except OSError:
        return None
    if not pool:
        return None
    # Prefer a candidate in the same parent dir (the usual slip is one token in the
    # folder name, not a moved folder), but fall back to the whole tree.
    same = [d for d in pool if d.parent == src_p.parent]
    if same:
        pool = same
    scored = sorted(
        ((difflib.SequenceMatcher(None, name,
                                  unicodedata.normalize("NFC", d.name).lower()).ratio(), d)
         for d in pool),
        key=lambda t: t[0], reverse=True,
    )
    best_ratio, best = scored[0]
    if best_ratio < 0.85:
        return None
    if len(scored) > 1 and (best_ratio - scored[1][0]) < 0.05:
        return None
    return best.resolve()


def _novel_zip_is_image_scan(src_p):
    """True when a `.zip` planned into Novels/ is really an image-scan manga: the archive
    holds no `.epub`/`.pdf` at all (so it is pages, not a book). False on an unreadable
    archive — fail closed and leave the file to the normal (rejecting) path."""
    try:
        with zipfile.ZipFile(str(src_p)) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return False
    if not names:
        return False
    return not any(n.lower().endswith((".epub", ".pdf")) for n in names)


def _reroute_novel_archives(plan, content_root):
    """Re-route a `.zip` "light novel" that is actually image-scan manga (§ diagnosis §4.2).

    The identify run is misled by a `[Light Novel]` title tag and plans a ZIP of page
    images into `Novels/`; the harness then rejects it as `wrong file type for Novels/`
    and it sits in failed/ for manual handling. This pass sniffs each `.zip` planned into
    `Novels/`: one containing no `.epub`/`.pdf` is re-routed to `Comics/Manga/<Series>/`
    as a `.cbz` (the pipeline already renames `.zip`→`.cbz` on apply), and the plan's
    `media_type` is fixed up to `comic` (or `mixed` if only some files re-route). A
    missing `src` is healed first so a small retype doesn't defeat the re-route.
    """
    files = plan.get("files")
    if not isinstance(files, list):
        return plan
    rerouted = 0
    for f in files:
        if not isinstance(f, dict):
            continue
        dst_rel = f.get("dst_rel") or ""
        if not dst_rel.startswith("Novels/"):
            continue
        src = f.get("src")
        if not src:
            continue
        src_p = Path(src).resolve()
        if not src_p.exists() or not src_p.is_file():
            healed = _heal_missing_src(src_p, content_root)
            if healed is None:
                continue
            src_p = healed
            f["src"] = str(healed)
        if src_p.suffix.lower() not in config.COMIC_CONVERT_EXTENSIONS:
            continue
        if not _novel_zip_is_image_scan(src_p):
            continue
        new = Path("Comics/Manga") / Path(dst_rel).relative_to("Novels")
        if new.suffix.lower() != ".cbz":
            new = new.with_suffix(".cbz")
        f["dst_rel"] = str(new)
        rerouted += 1
    if rerouted:
        plan["media_type"] = "comic" if rerouted == len(files) else "mixed"
    return plan


def verify_provider_ids(plan):
    """Strip a series id the provider itself contradicts; return the reasons.

    HANDOFF 10.3. The Twilight Zone (2019) was filed with `tmdb_id 80979` and
    `tvdb_id 325542`; TMDB 80979 is *Too Cute* (2013) and the correct id is 83135.
    Nothing asked the provider who the id belonged to, so the wrong cover art and
    the wrong `originaltitle`/`premiered` were locked over the owner's series and
    local art outranks remote, so a later correct refresh could not clear them.

    Rules, in the safe direction:
      * a 404 is a mismatch -- the plan names an id that names nothing;
      * a year that differs by more than one from the provider's is a mismatch;
      * a title with no shared distinctive word is a mismatch ONLY when both
        years are known, because romaji-vs-English titles share no words and
        are the ordinary case (Shingeki no Kyojin / Attack on Titan);
      * a tvdb id is checked against TMDB's own `external_ids` for the verified
        tmdb id; a different id is a mismatch, and **no id at all makes the
        claimed one uncorroborated** -- stripped too, because an id TMDB does not
        map for the series collides with whatever series does own it (Jellyfin
        keys a series' presentation on its provider ids, so two series sharing
        one tvdb id merge their seasons and episodes);
      * when the tmdb id was stripped the tvdb id came from the same
        wrong lookup and is stripped with it;
      * no key, no answer, a transport error -> fail open, id kept.

    Mutation is deliberate and idempotent: on a mismatch the id is removed from
    `plan` (so it can never pick art or author an nfo) and the reason is appended
    to `plan["_id_rejections"]` for the run log and the rejection feedback.
    """
    reasons = []
    media_type = plan.get("media_type")
    if media_type not in ("show", "mixed"):
        return reasons
    plan_title = plan.get("title")
    try:
        plan_year = int(plan.get("year")) if plan.get("year") is not None else None
    except (TypeError, ValueError):
        plan_year = None
    try:
        import tmdbguide
    except Exception:                                            # noqa: BLE001
        return reasons
    try:
        ident = tmdbguide.show_identity(plan.get("tmdb_id")) if plan.get("tmdb_id") else None
    except Exception:                                            # noqa: BLE001
        ident = None
    if plan.get("tmdb_id") and ident is not None:
        why = None
        if ident.get("dead"):
            why = "TMDB answers 404 for it"
        else:
            theirs = _identity_words(ident.get("name")) | _identity_words(
                ident.get("original_name"))
            ours = _identity_words(plan_title)
            year = ident.get("year")
            year_bad = (plan_year is not None and year is not None
                        and abs(plan_year - int(year)) > 1)
            # A title with no shared word is only a hint, never the trigger: romaji
            # against English (`Shingeki no Kyojin` / `Attack on Titan`) shares
            # nothing and is the ordinary case. The year is the computable fact.
            title_diff = bool(ours and theirs and not (ours & theirs))
            if year_bad:
                why = (f"the plan says year {plan_year} but TMDB says {year} "
                       f"({ident.get('name')!r})")
                if title_diff:
                    why += f"; the plan titles it {plan_title!r}"
        if why:
            reasons.append(f"tmdb_id {plan.get('tmdb_id')} stripped: {why}")
            plan.pop("tmdb_id", None)
            plan.pop("tvdb_id", None)     # same wrong lookup produced it
            ident = None
    if plan.get("tvdb_id") and ident:
        theirs = ident.get("tvdb_id")
        if not theirs:
            # TMDB records NO tvdb id for the verified tmdb id. The claimed id
            # cannot be corroborated by the provider the harness checks against,
            # and an uncorroborated id is exactly how two shows merge: the 2025
            # revival *The Wonderfully Weird World of Gumball* was pinned with the
            # 2011 *The Amazing World of Gumball*'s `tvdb_id 248482`, Jellyfin
            # built ONE PresentationUniqueKey (`248482-en-…`) for both series, and
            # each show listed the other's seasons and episodes. An id TMDB cannot
            # map is not evidence; strip it. (A transport error leaves `ident`
            # None and still fails open below.)
            reasons.append(
                f"tvdb_id {plan['tvdb_id']} stripped: TMDB records no TVDB id "
                f"for tmdb_id {plan.get('tmdb_id')}, so the id is uncorroborated "
                f"and may collide with another series in Jellyfin")
            plan.pop("tvdb_id", None)
        elif str(plan["tvdb_id"]) != str(theirs):
            reasons.append(
                f"tvdb_id {plan['tvdb_id']} stripped: TMDB records "
                f"{theirs} for the same series")
            plan.pop("tvdb_id", None)
    if reasons:
        plan.setdefault("_id_rejections", []).extend(reasons)
    return reasons


def _duplicate_rank(f):
    """Which copy of one episode to keep: untagged first, then the largest file.

    Shared by the intra-torrent destination collapse and the same-episode ALTERNATE
    collapse so the two can never disagree about the survivor.
    """
    low = str(f.get("src") or "").lower()
    clean = 0 if any(m in low for m in config.DUPLICATE_DEPRIORITIZE_MARKERS) else 1
    try:
        p = f.get("src")
        size = _page_size(p) if p and os.path.exists(p) else 0
    except OSError:
        size = 0
    return (clean, size)


def _collapse_same_episode_alternates(files):
    """Collapse files that are ALTERNATE CUTS of one release episode to one copy.

    HANDOFF 15.2 (Family Guy `S07E07`). A pack ships one episode twice -- a main cut
    plus an alternate scene/audio version under the SAME release `SxxEyy` -- and the
    model names only one. The other then reads as an unplaced release file and the
    coverage contract parks a whole pack. When two planned files share a destination
    episode key AND their source names reduce to the SAME core title
    (`journal.alternate_title_core`, which strips only release version markers), they
    are one episode: the ranked survivor is kept and every sibling is recorded in
    `_deduped_dropped` (accounted-for, so coverage never parks).

    The core comparison is exact, so `II` vs `III` and `Part 1` vs `Part 2` keep
    distinct cores and are never collapsed -- and a group with no provable core (bare
    file numbers) is left alone.

    Returns `(kept, dropped)`; the caller folds `dropped` into `_deduped_dropped`.
    """
    groups = {}
    for f in files:
        rel = Path(f.get("dst_rel") or "")
        if rel.parts[:1] != ("Shows",) or rel.suffix.lower() not in config.VIDEO_EXTENSIONS:
            continue
        key = _file_episode_key(f)
        if key is None:
            continue
        core = journal.alternate_title_core(Path(str(f.get("src") or "")).name)
        if not core:
            continue
        groups.setdefault(key, []).append((f, core))
    dropped = []
    for key, entries in groups.items():
        if len(entries) < 2:
            continue
        if len({core for _f, core in entries}) != 1:
            continue                     # not provably one episode: fail open
        survivor = max((f for f, _c in entries), key=_duplicate_rank)
        # The survivor keeps its own bytes; the model's AUTHORED metadata (title, plot,
        # ids) is carried over from whichever sibling supplied it, so dropping the cut
        # the model happened to name never drops the episode's metadata with it.
        for f, _core in entries:
            if f is survivor:
                continue
            for field in ("episode_title", "plot", "tmdb_id", "type"):
                if not survivor.get(field) and f.get(field):
                    survivor[field] = f[field]
            print(f"[validate_plan] dropped alternate cut of "
                  f"S{key[0]:02d}E{key[1]:02d} (kept {Path(str(survivor.get('src') or '')).name!r}): "
                  f"{Path(str(f.get('src') or '')).name!r}", flush=True)
            dropped.append(f)
    if not dropped:
        return files, []
    kept = [f for f in files if all(f is not d for d in dropped)]
    return kept, dropped


def _reject_title_numbering(files, title_map, identity_map=None):
    """Refuse a destination that contradicts the computed release numbering.

    TWO computed facts, one rule each:

      * `title_map` (reordered release, the Smurfs): the release's own `SxxEyy` is its
        catalogue order, and the harness matched the file's title against the provider
        to compute the true broadcast slot. Filing the release number is refused.
      * `identity_map` (HANDOFF 15.1, American Dad!): the release's own `SxxEyy` IS the
        broadcast slot -- the file's title matched the provider at exactly that number.
        REMAPPING it to another season/episode is refused. Numbered seasons only:
        Season 00's library scheme is its own (HANDOFF 15.5), so a special is never
        constrained by a TMDB number.

    Only files the maps cover are checked; anything unmatched fails open.
    """
    if not title_map and not identity_map:
        return
    for idx, f in enumerate(files):
        src_p = Path(f.get("src") or "")
        m = re.search(r"[Ss](\d{1,3})[Ee](\d{1,4})", src_p.name)
        if not m:
            continue
        try:
            key = (int(m.group(1)), int(m.group(2)))
        except ValueError:
            continue
        exp = (title_map or {}).get(key)
        identity = False
        if not exp and key[0] >= 1:
            exp = (identity_map or {}).get(key)
            identity = bool(exp)
        if not exp:
            continue
        # The DESTINATION is what gets filed, so that is what is checked. The optional
        # `season`/`episode` fields are the model's own annotation and are routinely the
        # release's numbers (or absent); reading them rejected correct plans on
        # 2026-09-20 -- the model put `S01E06 -> S01E01.mp4`, the guard compared the
        # FIELDS `(1, 6)` against the map's `(1, 1)` and rejected its own computed slot.
        got = None
        dm = re.search(r"Season\s+(\d+)/.*?S(\d+)E(\d+)",
                       str(f.get("dst_rel") or ""))
        if dm:
            got = (int(dm.group(2)), int(dm.group(3)))
        if got is None:
            try:
                got = (int(f.get("season")), int(f.get("episode")))
            except (TypeError, ValueError):
                got = None
        if got == tuple(exp):
            continue
        if identity:
            # Season 00 is the LIBRARY's own specials scheme, which routinely differs
            # from the provider's numbering (HANDOFF 15.5, Doctor Who: TMDB calls
            # *The Return of Doctor Mysterio* S00E149 while the locked shelf holds it at
            # S00E04). A numbered release file filed as a special is therefore not
            # constrained by its TMDB number, and an unparseable destination fails open.
            if got is None or got[0] == 0:
                continue
            raise PlanError(
                f"file[{idx}] {src_p.name!r}: the harness matched this file's episode "
                f"title against the provider at its own `S{key[0]:02d}E{key[1]:02d}` -- "
                f"the release numbering HERE IS the broadcast numbering. The plan would "
                f"remap it to {f.get('dst_rel')!r}. File it at S{key[0]:02d}E{key[1]:02d}; "
                f"do not shift a season.")
        raise PlanError(
            f"file[{idx}] {src_p.name!r}: this release's `S{m.group(1)}E{m.group(2)}` "
            f"is its OWN catalogue order, not the broadcast slot. The harness matched "
            f"the file's episode title against the provider and computed "
            f"S{exp[0]:02d}E{exp[1]:02d}, but the plan files it at "
            f"{f.get('dst_rel')!r}. File it at the computed slot.")


def _reject_same_season_episode_shift(files, episode_agreement):
    """Refuse changing a confirmed episode's NUMBER inside its own season.

    THE AMAZON GUMBALL PACK (2026-09-26). Its release files are dot-titled
    (`The.Amazing.World.of.Gumball.S01E03.The.Third.1080p.AMZN.WEB-DL.mkv`) and every
    title matches the provider Jellyfin scrapes at the SAME `SxxEyy` the release states
    -- the release's numbering IS broadcast numbering. The run nonetheless treated the
    32-file season as a continuation of the library and filed E01-E32 at S01E16-E47;
    `validate_plan` accepted it and 32 episodes landed in the wrong slots, which is what
    then parked the other in-flight Gumball pack on the collisions.

    `episode_agreement` is `identify.release_episode_agreement`: own-key title claims
    for dot-named packs. The rule is deliberately the narrowest one that catches that
    shape: the plan may keep, move ACROSS seasons, or file a special; it may not keep
    the season and change the episode when the file's own title confirms the number.
    Replayed over every plan in `state/tmp` with a live guide (190 dot-titled plans):
    this rejects the 30 Gumball AMZN files and ZERO correct historical plans. The
    cross-season case is exempt because libraries deliberately renumber (Steven
    Universe's TMDB S02 opener lives at S01E50; One Piece runs absolute numbers in S01;
    Doctor Who's serials are remapped by `serial_map`), and those packs confirm their
    own keys too. Season 00 plays by the library's own specials scheme, and an
    unparseable destination fails open.
    """
    if not episode_agreement:
        return
    for idx, f in enumerate(files):
        src_p = Path(f.get("src") or "")
        m = re.search(r"[Ss](\d{1,3})[Ee](\d{1,4})", src_p.name)
        if not m:
            continue
        try:
            key = (int(m.group(1)), int(m.group(2)))
        except ValueError:
            continue
        if key not in episode_agreement or key[0] < 1:
            continue
        got = None
        dm = re.search(r"Season\s+(\d+)/.*?S(\d+)E(\d+)", str(f.get("dst_rel") or ""))
        if dm:
            got = (int(dm.group(2)), int(dm.group(3)))
        else:
            try:
                got = (int(f.get("season")), int(f.get("episode")))
            except (TypeError, ValueError):
                got = None
        if got is None or got[0] == 0:
            continue
        if got[0] == key[0] and got[1] != key[1]:
            raise PlanError(
                f"file[{idx}] {src_p.name!r}: the harness matched this file's episode "
                f"title against the provider at its own `S{key[0]:02d}E{key[1]:02d}`, so "
                f"the release numbering is confirmed THERE. The plan would keep Season "
                f"{key[0]:02d} but file it at {f.get('dst_rel')!r} -- E{got[1]:02d} is a "
                f"different episode of the same season. File it at "
                f"S{key[0]:02d}E{key[1]:02d}. A deliberate library renumber moves the "
                f"file to another SEASON (an absolute run or a merged cour); it never "
                f"silently shifts a season's episode numbers.")


def validate_plan(plan, content_root, sibling_seasons=None, serial_map=None,
                  release_name=None, title_map=None, identity_map=None,
                  episode_agreement=None):
    """Raise PlanError if the plan is unsafe or malformed. Returns normalized plan.

    `sibling_seasons` is the set of season numbers the SOURCE the plan was cut from
    also carries outside this plan's slice of it — a chunked torrent's remaining
    waves. It only feeds the season-gap guard; see `_reject_season_gap`.

    `serial_map` is `identify.serial_release_map` over the whole release, and it makes
    the computed broadcast numbering BINDING for a pack that names its files by serial
    (see the guard below). Optional: None simply disables that check.

    `title_map` is `identify.release_title_map`: the release is REORDERED, and this is
    its computed broadcast numbering. `identity_map` is `identify.release_identity_map`:
    the release's own numbering AGREES with the provider, so a remap is refused
    (HANDOFF 15.1). Either may be None; both fail open on uncovered files.

    `episode_agreement` is `identify.release_episode_agreement`: own-key title claims
    for dot-named packs. It feeds `_reject_same_season_episode_shift` -- a plan may not
    keep a confirmed file's season and change its episode number.

    `release_name` is the torrent/content name the drop arrived under. It feeds the
    release-identity guard (`_reject_release_identity`): a release whose own name
    states a year may not be filed into a series of a different one. Optional and
    fail-open -- no year in the name means no check.

    Guarantees before any file is touched:
      * media_type is show|movie|comic|mixed,
      * every source file exists and lives under the downloaded content,
      * every destination is under Shows/, Movies/, or Comics/, and its file type
        matches that top-dir's family (video+subs for Shows/Movies, archives for
        Comics, plus a `.zip` comic that must be filed as `.cbz`) — decided PER
        FILE, so one torrent can span multiple top-dirs,
      * for a non-"mixed" media_type, every file's top-dir matches it,
      * no two SURVIVING planned files target the same destination (intra-torrent
        duplicates that map to one destination are first collapsed to a single
        best copy — see below — so a pack that ships the same episode several
        times is deduped, not failed).

    A destination that ALREADY exists in the library is NOT an error here: it is
    resolved at apply time (the pre-existing file is left untouched and the local
    copy is simply dropped). The "never overwrite a pre-existing file" invariant
    is upheld by apply_plan, which skips rather than clobbers — so a torrent whose
    every file is already present is a success, not a failure.
    """
    if not isinstance(plan, dict):
        raise PlanError("plan is not an object")
    media_type = plan.get("media_type")
    if media_type not in ("show", "movie", "comic", "novel", "mixed"):
        raise PlanError(f"media_type must be show|movie|comic|novel|mixed, got {media_type!r}")

    files = plan.get("files")
    if not isinstance(files, list) or not files:
        raise PlanError("plan.files must be a non-empty list")

    content_root = Path(content_root).resolve()
    # Re-route a `.zip` "light novel" that is really image-scan manga to Comics/ as a
    # `.cbz` before any per-file check runs (§ diagnosis §4.2), so it never fails on
    # "wrong file type for Novels/".
    plan = _reroute_novel_archives(plan, content_root)
    files = plan["files"]
    # The re-route above may have flipped media_type (novel -> comic/mixed); re-read it
    # so the forced_top consistency check below uses the post-route type.
    media_type = plan.get("media_type")
    # NOTE: provider-id verification (`verify_provider_ids`) deliberately does NOT run
    # here. It reaches the network, and `validate_plan` is called by tests and by
    # offline tools that must stay deterministic; it is invoked by `identify` on the
    # model's plan instead, before this function (HANDOFF 10.3).
    # For a single-type plan, every file must land under that type's top-dir; a
    # "mixed" plan lets each file pick its own among the four.
    forced_top = {"show": "Shows", "movie": "Movies", "comic": "Comics",
                  "novel": "Novels"}.get(media_type)
    seen_dst = set()

    # Movie identity guard (mirror of the apply-time seeding in _write_movie_nfo):
    # every placed MOVIE video must resolve to a TMDB id, and no two DISTINCT
    # movies may share one. Without a pinned id Jellyfin either fails to identify
    # an oddly-titled film and ships it blank (the One Piece 3D2Y case) or
    # fuzzy-matches it to a sibling in the same collection (the Madoka Part II ->
    # Part I case); a shared id IS that collision, already present in the plan.
    # The effective id resolves from the file's own `tmdb_id`, or the plan's
    # top-level `tmdb_id` ONLY when the plan places exactly one movie (so it is
    # unambiguous) — the identical rule _write_movie_nfo uses, so validation and
    # seeding can never diverge.
    def _is_movie_video(dst_rel):
        p = Path(dst_rel or "")
        return p.parts[:1] == ("Movies",) and p.suffix.lower() in config.VIDEO_EXTENSIONS
    n_movies = sum(1 for f in files if _is_movie_video(f.get("dst_rel")))
    single_movie_id = plan.get("tmdb_id") if n_movies == 1 else None
    movie_ids = {}

    # --- Intra-torrent duplicate collapse ------------------------------------
    # One pack can ship the SAME episode more than once — most often the real
    # line plus lower-quality alternates in sibling folders (a complete-series
    # One Piece pack bundling "Episode 001-206 Uncropped (480p)" and a colour-
    # distorted 1080p upscale next to the BD/CR line). Every copy maps to the
    # identical destination, so without this the plan would carry N entries for
    # one dst and the duplicate-destination guard below would fail the WHOLE
    # torrent — throwing away the good copy along with the junk. Instead: when
    # several sources target one destination, keep exactly one and drop the rest.
    # The survivor is the source whose path carries NO alternate-version marker
    # (config.DUPLICATE_DEPRIORITIZE_MARKERS), tie-broken by largest file (the
    # higher-bitrate cut) — so the untagged, larger BD/CR copy beats a "480p" or
    # "upscale"-tagged one. Dropped copies are left out of the plan entirely and
    # deleted with the local download. This runs BEFORE per-file validation, so
    # the survivor is what gets healed, checked, and applied.
    def _dst_key(f):
        try:
            return str((config.MEDIA_ROOT / Path(f.get("dst_rel") or "")).resolve())
        except (TypeError, ValueError):
            return None

    def _dup_rank(f):
        return _duplicate_rank(f)

    groups = {}
    for f in files:
        groups.setdefault(_dst_key(f), []).append(f)
    dropped_records = []
    if any(k is not None and len(g) > 1 for k, g in groups.items()):
        deduped = []
        for f in files:
            g = groups.get(_dst_key(f))
            if _dst_key(f) is None or g is None or len(g) == 1 or f is max(g, key=_dup_rank):
                deduped.append(f)
            else:
                dropped_records.append({"src": f.get("src"), "dst_rel": f.get("dst_rel"),
                                        "reason": "duplicate"})
                print(f"[validate_plan] dropped duplicate of {f.get('dst_rel')} "
                      f"(kept higher-quality copy): {f.get('src')}", flush=True)
        files = deduped
        plan["files"] = deduped

    # ALTERNATE CUTS of one release episode that the model gave DIFFERENT destinations
    # (HANDOFF 15.2, Family Guy S07E07): one episode, two cuts under the same release
    # key. The ranked survivor stays; the sibling is recorded as accounted-for.
    files, dropped_alt = _collapse_same_episode_alternates(files)
    for f in dropped_alt:
        dropped_records.append({"src": f.get("src"), "dst_rel": f.get("dst_rel"),
                                "reason": "alternate"})
    if dropped_alt:
        plan["files"] = files
    if dropped_records:
        plan["_deduped_dropped"] = dropped_records

    # Two distinct video files on one episode of one show is a misnumbering (or a
    # duplicate); see `_reject_same_episode` for why the replay allows the shapes it does.
    _reject_same_episode(plan, files)

    # Show identity guard (the movie guard's sibling). A plan that places show video(s)
    # into a show folder that does NOT yet exist (a NEW show, not a match against the
    # library) must pin a TMDB/TVDB id, or Jellyfin cannot identify the series: it ships
    # blank and no scan/refresh ever fixes it, because both match against an identity the
    # item lacks. This is the validator backstop for the identify prompt's "resolve to a
    # real provider" instruction -- the prompt declines an unmatchable torrent (an empty
    # plan), and this refuses the plan when a run places files into a fresh folder but
    # supplies no id. Two cases are deliberately exempt: an OWNED show (its episodes are
    # LOCKED with authored title+plot, enforced by the owned-episode guard below -- One
    # Pace and the YouTube ingest), and an EXISTING folder (its tvshow.nfo already carries
    # the identity, so the plan need not repeat it).
    new_show_folder = None
    for f in files:
        rel = Path(f.get("dst_rel") or "")
        if len(rel.parts) >= 2 and rel.parts[0] == "Shows" \
                and rel.suffix.lower() in config.VIDEO_EXTENSIONS:
            folder = config.MEDIA_ROOT / rel.parts[0] / rel.parts[1]
            if not folder.exists():
                new_show_folder = folder
                break
    if new_show_folder and not plan.get("owned"):
        if not (str(plan.get("tmdb_id") or "").strip()
                or str(plan.get("tvdb_id") or "").strip()):
            raise PlanError(
                f"plan places show video(s) into a new folder ({new_show_folder.name}) "
                f"but carries no tmdb_id/tvdb_id; an un-owned show must pin its identity "
                f"so Jellyfin can identify it -- or mark the plan `owned` and author "
                f"per-episode title+plot")

    # Show LAYOUT guard: `Shows/<Title>/Season NN/<file>`, exactly four segments.
    #
    # A `/` inside a show title is not a naming wart, it is a MERGE. `Fate/Grand Order -
    # Absolute Demonic Front: Babylonia` and `Fate/kaleid liner Prisma Illya` both wrote
    # themselves into one phantom `Shows/Fate/` folder; Jellyfin saw ONE series, identified
    # it as Prisma Illya, and applied Prisma Illya's titles and plots to Babylonia's
    # episodes. Ten of those thirteen episodes were confidently, plausibly WRONG and looked
    # perfectly healthy to the metadata scanner -- only the three that kept raw fansub
    # filenames were ever flagged. Nothing else in the pipeline can catch this: every
    # destination under Shows/ is created by `dst_abs.parent.mkdir(parents=True)`, which
    # turns the extra separator into an extra directory without complaint.
    #
    # Refuse rather than silently rewrite: which character the title should carry instead
    # (`Fate - Grand Order`? `Fate Grand Order`?) is the owner's call, and a plan quietly
    # re-pointed at a folder the model did not name is how content ends up in the wrong
    # show. The next provider in the chain is handed this message and fixes it (§6.5).
    #
    # Free: all 8,987 Shows/ destinations in the journal's entire history are exactly four
    # segments, so this rejects nothing the fleet has ever legitimately planned.
    for idx, f in enumerate(files):
        rel = Path(f.get("dst_rel") or "")
        if rel.parts and rel.parts[0] == "Shows" and len(rel.parts) > 4:
            raise PlanError(
                f"file[{idx}] destination {f.get('dst_rel')!r} has "
                f"{len(rel.parts)} path segments; the Shows layout is exactly "
                f"Shows/<Title>/Season NN/<file>. This is almost always a '/' inside the "
                f"show TITLE (e.g. 'Fate/Grand Order'), which does not create a titled "
                f"folder -- it splits one show across two directories and merges it with "
                f"every other title sharing that first word. Replace the '/' in the title "
                f"(a ' - ' reads best) and re-plan.")

    for idx, f in enumerate(files):
        src = f.get("src")
        dst_rel = f.get("dst_rel")
        if not src or not dst_rel:
            raise PlanError(f"file[{idx}] missing src or dst_rel")

        src_p = Path(src).resolve()
        if not src_p.exists() or (not src_p.is_file() and not src_p.is_dir()):
            # The identify run mis-typed the filename (usually an audio-codec token).
            # Before failing the whole torrent, try to resolve it to the real file
            # it unmistakably meant — same episode, same extension, inside the
            # download (§ _heal_missing_src). Fail closed only if there is no
            # unambiguous match.
            healed = _heal_missing_src(src_p, content_root)
            # A .cbz destination may instead name a DIRECTORY of loose pages (the
            # packaging case) whose story-folder name the run retyped; heal that
            # the same way before giving up.
            if healed is None and Path(dst_rel).suffix.lower() == ".cbz":
                healed = _heal_missing_dir(src_p, content_root)
            if healed is None:
                raise PlanError(f"file[{idx}] src does not exist: {src}")
            f["src"] = str(healed)
            src_p = healed
        # src must be inside what we downloaded (defense against a stray path).
        if content_root not in src_p.parents and src_p != content_root:
            raise PlanError(f"file[{idx}] src outside download: {src}")

        dst_rel_p = Path(dst_rel)
        if dst_rel_p.is_absolute():
            raise PlanError(f"file[{idx}] dst_rel must be relative to media root: {dst_rel}")
        parts = dst_rel_p.parts
        top = parts[0] if parts else None
        if top not in ("Shows", "Movies", "Comics", "Novels"):
            raise PlanError(f"file[{idx}] dst_rel must start with Shows/, Movies/, Comics/, "
                            f"or Novels/: {dst_rel}")
        if forced_top and top != forced_top:
            raise PlanError(f"file[{idx}] top-dir {top}/ inconsistent with media_type "
                            f"{media_type!r} (use media_type 'mixed' for multi-type torrents): {dst_rel}")

        # Novels route to the Google Drive tree, everything else to the media root. The
        # library-relative path still starts with "Novels/" in both cases, so the plan
        # (and the identify run) stay uniform; only the ROOT the applier resolves against
        # differs. Confinement is enforced against whichever root applies.
        root = config.NOVELS_ROOT if top == "Novels" else config.MEDIA_ROOT
        sub_path = dst_rel_p.relative_to(dst_rel_p.parts[0]) if top == "Novels" else dst_rel_p
        dst_abs = (root / sub_path).resolve()
        if root.resolve() not in dst_abs.parents:
            raise PlanError(f"file[{idx}] dst escapes {'Novels' if top == 'Novels' else 'media'} "
                            f"root: {dst_rel}")
        # A directory src is the loose-page comic case: it must be packaged into a
        # Comics .cbz (apply zips it). A file src must match its top-dir's family.
        if src_p.is_dir():
            if top != "Comics" or dst_rel_p.suffix.lower() != ".cbz":
                raise PlanError(f"file[{idx}] a directory src must be packaged as a "
                                f"Comics .cbz: {dst_rel}")
        elif src_p.suffix.lower() not in _allowed_ext_for(top):
            raise PlanError(f"file[{idx}] wrong file type for {top}/: {src}")
        # A comic archive that isn't already a comic extension (a plain `.zip`)
        # must be filed as `.cbz` — apply renames it by copying to that name.
        if top == "Comics" and src_p.suffix.lower() in config.COMIC_CONVERT_EXTENSIONS \
                and dst_rel_p.suffix.lower() != ".cbz":
            raise PlanError(f"file[{idx}] a {src_p.suffix} comic archive must be filed "
                            f"as .cbz: {dst_rel}")
        # ...and the converse, which had no rule at all. A comic that is ALREADY a comic
        # archive must KEEP its own extension. `apply_plan` only ever COPIES a file src --
        # the sole repacking path (`zipfile.ZipFile(dst, "w")`) takes a DIRECTORY of loose
        # pages -- so filing a source under a different archive extension does not convert
        # it, it just lies about the format and produces a file no reader can open.
        # "An_ElfQuest_Story_-_A_Gift_of_Her_Own.cbr" was filed as "ElfQuest v10.cbz" and
        # landed in the library as `RAR archive data, v2.0` wearing a .cbz name.
        if top == "Comics" and not src_p.is_dir() \
                and src_p.suffix.lower() in config.COMIC_EXTENSIONS \
                and dst_rel_p.suffix.lower() != src_p.suffix.lower():
            raise PlanError(f"file[{idx}] a {src_p.suffix} comic is copied, never "
                            f"repacked, so it must keep its extension: {dst_rel}")
        if str(dst_abs) in seen_dst:
            raise PlanError(f"file[{idx}] duplicate destination: {dst_rel}")
        seen_dst.add(str(dst_abs))

        # Every movie video must pin a resolvable, unique TMDB id (see the guard's
        # rationale above). Fail the whole torrent rather than let a movie ship
        # blank or mis-matched — the same fail-closed stance as owned episodes.
        #
        # THE ONE ALTERNATIVE is an OWNED movie: a film that exists on NO metadata
        # provider at all, so there is no id to pin. The canonical case is the
        # YouTube ingest (a long-form video filed as a standalone film) and, in the
        # same spirit as One Pace's owned episodes, a fan edit or original work. The
        # TMDB requirement exists to stop a film shipping BLANK or fuzzy-matched;
        # an owned movie is protected by construction instead, exactly as an owned
        # episode is: the pipeline authors its title/plot and apply_plan writes a
        # LOCKED movie .nfo, so Jellyfin serves what we wrote and never scrapes it.
        # So an owned movie may omit tmdb_id, but it MUST then carry real metadata —
        # otherwise it would be a permanently blank film Jellyfin can never repair,
        # which is the very failure this guard exists to prevent.
        if top == "Movies" and dst_rel_p.suffix.lower() in config.VIDEO_EXTENSIONS:
            eff_id = f.get("tmdb_id") or single_movie_id
            # `owned` may be set per file (a mixed plan where only some films are
            # provider-less) or plan-wide.
            owned_movie = bool(f.get("owned", plan.get("owned")))
            if eff_id is None or not str(eff_id).strip():
                if not owned_movie:
                    raise PlanError(f"file[{idx}] movie has no tmdb_id; a movie must pin "
                                    f"its TMDB id so Jellyfin identifies the exact film "
                                    f"instead of shipping it blank or matching a collection "
                                    f"sibling (use a per-file tmdb_id in a multi-movie "
                                    f"plan), OR mark it `owned` and supply a real "
                                    f"movie_title + plot: {dst_rel}")
                # An owned movie's title may come from the file, or from the plan's
                # top-level title ONLY when the plan places exactly one movie (so it
                # is unambiguous and a mixed plan's SHOW title can never leak onto a
                # film) — the same single-movie rule the id resolution uses.
                title_src = (f.get("movie_title") or f.get("title")
                             or (plan.get("title") if n_movies == 1 else None))
                if not str(title_src or "").strip():
                    raise PlanError(f"file[{idx}] owned movie has no movie_title; it will "
                                    f"be LOCKED, so a missing title is permanently blank "
                                    f"in Jellyfin: {dst_rel}")
                if not str(f.get("plot") or "").strip():
                    raise PlanError(f"file[{idx}] owned movie has no plot; it will be "
                                    f"LOCKED, so a missing plot is permanently blank in "
                                    f"Jellyfin: {dst_rel}")
                # No id to collide with, so the uniqueness check below is skipped —
                # deliberately NOT via `continue`, which would also skip the
                # `_src_abs`/`_dst_abs`/`_src_size` bookkeeping apply_plan requires.
            else:
                eff_id = str(eff_id).strip()
                if eff_id in movie_ids:
                    raise PlanError(f"file[{idx}] tmdb_id {eff_id} is already used by "
                                    f"'{movie_ids[eff_id]}'; two movies cannot share one "
                                    f"TMDB id — that is the collection-sibling mis-match, "
                                    f"in the plan: {dst_rel}")
                movie_ids[eff_id] = dst_rel

        # Owned episodes get LOCKED .nfo, which tells Jellyfin to serve exactly
        # what we wrote and never scrape. Locking an episode with no title/plot
        # therefore produces a permanently blank episode Jellyfin can never
        # repair (the bug that motivated this guard). So an owned show video MUST
        # carry a season, episode, non-empty episode_title, and non-empty plot —
        # otherwise fail the whole torrent rather than lock blanks into place.
        #
        # SEASON-0 SPECIALS ARE HELD TO THE SAME BAR, always — independent of the
        # plan's `owned` flag — because they are always LOCKED at apply time (see
        # _write_owned_nfo). A provider's Season-0 ordering routinely disagrees with
        # a release's own S00Exx numbering, so an un-owned special gets mis-scraped:
        # the wrong title/plot, or — worse, and invisible to the plot-centric audit
        # — a *separate movie's* entry pulled onto it. (Kim Possible: the "So the
        # Drama" film we hold in Movies/ was scraped onto an "A Sitch in Time"
        # special.) Forcing per-special title+plot here makes the identify run author
        # real metadata that apply_plan then locks, so a special is never again left
        # to the scraper's unreliable Season-0 guess.
        is_show_video = (top == "Shows"
                         and src_p.suffix.lower() in config.VIDEO_EXTENSIONS)
        try:
            is_special = is_show_video and int(f.get("season")) == 0
        except (TypeError, ValueError):
            is_special = False
        if is_show_video and (f.get("owned", plan.get("owned")) or is_special):
            role = "Season-0 special" if is_special else "owned show episode"
            why = ("provider Season-0 ordering is unreliable, so an un-owned special "
                   "gets mis-scraped — wrong metadata, or a separate movie's entry "
                   "pulled onto it; every special is locked and must carry real metadata"
                   if is_special else
                   "locking it makes the episode permanently blank in Jellyfin")
            if f.get("season") is None or f.get("episode") is None:
                raise PlanError(f"file[{idx}] {role} missing season/episode: {dst_rel}")
            if not str(f.get("episode_title") or "").strip():
                raise PlanError(f"file[{idx}] {role} has no episode_title ({why}): {dst_rel}")
            if not str(f.get("plot") or "").strip():
                raise PlanError(f"file[{idx}] {role} has no plot ({why}): {dst_rel}")

        f["_src_abs"] = str(src_p)
        f["_dst_abs"] = str(dst_abs)
        f["_src_size"] = _page_size(src_p) if src_p.is_dir() else src_p.stat().st_size

    # --- Manga supersede (hierarchy: volume replaces chapters, colored replaces B/W) --
    # The identify run may name existing library files that a new volume/colored volume
    # makes redundant. Validate them here (Comics-only, confined, not being written by
    # this plan) so apply_plan can delete them locally and purge them remotely.
    supersedes = plan.get("supersedes")
    if supersedes is not None:
        if not isinstance(supersedes, list):
            raise PlanError("plan.supersedes must be a list of library-relative paths")
        writing = {str(f.get("_dst_abs")) for f in files}
        for idx, sp in enumerate(supersedes):
            if not isinstance(sp, str) or not sp:
                raise PlanError(f"supersedes[{idx}] must be a non-empty path")
            sp_path = Path(sp)
            if sp_path.is_absolute() or sp_path.parts[:1] != ("Comics",):
                raise PlanError(f"supersedes[{idx}] must be a Comics/ library-relative "
                                f"path: {sp}")
            sp_abs = (config.MEDIA_ROOT / sp_path).resolve()
            if config.MEDIA_ROOT.resolve() not in sp_abs.parents:
                raise PlanError(f"supersedes[{idx}] escapes media root: {sp}")
            if sp_path.suffix.lower() not in (
                    config.COMIC_EXTENSIONS | config.COMIC_CONVERT_EXTENSIONS):
                raise PlanError(f"supersedes[{idx}] is not a comic file: {sp}")
            if str(sp_abs) in writing:
                raise PlanError(f"supersedes[{idx}] is also being written by this plan: {sp}")

    # --- serial-numbered releases: the numbering is arithmetic --------------------
    # A pack whose files are named `S01E05 (005) - The Keys of Marinus (1)` uses the
    # release's SERIAL as `SxxEyy`, so every part of a story advertises the same episode.
    # `serial_map` is `identify.serial_release_map` over the WHOLE release (folder,
    # basename) -> computed broadcast slot. A plan that files a mapped file anywhere else
    # is contradicting arithmetic, so it is refused here -- the model gets the computed
    # numbers in its prompt, and this is the backstop that makes them binding. Fail-open
    # for BOTH sides: no map, or a file the map does not name (an extra, a movie), is
    # simply not checked.
    if serial_map:
        for idx, f in enumerate(files):
            src_p = Path(f.get("src") or "")
            exp = serial_map.get((src_p.parent.name, src_p.name))
            if not exp:
                continue
            m = re.search(r"[Ss](\d{1,3})[Ee](\d{1,4})", Path(f.get("dst_rel") or "").name)
            got = (int(m.group(1)), int(m.group(2))) if m else None
            if got != (exp["season"], exp["episode"]):
                raise PlanError(
                    f"file[{idx}] {src_p.name!r} is {exp['story']!r} part {exp['part']} "
                    f"of a SERIAL-NUMBERED release: its computed broadcast slot is "
                    f"S{exp['season']:02d}E{exp['episode']:02d}, but the plan files it at "
                    f"{f.get('dst_rel')!r}. The `SxxEyy` in the release filename is a "
                    f"SERIAL number, not the episode -- do not copy it onto the "
                    f"destination. File it at the computed slot.")

    _reject_comic_at_franchise_root(files)
    _reject_franchise_member_outside_master(files)
    _reject_manga_mislabels(plan, files)
    _reject_title_numbering(files, title_map, identity_map)
    _reject_same_season_episode_shift(files, episode_agreement)
    _reject_absolute_run_split(files)
    _reject_arc_split_across_seasons(files)
    _reject_season_over_provider_count(plan, files)
    _reject_season_gap(plan, files, sibling_seasons)
    _reject_release_identity(plan, files, release_name)
    files, dropped_ep = _collapse_existing_episode_collisions(files)
    if dropped_ep:
        # A collision is NOT a junk verdict: the planned file may be the right content
        # for that slot and the file already on disk the wrong one (Doctor Who (2005)
        # filed into the (1963) series -- every early-2005 file collided with an existing
        # classic slot). It is kept OUT of the plan so no byte is written, but the
        # coverage contract must still see it as unaccounted and PARK the release
        # (HANDOFF 10.2). `_deduped_dropped` is the opposite -- an intra-torrent
        # duplicate whose surviving copy IS in the plan -- and stays accounted-for.
        plan["files"] = files
        plan["_collision_parked"] = [
            {"src": d.get("src"), "dst_rel": d.get("dst_rel"), "reason": "collision"}
            for d in dropped_ep
        ]
    return plan


def _season_of(dst_rel):
    """Content season number (>0) a show-video destination lands in, else None.
    Reads the `Season NN` folder component (placement is what matters here);
    Season 00 / specials return None so they never count toward the gap check."""
    parts = Path(dst_rel or "").parts
    if len(parts) < 3 or parts[0] != "Shows":
        return None
    m = re.search(r"(\d+)", parts[2])                # parts[2] == "Season NN"
    if not m:
        return None
    n = int(m.group(1))
    return n if n > 0 else None


_EPISODE_NUM_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,4})")


def _file_episode_key(f):
    """(season, episode) base number of a show-video plan entry, else None.

    Prefers the plan's own `season`/`episode` fields; falls back to parsing the
    destination filename's `SxxEyy`, so the guard also works for a run that placed
    a bare-numbered filename (no explicit episode fields)."""
    season = f.get("season")
    episode = f.get("episode")
    try:
        s = int(season) if season is not None else None
        e = int(episode) if episode is not None else None
    except (TypeError, ValueError):
        s = e = None
    if s is not None and e is not None:
        return (s, e)
    m = _EPISODE_NUM_RE.search(Path(f.get("dst_rel") or "").name)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)))


def _reject_release_identity(plan, files, release_name):
    """Refuse a plan whose own year contradicts the series folder it targets.

    THE INCIDENT: Doctor Who (2005). `74c608c7ba56dd4b3f2c04ab3999f045d767013f`
    ("Doctor Who Seasons 1 to 13 Mp4 1080p") was identified as Doctor Who (1963) by the
    provider that served the first wave. Every early-2005 file collided with an existing
    classic-series slot, the collision collapse removed those files from the plan, and
    the cleanup then deleted 38 files / 60.9 GB as "not in plan" (HANDOFF 10.2). Later
    waves of the same release identified it correctly as (2005), which is the tell: the
    release and the destination were never the same show.

    WHAT THE FIRST REPLAY TAUGHT, and why the rule is this narrow. A title-overlap plus
    "years differ" rule rejected 22 of 177 historical whole-torrent plans and almost
    none of them were wrong: `Lupin III Part IV (2015)` lives in `Lupin III (1971)` by
    the owner's franchise convention, `Little Witch Academia [Movies 2013+2015]` in the
    (2017) series folder, `SAC_2045`'s own title number read as a year, `[DD3A2043]` (a
    CRC32) read as 2043. A guard that rejects real content is worse than the bug it
    fixes, so the checks are narrowed to the shapes the fleet has never legitimately
    produced:

      * the release name's year(s) matter only when the release name says NOTHING MORE
        than the folder's own name (`Lupin III Part IV Italian Adventure` does -- its
        extra words mark it a part/edition entry, and those are filed into franchise
        folders on purpose);
      * square-bracket groups and `1920x1080` pairs are stripped before looking for a
        year, and a number the DESTINATION folder also carries (SAC_2045, Blade Runner
        2049) is the show's title, not a year claim;
      * the plan's own `year` is compared only when the plan targets exactly ONE show
        folder (a multi-show pack's top-level year is one member's, not the pack's --
        the Steins;Gate + Steins;Gate 0 and Haruhi false positives), and only with the
        same subset gate.

    Tolerance is one year: production-vs-premiere off-by-one is ordinary, while 2005
    against 1963 is 42. Fail open everywhere else -- no year, no name overlap, a
    folder without a year, an unparseable value -> no opinion.
    """
    plan_year = None
    try:
        plan_year = int(plan.get("year")) if plan.get("year") is not None else None
    except (TypeError, ValueError):
        plan_year = None
    folders = sorted({Path(f.get("dst_rel") or "").parts[1]
                      for f in files
                      if len(Path(f.get("dst_rel") or "").parts) >= 2
                      and Path(f.get("dst_rel") or "").parts[0] == "Shows"})
    if not folders:
        return
    release_words = _identity_words(release_name)
    plan_words = _identity_words(plan.get("title"))
    for folder in folders:
        dst_year = _folder_year(folder)
        if dst_year is None:
            continue                      # a folder without a year carries no claim
        folder_words = _identity_words(_series_name_of(folder))
        extra = release_words - folder_words if (release_words and folder_words) else set()
        if release_words and not extra:
            rel_years = _release_years(release_name)
            if rel_years and not any(abs(y - dst_year) <= 1 for y in rel_years):
                # A year the folder itself carries (SAC_2045, Blade Runner 2049) is the
                # show's title number, not a claim about the release -- so a release whose
                # every year is a title number of the destination is not a mismatch.
                folder_title_numbers = {int(m.group(1)) for m in _YEAR_RE.finditer(folder)}
                if not rel_years <= folder_title_numbers:
                    raise PlanError(
                        f"release identity mismatch: the release is named "
                        f"{str(release_name)[:80]!r} (year {sorted(rel_years)}) but the "
                        f"plan files it into '{folder}' ({dst_year}). A release whose "
                        f"own name states a year may not be filed into a series of "
                        f"another one -- that is almost always a remake/reboot/revival "
                        f"being merged into the original show, and every episode will "
                        f"scrape the wrong series' metadata. Re-check which series this "
                        f"release actually is, and file it under that series' own folder "
                        f"(or own it, with real per-episode titles+plots, if the provider "
                        f"carries no entry for it).")
        if len(folders) == 1 and plan_year is not None:
            words = plan_words or release_words
            if words and folder_words and words <= folder_words \
                    and abs(plan_year - dst_year) > 1:
                raise PlanError(
                    f"release identity mismatch: the plan says this is "
                    f"{plan.get('title')!r} ({plan_year}) but files it into '{folder}' "
                    f"({dst_year}). The plan's own identity contradicts the series "
                    f"folder it targets. Re-check which series this release actually is "
                    f"and file it under that series' own folder.")


_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_NON_YEARS = {480, 576, 720, 1080, 1280, 1440, 1600, 1920, 2048, 2160, 2560, 3840, 4320}
_RESOLUTION_RE = re.compile(r"\d{3,4}\s*x\s*\d{3,4}", re.IGNORECASE)
_BRACKET_RE = re.compile(r"\[[^\]]*\]")
_EP_TOKEN_RE = re.compile(r"\bs\d{1,3}e\d{1,4}\b|\b\d{3,4}p\b|\b\d{1,2}bit\b", re.IGNORECASE)
_IDENTITY_STOPWORDS = {
    "the", "a", "an", "of", "and", "complete", "season", "seasons", "series", "vol",
    "volume", "part", "parts", "disc", "disk", "remaster", "remastered", "edition",
    "uncut", "extended", "dvdrip", "bdrip", "bluray", "webrip", "web", "multi",
    "audio", "dual", "mp4", "mkv", "x264", "x265", "hevc", "aac", "flac", "sub",
    "subs", "custom", "proper", "repack", "batch", "movie", "movies", "special",
    "specials", "ova", "ovas",
}


def _release_years(name):
    """Years a release name claims, with the traps the first replay found removed.

    Square-bracket groups carry group tags and CRC32s (`[DD3A2043]` -> "2043") and
    resolution pairs (`[1920x1080 ...]` -> "1920"); both are stripped first. Whether a
    remaining year is the show's title number (SAC_2045) rather than a claim is decided
    by the caller, which knows the destination folder.
    """
    s = _BRACKET_RE.sub(" ", str(name or ""))
    s = _RESOLUTION_RE.sub(" ", s)
    return {int(m.group(1)) for m in _YEAR_RE.finditer(s)
            if int(m.group(1)) not in _NON_YEARS}


def _folder_year(folder):
    m = re.search(r"\((\d{4})\)\s*$", str(folder or ""))
    return int(m.group(1)) if m else None


def _identity_words(text):
    """Distinctive lowercase words of a release or series name, tags dropped."""
    s = _BRACKET_RE.sub(" ", str(text or ""))
    s = _EP_TOKEN_RE.sub(" ", s)
    s = _RESOLUTION_RE.sub(" ", s)
    words = re.sub(r"[^a-z0-9]+", " ", s.lower()).split()
    return {w for w in words if w not in _IDENTITY_STOPWORDS and not w.isdigit()
            and len(w) > 1}


def _titles_same_episode(a, b):
    """Whether two recorded episode titles are the same episode, spellings aside."""
    import difflib
    na = re.sub(r"[^a-z0-9]+", " ", str(a or "").lower()).strip()
    nb = re.sub(r"[^a-z0-9]+", " ", str(b or "").lower()).strip()
    if not na or not nb:
        return False
    if na == nb:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.85


def _clean_episode_title(text):
    """An episode title with its release-tag tail removed, for identity comparison.

    The raw extraction keeps everything between the SxxEyy dash and the extension, so
    `... - Homer Defined [DSNP WEBDL-1080p][EAC3 5.1][h264]-HONE` and the SAME file at
    another slot share a 40-character tag suffix. SequenceMatcher scores that pair
    0.838 -- under `_titles_same_episode`'s 0.85 bar, but only by accident, and a
    different codec/group pair crosses it and reads two different episodes as one.
    The title is the part before the first bracket group; a trailing `-GROUP` (` -HONE`)
    comes off too. Applied to BOTH sides of every comparison, so it cannot invent a
    difference by cleaning only one.
    """
    t = _BRACKET_RE.sub(" ", str(text or ""))
    t = re.sub(r"\s+-\s*[A-Za-z0-9_]+\s*$", "", t)
    return t.strip()


def _existing_episode_mismatch(planned, rel, collisions):
    """A PlanError message when a colliding existing file is provably a different episode.

    The journal records the source title every destination was filed from
    (`journal.source_titles`), and the planned file's source title names its content.
    When both are known and clearly different, the existing file is a wrong-slot copy:
    dropping the planned file would file nothing and cement the misplacement. Returns
    "" when identity cannot be proven different, so the historical duplicate-drop stands
    for the ordinary same-episode / no-evidence cases.

    WHEN THE JOURNAL IS SILENT, THE EXISTING FILE'S OWN NAME IS THE SECOND WITNESS. A
    chunked wave's already-filed episodes carry their release title in their filename;
    the name alone proves `When Flanders Failed` is not the planned `Homer Defined`
    (the 2026-09-24 wave that would have renamed a correct S03E05 into the occupied
    S03E03). A bare-numbered filename carries no title and proves nothing, so that case
    keeps the historical drop. Comparison is on tag-cleaned titles (see
    `_clean_episode_title`), because the shared tag suffix of two same-release files
    otherwise inflates their similarity toward the same-episode threshold.
    """
    plan_title = journal.title_from_release_name(
        str(planned.get("src") or "").rsplit("/", 1)[-1])
    if not plan_title:
        return ""
    plan_clean = _clean_episode_title(plan_title)
    if not plan_clean:
        return ""
    book = journal.source_titles()
    base = Path(*rel.parts[:3])
    diffs = []
    for existing in collisions:
        have = book.get(str(base / existing.name))
        if not have:
            have = journal.title_from_release_name(existing.name)
        have_clean = _clean_episode_title(have)
        if have_clean and not _titles_same_episode(have_clean, plan_clean):
            diffs.append((existing.name, have_clean))
    if not diffs:
        return ""
    detail = "; ".join(f"{name!r} holds {title!r}" for name, title in diffs)
    dst_name = Path(str(planned.get("dst_rel") or "")).name
    return (f"slot {rel.parts[2]}/S{_file_episode_key(planned)[0]:02d}"
            f"E{_file_episode_key(planned)[1]:02d} is already held by a differently-named "
            f"file whose recorded content contradicts this plan: {detail}, while the "
            f"planned {dst_name!r} carries {plan_title!r}. This is a wrong-slot file, not "
            f"a duplicate -- re-file it (scripts/refile_season.py --mapping), never "
            f"silently drop the planned copy.")


def queued_for_purge():
    """Library-relative paths already queued for MEGA purge (queue + in-flight batch).

    A purge has two halves: the local unlink (which is a no-op for an episode evicted to
    the pool) and the reaper's remote delete. Between them the pool copy is still served
    through the mount, so a just-superseded episode would read as "already present" and
    block the replacement that superseded it -- the exact window `pack_conflict` opens
    when it supersedes a displaced duplicate's footprint and lets the blocked pack retry.
    The queue is the harness's own statement that these paths are gone on purpose, which
    is also how the owner report already treats them (a queued purge reads PENDING, not
    FAIL). Best-effort: an unreadable queue answers nothing, so the caller keeps its
    historical behavior.
    """
    out = set()
    try:
        q = config.MEDIAFS_DELETIONS_QUEUE
    except AttributeError:
        return out
    for name in (q.name, q.name + ".processing"):
        try:
            text = q.with_name(name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rel = json.loads(line).get("path")
            except (ValueError, AttributeError):
                continue
            if rel:
                out.add(str(rel))
    return out


def _collapse_existing_episode_collisions(files):
    """Drop planned show episodes that duplicate an episode ALREADY on disk under the
    same season+episode number but a DIFFERENT filename.

    The write-once / single-residence invariant is per EPISODE, not per filename:
    "Helluva Boss ... - S02E05.mkv" and "Helluva Boss ... - S02E05 - Unhappy
    Campers.mkv" are the same episode filed under two paths (a title-with-title vs
    bare-number rename mismatch), and both on disk breaks the invariant and accumulates
    duplicates. `apply_plan` only skips an EXACT destination path, so a differently-
    named sibling slips past it. This guard collapses such a planned file before any
    byte is written — the same shape as the intra-torrent duplicate collapse above.

    Deliberately scoped to Shows only, and to an EXACT (season, episode) match against
    a differently-named existing video file in the same `Season NN` folder: it never
    touches a planned file whose destination is genuinely absent, and it never deletes
    the existing library copy. An anime quality-upgrade that wants to replace an episode
    at a different name must name the SAME destination path (which apply_plan's replace
    path handles); a differently-named same-number file is a duplicate, not an upgrade.
    """
    dropped: list[dict] = []
    kept: list[dict] = []
    for f in files:
        rel = Path(f.get("dst_rel") or "")
        if len(rel.parts) < 3 or rel.parts[0] != "Shows":
            kept.append(f)
            continue
        if rel.suffix.lower() not in config.VIDEO_EXTENSIONS:
            kept.append(f)
            continue
        key = _file_episode_key(f)
        if key is None:
            kept.append(f)
            continue
        # The MOUNT is the complete view: `~/Media` is only the SSD cache and an
        # evicted episode does not exist there (HANDOFF §2.1). This scan used to read
        # the SSD alone, so a pool-only file already holding the slot was invisible and
        # the planned file landed beside it -- the Smurfs' 40 old dvdrip S01 files were
        # evicted, the replacement pack's S01 files were applied next to them, and the
        # duplicate then read as a cleanup decision (handed to media_doctor, 2026-09-20).
        # Both roots are scanned; the mount wins on a name collision (same bytes).
        entries: list[Path] = []
        seen_names: set[str] = set()
        # A path already queued for purge is GONE (see `queued_for_purge`): the pool copy
        # the mount still serves must not block the replacement that superseded it.
        purge_queued = queued_for_purge()
        for base in (config.MEDIAFS_MOUNT, config.MEDIA_ROOT):
            season_dir = base / rel.parts[0] / rel.parts[1] / rel.parts[2]
            if not season_dir.is_dir():
                continue
            try:
                listing = list(season_dir.iterdir())
            except OSError:
                continue
            for p in listing:
                if p.name in seen_names:
                    continue
                if str(Path(*rel.parts[:3]) / p.name) in purge_queued:
                    continue
                seen_names.add(p.name)
                entries.append(p)
        dst_name = rel.name
        # Every differently-named file already holding this slot. The COUNT matters
        # for the One Pace retarget below, so they are collected, not short-circuited.
        collisions = []
        for existing in entries:
            if not existing.is_file():
                continue
            if existing.suffix.lower() not in config.VIDEO_EXTENSIONS:
                continue
            if existing.name == dst_name:
                continue        # exact path: apply_plan's preexisting skip handles it
            m = _EPISODE_NUM_RE.search(existing.name)
            if not m:
                continue
            if (int(m.group(1)), int(m.group(2))) == key:
                collisions.append(existing)
        collision = bool(collisions)
        if collision:
            # ONE PACE IS THE EXCEPTION, because for One Pace a same-slot repeat is
            # the INTENDED outcome, not a duplicate: it is the library's lone churn
            # class (§ _overwrites_preexisting) and a re-release is meant to replace
            # the cut on disk.
            #
            # And One Pace re-cuts routinely change the episode TITLE -- the filename
            # carries it, so the new cut's destination differs from the old file's
            # path at the same slot, and this guard used to drop it. The replacement
            # then silently never landed. Measured: One Pace re-released ch. 141-145
            # "Quack Doctor" as ch. 140-145 "Inherited Will" at S13E05.
            #
            # `prompts/identify.md` already tells the run to reuse the existing
            # filename, but that is an instruction to a free model in a 67,000-char
            # prompt. So the harness does it deterministically instead: RETARGET the
            # planned file onto the existing path, and let apply_plan's replace branch
            # overwrite it gaplessly (os.replace, same volume -- the episode is never
            # absent for an instant).
            #
            # Only when exactly ONE existing file holds the slot. Two already there
            # (S13E05 held both cuts for a month) is ambiguous -- replacing one would
            # leave the duplicate standing -- so that still drops and reports.
            if (str(f.get("dst_rel", "")).startswith(config.ONE_PACE_PREFIX)
                    and len(collisions) == 1):
                existing = collisions[0]
                old_rel = f.get("dst_rel")
                f["dst_rel"] = str(Path(*rel.parts[:3]) / existing.name)
                if f.get("_dst_abs"):
                    f["_dst_abs"] = str(existing)
                kept.append(f)
                print(f"[validate_plan] One Pace re-cut retargeted onto the episode it "
                      f"replaces at S{key[0]:02d}E{key[1]:02d}: {old_rel} -> "
                      f"{f['dst_rel']}", flush=True)
                continue
            # IDENTITY BEFORE DUPLICATION. A differently-named file at this number is
            # only a duplicate when it is the SAME episode. The journal records the
            # source title each destination was filed from, so when both sides are
            # known and clearly different, the existing file is a wrong-slot copy and
            # dropping the planned file would cement the misplacement. Park the release
            # instead: the repair is a re-file, never a silent choice between contents.
            # `CollisionPark` (not PlanError) lets the chunked per-file caller park the
            # release's bytes instead of freeing them (HANDOFF 10.2).
            mismatch = _existing_episode_mismatch(f, rel, collisions)
            if mismatch:
                raise CollisionPark(mismatch)
            dropped.append(f)
            print(f"[validate_plan] dropped duplicate of an existing episode "
                  f"S{key[0]:02d}E{key[1]:02d} (a differently-named file with that "
                  f"number is already on disk): {f.get('dst_rel')}", flush=True)
        else:
            kept.append(f)
    return kept, dropped


def _tmdb_id_for_show(plan, show_folder):
    """The TMDB id Jellyfin will scrape this show with: the plan's, else the pinned one.

    `tvshow.nfo` is what Jellyfin actually obeys, so a plan that omits `tmdb_id` for a show
    the library already holds is still filed against the pinned id -- which is why the
    existing folder is consulted rather than trusted to the plan alone.
    """
    tid = plan.get("tmdb_id")
    if tid:
        return tid
    try:
        nfo = config.SHOWS_ROOT / show_folder / "tvshow.nfo"
        text = nfo.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r"<tmdbid>\s*(\d+)\s*</tmdbid>", text)
    return m.group(1) if m else None


def _reject_arc_split_across_seasons(files):
    """Fail closed on ONE release arc torn between two numbered seasons.

    THE FAILURE THIS CATCHES, measured 2026-09-12 on the third run of
    `[MTBB] Monogatari Series (BD 1080p)`. Season 03 held 13 files from
    `Owarimonogatari S1`, 6 from `Owarimonogatari S2`, 3 from `Tsukimonogatari` and 1
    from `Nekomonogatari (Black)`. Season 05 held all of `Koyomimonogatari` and all of
    `Hanamonogatari`. Season 06 held all of `Otorimonogatari` and one file of
    `Tsukimonogatari`. Every one of those 78 episodes resolved to a real title and a real
    plot -- another arc's.

    `_reject_absolute_run_split` cannot see this: it looks at the PLANNED numbers, and
    these were perfectly well-formed, each season starting at 1. The tell is on the other
    side of the mapping, in the SOURCE filenames: `Tsukimonogatari - 01..04` is one thing
    the release is asserting about itself, and a plan that sends 03 to one season and 04
    to another has torn it in half.

    WHY "NON-CONSECUTIVE FOLDERS" IS THE WRONG RULE, and it took a census to see it. The
    obvious version of this check -- refuse a season drawing on folders that are not
    consecutive in the release -- rejects the CORRECT answer here. Monogatari's Season 03
    is folders 05, 06, 08, 09 and 10; folder 07 is `Hanamonogatari`, which aired a year
    later and belongs to no season at all. The release orders its folders by the novels,
    not by broadcast. So the unit is not the folder. It is the filename LABEL: all 23 of
    those files are named `Monogatari Series Second Season - 01..23` across five folders,
    and THAT is what must not be torn. See `arcmap.units`.

    SEASON 00 IS EXEMPT, and deliberately. Splitting an arc between a season and the
    show's specials is normal and often right -- a provider that carries a season of 12
    against a release's 15 is telling you the last three are specials. What is never right
    is one arc in two NUMBERED seasons.

    Replayed over every plan in the journal that carries a file list before it shipped:
    it rejects nothing that was ever correct work. A plan whose sources do not parse into
    arcs at all (most of the library -- ordinary `S01E04` filenames) is untouched, because
    an unparseable source belongs to no unit.
    """
    try:
        import arcmap
    except Exception:                                                  # noqa: BLE001
        return
    srcs = [str(f.get("src") or "") for f in files
            if f.get("type") == "episode" and f.get("src")]
    if len(srcs) < 2:
        return
    try:
        us = arcmap.units([Path(s).name for s in srcs])
    except Exception:                                                  # noqa: BLE001
        return
    if not us:
        return

    # {label: {season: [basename, ...]}} over the NUMBERED seasons only.
    torn = {}
    for f in files:
        if f.get("type") != "episode" or not f.get("src"):
            continue
        try:
            season = int(f.get("season"))
        except (TypeError, ValueError):
            continue
        if season <= 0:
            continue                       # specials are numbered by their own rules
        unit = arcmap.unit_of_source(us, f["src"])
        if unit is None or unit.size < 2:
            continue
        torn.setdefault(unit.label, {}).setdefault(season, []).append(Path(f["src"]).name)

    for label, by_season in sorted(torn.items()):
        if len(by_season) < 2:
            continue
        where = "; ".join(
            f"Season {s:02d} gets {len(by_season[s])} "
            f"({', '.join(sorted(by_season[s])[:2])}"
            f"{', ...' if len(by_season[s]) > 2 else ''})"
            for s in sorted(by_season))
        raise PlanError(
            f"one arc split across two seasons: the release names "
            f"{len([n for v in by_season.values() for n in v])} of these files "
            f"{label!r} -- one arc, numbered straight through -- and the plan splits it: "
            f"{where}. An arc belongs to ONE season. Either every file the release labels "
            f"{label!r} goes to the same season, or the ones that do not belong to a "
            f"season at all go to Season 00 as specials. Re-check which season this arc "
            f"belongs to against the provider's season sizes (the harness computed that "
            f"mapping for you in the prompt) before re-filing.")


def _reject_season_over_provider_count(plan, files):
    """Fail closed on a season filled with MORE episodes than the show has.

    The companion to `_reject_arc_split_across_seasons`, and it catches what that one
    cannot: two WHOLE arcs stacked into one season. Monogatari's Season 05 held all 12
    files of `Koyomimonogatari` and all 5 of `Hanamonogatari` -- neither arc torn, 17
    episodes in a season the provider says has 6.

    Counts what is ALREADY ON DISK too, because this pack is filed a wave at a time and
    the mixing happened across waves as much as within one. A wave that would make a
    season overflow is refused even when the wave alone looks fine.

    Fails soft and fails OPEN in every direction a provider can be wrong:
      * no provider entry for this show, or none for this season -> nothing to say. The
        guide is frequently behind on anime, where each cour is often a separate entry.
      * the RELEASE's own filenames claim this season and claim this many episodes ->
        believe the release. The same escape `_reject_season_gap` grants, for the same
        reason: a fan re-cut like One Pace carries its own arc numbering and the provider
        has never heard of it.
      * Season 00 -> exempt. A show may hold any number of specials.
    """
    shape = None
    planned = {}
    for f in files:
        if f.get("type") != "episode":
            continue
        try:
            season = int(f.get("season"))
        except (TypeError, ValueError):
            continue
        if season <= 0:
            continue
        dst = Path(str(f.get("_dst_abs") or ""))
        rel = _media_rel(dst)
        if rel is None or len(rel.parts) < 3 or rel.parts[0] != "Shows":
            continue
        planned.setdefault(rel.parts[1], {}).setdefault(season, set()).add(
            str(f.get("_dst_abs")))

    for folder, seasons in sorted(planned.items()):
        shape = _provider_season_shape(folder)
        if not shape:
            continue
        for season in sorted(seasons):
            allowed = shape.get(season)
            if not allowed:
                continue
            on_disk = _existing_season_episode_count(folder, season, seasons[season])
            total = len(seasons[season]) + on_disk
            if total <= allowed:
                continue
            # The release may simply be a show the guide is behind on. If the SOURCE
            # filenames themselves number this season past the provider's count, the
            # provider is the one that is wrong.
            said = 0
            for f in files:
                if f.get("type") != "episode":
                    continue
                m = re.search(r"[Ss](\d{1,2})[Ee](\d{1,3})", Path(str(f.get("src") or "")).name)
                if m and int(m.group(1)) == season:
                    said = max(said, int(m.group(2)))
            if said > allowed:
                continue
            raise PlanError(
                f"season over-filled: the plan puts {len(seasons[season])} file(s) into "
                f"'{folder}' Season {season:02d}, which already holds {on_disk} -- "
                f"{total} in a season the episode guide says has {allowed}. A season that "
                f"overflows is two different arcs stacked into one season number, and "
                f"every episode in it will scrape another arc's title. Re-check which "
                f"season each arc belongs to (the harness computed that mapping in the "
                f"prompt) and file the overflow where it belongs -- Season 00 if it is a "
                f"special, its own season if the provider has one.")


def _existing_season_episode_count(folder, season, writing=()):
    """How many episode videos already sit in `folder`'s Season `season`, excluding any
    path this plan is about to write (a re-file of a file already there is not a new one).

    Reads the SSD (`config.SHOWS_ROOT`), never the mount, and counts video files only --
    a `.nfo`/poster beside an evicted video must not read as a second episode.
    """
    d = config.SHOWS_ROOT / folder / f"Season {season:02d}"
    if not d.is_dir():
        return 0
    writing = {str(w) for w in writing}
    n = 0
    try:
        for p in d.iterdir():
            if (p.is_file() and p.suffix.lower() in config.VIDEO_EXTENSIONS
                    and str(p) not in writing):
                n += 1
    except OSError:
        return 0
    return n


def _reject_absolute_run_split(files):
    """Fail closed on ONE continuous episode run chopped across several season folders.

    Season-relative numbering restarts near 1 every season, so seasons overlapping each
    other's episode ranges is the NORMAL case and proves nothing. What is not normal is
    CHAINING: season N+1's first episode being exactly season N's last plus one, over and
    over. That is a single absolute run -- 1..26 for the whole show -- sliced into season
    folders, and it is what a model produces when it knows a franchise has several arcs
    but cannot map arcs onto provider seasons.

    WHY IT MATTERS TO THE OWNER. Jellyfin numbers within a season. An absolute run split
    this way gives every season after the first a first episode that is not 1, and any
    off-by-one puts an episode in the wrong folder entirely -- where it reads as a gap in
    one season and a stray in the next. Measured 2026-09-10 on
    `[MTBB] Monogatari Series (BD 1080p)`, the hardest naming case in the library: the
    single 26-episode arc "Monogatari Series Second Season" was spread over Seasons 04,
    05, 07, 08, 09 and 10 as episodes 1-23, and Season 09 ended up holding 18,19,20,21,23
    while Season 10 held only 22. That is a permanent hole in the season the owner
    actually watches, authored confidently, with correct titles and plots on every file.

    THE HARNESS DISPOSES (§4.4). Nothing here can make a free model understand a
    fourteen-arc franchise. What it can do is refuse an answer that is internally
    incoherent, so the pack lands UNFILED and visible instead of misfiled and silent --
    the cheap failure rather than the expensive one.

    Deliberately narrow, and measured before it shipped: over all 783 completed plans in
    the journal that carry a file list, this rejects ZERO. It needs three or more seasons
    and at least two chained pairs, so a two-season show, a specials folder, and an
    ordinary absolute-numbered single season all pass untouched. Season 00 is exempt --
    specials are numbered by their own rules.
    """
    per: dict = {}
    for f in files:
        rel = f.get("dst_rel") or ""
        parts = Path(rel).parts
        if len(parts) < 2 or parts[0] != "Shows":
            continue
        m = re.search(r"S(\d{1,3})E(\d{1,4})", parts[-1])
        if not m:
            continue
        season, episode = int(m.group(1)), int(m.group(2))
        if season <= 0:                       # specials play by their own rules
            continue
        per.setdefault(parts[1], {}).setdefault(season, set()).add(episode)

    # MERGE IN WHAT IS ALREADY ON DISK for each show this plan touches. A chunked pack is
    # filed a wave at a time, and a run split across seasons can therefore be split across
    # WAVES too -- seasons 4 and 5 in one plan, 7, 8 and 9 in the next, each individually
    # too small to trip the test. Judging the plan against the show's whole layout closes
    # that. Measured 2026-09-10 before shipping: of 295 show folders on disk, ZERO already
    # look chained, so this adds detection without refusing anything the library holds.
    for folder in list(per):
        root = config.MEDIA_ROOT / "Shows" / folder
        if not root.is_dir():
            continue
        try:
            season_dirs = [d for d in root.iterdir() if d.is_dir()]
        except OSError:
            continue
        for sd in season_dirs:
            try:
                entries = list(sd.iterdir())
            except OSError:
                continue
            for e in entries:
                if e.suffix.lower() not in config.VIDEO_EXTENSIONS:
                    continue
                m = re.search(r"S(\d{1,3})E(\d{1,4})", e.name)
                if not m:
                    continue
                season = int(m.group(1))
                if season > 0:
                    per[folder].setdefault(season, set()).add(int(m.group(2)))

    for folder, seasons in per.items():
        ordered = sorted(seasons)
        if len(ordered) < 3:
            continue
        chained = [(a, b) for a, b in zip(ordered, ordered[1:])
                   if seasons[a] and seasons[b]
                   and min(seasons[b]) == max(seasons[a]) + 1]
        if len(chained) >= 2:
            shape = ", ".join(f"S{s:02d}={min(seasons[s])}-{max(seasons[s])}"
                              for s in ordered)
            pairs = ", ".join(f"S{a:02d}->S{b:02d}" for a, b in chained)
            raise PlanError(
                f"{folder}: one continuous episode run has been split across season "
                f"folders ({shape}); seasons chain at {pairs}. Season-relative numbering "
                f"restarts near 1 each season, so this is absolute numbering filed as if "
                f"it were per-season -- it leaves holes in the seasons the owner watches. "
                f"Re-identify with each arc mapped to its own season, numbered from 1.")


def _series_from_comic_filename(name):
    """The series a comic filename states, with its volume/chapter marker removed.

    `One Piece v112.cbz` -> `One Piece`; `ElfQuest - The Final Quest (2026).cbr` ->
    `ElfQuest - The Final Quest` (no marker to remove). Used only to tell a flat
    master's OWN run from an unrecognised member's file."""
    stem = Path(str(name)).stem
    stem = re.sub(r"\b(?:v|vol|volume)\.?\s*\d{1,4}\b", " ", stem, flags=re.IGNORECASE)
    stem = re.sub(r"\b(?:c|ch|chapter|chap)\.?\s*\d{1,4}\b", " ", stem,
                  flags=re.IGNORECASE)
    return strip_edition_label(re.sub(r"\s+", " ", stem).strip(" -_"))


def _flat_dir_has_comics(base):
    """True when a folder holds comic archives directly (the pre-franchise layout)."""
    try:
        return any(p.is_file() and p.suffix.lower() in config.COMIC_EXTENSIONS
                   for p in Path(base).iterdir())
    except OSError:
        return False


def _reject_comic_at_franchise_root(files):
    """Fail closed when a comic is filed directly into a franchise MASTER folder.

    A franchise master (`config.COMIC_FRANCHISES`) is a container of named series --
    `Comics/ElfQuest/The Original Quest/`, `.../The Final Quest/`, `.../Stargazer's Hunt/`.
    It holds sub-folders, never files. The moment it holds files it becomes a numbered
    `<Master> vNN` namespace that belongs to nothing in particular, and everything the
    table failed to recognise gets dropped into it on top of everything else already
    there.

    That is not a tidiness argument; it is where the owner's "ElfQuest comics have
    repeats" came from, and the repeats were not repeats. Measured off `direct_ingest.log`:
    `ElfQuest - The Final Quest (2026).cbr` was filed as `ElfQuest v04.cbz`, then as
    `ElfQuest v01.cbr`, then as `ElfQuest v01.cbz` -- three numbers for one work on three
    passes -- while `An_ElfQuest_Story_-_A_Gift_of_Her_Own.cbr`, a standalone story with no
    volume at all, became `ElfQuest v06` and later `ElfQuest v10`. The names collided, and
    two entirely DIFFERENT books ended up sharing the slot `ElfQuest v01`. A
    format-precedence dedupe over those names would have deleted one of them believing it
    was a lesser copy of the other (which is why §4.90(B) was suspended).

    The rule is deliberately structural rather than a numbering heuristic. Numbering
    heuristics were tried first and measured against all 1510 historical comic filings:
    "a source with no volume number may not be filed as vNN" rejected 52 of them, and 45
    were correct -- a one-volume standalone like `Uzumaki (Deluxe Edition)` is legitimately
    `Uzumaki v01` in its own folder. What separates that from the ElfQuest mess is not the
    number, it is WHOSE namespace the number is in. So the rule is about the namespace.
    """
    for idx, f in enumerate(files):
        dst_p = Path(f.get("dst_rel") or "")
        if dst_p.parts[:1] != ("Comics",):
            continue
        parent = dst_p.parent
        for fr in config.COMIC_FRANCHISES:
            root_rel = ("Manga/" if fr["kind"] == "manga" else "") + fr["name"]
            if normalize_folder_name(str(parent)) != normalize_folder_name("Comics/" + root_rel):
                continue
            # A FLAT master is a legitimate layout until the franchise migration runs:
            # the master series' own run lives directly in the master folder (One
            # Piece's 322 files do today). The rule still refuses everything else --
            # an unrecognised `ElfQuest - The Final Quest` in the ElfQuest root invents
            # the shared vNN namespace that caused the 2026-09-02 incident -- by
            # allowing ONLY the file whose series name IS the master and only when the
            # master folder already holds flat archives.
            #
            # The SOURCE name is the evidence, and a source that names only a chapter
            # (`Chapter 1133.zip`) carries none -- that is the normal shape of a
            # chapter-only drop, not a franchise-root filing. Only then does the
            # DESTINATION name supply the series; a source that states a DIFFERENT
            # series (the ElfQuest incident) is still refused.
            src_name = Path(f.get("src") or "").name or dst_p.name
            own = normalize_folder_name(_series_from_comic_filename(src_name))
            if not own:
                own = normalize_folder_name(_series_from_comic_filename(dst_p.name))
            if own == normalize_folder_name(fr["name"]) and any(
                    _flat_dir_has_comics(root / root_rel)
                    for root in (config.COMICS_ROOT, config.MEDIA_ROOT,
                                 config.MEDIAFS_MOUNT)):
                continue
            members = sorted({v for v in (fr.get("members") or {}).values() if v})
            raise PlanError(
                f"file[{idx}] '{Path(f.get('src') or '').name}' is filed directly into the "
                f"franchise folder Comics/{root_rel}/, which holds SUB-FOLDERS only -- one "
                f"per series ({', '.join(members)}). Filing a file there puts it in a "
                f"shared '{fr['name']} vNN' namespace that belongs to no series, where it "
                f"collides with unrelated books. Put it in the sub-folder for the series "
                f"it actually is, creating a new one if this series has none yet.")


def _reject_franchise_member_outside_master(files):
    """Refuse a comic the franchise table knows but the plan files outside its master.

    THE FAULT (owner report 2026-10-03): `Citrus` and `Citrus+` were filed as two
    top-level folders while `Akame ga KILL!` + ZERO were correctly nested. The Citrus
    row was missing from the table; nothing else refused a top-level home for a series
    the table DOES know. This is the backstop that makes table rows binding at plan
    time: the model proposes, the harness disposes.

    Narrow by design, like `_reject_release_identity`. It fires only when the series is
    computable -- the source filename names a franchise member, or the destination's own
    series folder does -- and the destination is not inside that franchise's master at
    all. It says nothing about WHICH member sub-folder (the digest lists the live names
    and `resolve_comic_folder` resolves them), and it fails open when no series can be
    derived (a bare `Chapter 1133.zip` against an unrecognised folder is untouched).
    """
    for idx, f in enumerate(files):
        dst_p = Path(f.get("dst_rel") or "")
        if dst_p.parts[:1] != ("Comics",):
            continue
        kind = "manga" if dst_p.parts[1:2] == ("Manga",) else "comic"
        src_name = Path(f.get("src") or "").name
        candidates = [_series_from_comic_filename(src_name)] if src_name else []
        if len(dst_p.parts) >= 3:
            candidates.append(dst_p.parts[-2])      # the series folder the plan created
        hit = None
        for cand in candidates:
            if cand:
                hit = comic_franchise(cand, kind)
                if hit is not None:
                    break
        if hit is None:
            continue
        fr, sub = hit
        root_rel = Path("Comics") / (("Manga/" if fr["kind"] == "manga" else "")
                                     + fr["name"])
        if root_rel in dst_p.parents or dst_p.parent == root_rel:
            continue                                # already inside the master
        expected = f"{root_rel}/{sub}/" if sub else f"{root_rel}/<series>/"
        raise PlanError(
            f"file[{idx}] '{src_name or dst_p.name}' belongs to the franchise "
            f"'{fr['name']}' but the plan files it at {f.get('dst_rel')!r}, outside its "
            f"master folder. File it under {expected} -- the digest's COMIC/MANGA "
            f"FRANCHISES block lists the sub-folder the series lives in.")


# A filename stem that is ONLY a marker (`c1151`, `v001`, `d1078`, `c1151.5`): no
# series name, so nothing on the shelf or in the reader can place it. Refused at plan
# time (10.5c follow-up, 2026-09-20) and purged as a duplicate by the reconciler.
_BARE_MARKER_STEM = re.compile(
    r"^(?:c|ch|chapter|v|vol|volume|d)\.?\s*\d{1,5}(?:[.\-]\d{1,4})?$", re.IGNORECASE)


def _comic_marker(name):
    """`("volume"|"chapter", number)` a comic filename claims, or None."""
    m = re.search(r"\bv\.?\s*(\d{1,4})\b", name, re.IGNORECASE)
    if m:
        return "volume", int(m.group(1))
    m = re.search(r"\bc\.?\s*(\d{1,4})\b", name, re.IGNORECASE)
    if m:
        return "chapter", int(m.group(1))
    return None


def _comic_series_candidates(dst_rel):
    """Series-name candidates for a comic destination, deepest folder first.

    Manga nests `Comics/Manga/<Series>/` and `Comics/Manga/<Franchise>/<Series>/`;
    the persisted volume map is keyed by the whole chain under the category
    (`series_label_for_rel`), so both joins are tried.
    """
    parts = Path(dst_rel or "").parts
    if len(parts) < 3 or parts[0] != "Comics":
        return []
    dirs = list(parts[1:-1])
    if dirs and dirs[0] == "Manga":
        dirs = dirs[1:]
    return [" ".join(dirs[i:]) for i in range(len(dirs))]


def _reject_manga_mislabels(plan, files):
    """Refuse a comic whose own archive says it is a chapter filed as a volume, and
    refuse a grey file superseding a coloured one -- both computed, both fail-open.

    HANDOFF 10.0 rows 2-3 / 10.5a/c/d. The model filed `v1078`..`v1176` because the
    library digest showed `v1078` as a volume and nothing could say otherwise: no
    volume ceiling was persisted and no check read the archive. The owner's report --
    "there are not that many volumes" -- becomes a computation here: the persisted
    ceiling (AniList total or the shelf's highest real volume) plus the archive's own
    entries. Above the ceiling AND the entries are chapter pages -> refuse.

    The supersede half enforces the owner rule the other way round: the archive being
    replaced carries colour evidence, and this plan is writing the same number as a
    grey file -> refuse, so a grey copy can never delete the coloured one.

    Everything fails OPEN: an unreadable source, an unknown ceiling, a network-shaped
    absence -> no opinion. A rejection here must be a fact, never a guess.
    """
    try:
        import comicfacts
    except Exception:                                            # noqa: BLE001
        return
    written = []
    for idx, f in enumerate(files):
        dst = Path(f.get("dst_rel") or "")
        if dst.parts[:1] != ("Comics",):
            continue
        marker = _comic_marker(dst.name)
        if marker is None or marker[0] not in ("volume", "chapter"):
            continue
        mtype, number = marker
        # A destination that names ONLY the marker carries no identity. `c1151.cbz`
        # sat beside `One Piece c1151.cbz` (2026-09-20) and the shelf read it as a
        # second, nameless series member: unindexable by the reader, un-purgeable by
        # the reconciler's series grouping, and the owner's complaint starts there.
        if _BARE_MARKER_STEM.match(dst.stem):
            raise PlanError(
                f"file[{idx}] destination {dst.name!r} names only the chapter/volume "
                f"marker and not the series. A comic destination must carry the series "
                f"name (`<Series> c{number:04d}.cbz` / `<Series> v{number:03d}.cbz`); a "
                f"bare marker is an orphan file the shelf and the reader cannot place.")
        # A chapter number above a FINISHED series' chapter total cannot belong to that
        # series. `Chapter 1093.zip` was filed as `Jujutsu Kaisen c1093.cbz`; Jujutsu
        # Kaisen ended at 271 chapters, so the number itself proves the wrong series
        # (it is One Piece's chapter). ONGOING series are exempt -- their total lags
        # their latest chapter and a bound there would refuse real work.
        if mtype == "chapter":
            for cand in _comic_series_candidates(str(dst)):
                total, finished = comicfacts.chapter_ceiling_for(cand)
                if not finished or not total:
                    continue
                if number > total:
                    raise PlanError(
                        f"file[{idx}] {Path(f.get('src') or '').name!r} is filed as "
                        f"c{number:04d} under {cand!r}, which is a FINISHED series of "
                        f"{total} chapter(s) -- a chapter above the final number cannot "
                        f"be one of its own. This is a different series' chapter (check "
                        f"the number against the series that actually reaches it).")
                break
        facts = None
        try:
            src = f.get("src")
            facts = comicfacts.facts(src, name_hint=dst.name) if src else None
        except Exception:                                        # noqa: BLE001
            facts = None
        if mtype == "volume":
            ceiling = None
            for cand in _comic_series_candidates(str(dst)):
                ceiling = comicfacts.ceiling_for(cand)
                if ceiling:
                    break
            chapter_evidence = bool(facts and facts.get("kind") == "chapter"
                                    and facts.get("volume") is None)
            if ceiling and number > ceiling and chapter_evidence:
                raise PlanError(
                    f"file[{idx}] {Path(f.get('src') or '').name!r} is filed as "
                    f"v{number}, but this series has {ceiling} volume(s) and the "
                    f"archive's own entries are chapter pages (chapter "
                    f"{(facts.get('chapters') or ['?'])[0]}). There is no volume "
                    f"{number}: file it as c{number}. A bare number above the ceiling "
                    f"is a chapter, never the next volume.")
        colored = None
        if facts and facts.get("colored") is not None:
            colored = bool(facts["colored"])
        written.append((mtype, number, colored))

    for rel in (plan.get("supersedes") or []):
        marker = _comic_marker(Path(str(rel)).name)
        if not marker or marker[0] not in ("volume", "chapter"):
            continue
        try:
            replaced = None
            for root in (config.MEDIAFS_MOUNT, config.MEDIA_ROOT):
                p = root / str(rel)
                if p.exists():
                    replaced = comicfacts.colour(p, name_hint=Path(str(rel)).name)
                    break
        except Exception:                                        # noqa: BLE001
            replaced = None
        if replaced is not True:
            continue                    # no proof it is coloured -> fail open
        for mtype, number, colored in written:
            if (mtype, number) == marker and colored is not True:
                raise PlanError(
                    f"the plan supersedes the COLOURED file {rel!r} with a "
                    f"same-numbered non-coloured copy. The owner's rule is that the "
                    f"coloured copy is kept and the grey one is superseded -- never "
                    f"the reverse. Drop the grey candidate, or supersede it with a "
                    f"coloured copy.")


def _reject_same_episode(plan, files):
    """Refuse a plan that puts two distinct video files on one episode of one show.

    One episode number resolves to one episode in Jellyfin, so a second file sharing the
    number is either a duplicate or a misnumbering -- and in the 2026-09-15 Doctor Who
    (1963) case it was the latter: six parts of The Keys of Marinus all filed as `S01E05`,
    because the model copied the release's SERIAL number instead of numbering the parts
    consecutively the way the library already numbered An Unearthly Child's four parts
    (E01-E04). Every later wave then shifted to fit around the collision, corrupting the
    whole season. The refusal message pushes the next provider to the consecutive scheme.

    Keyed by SHOW, not just season+episode: a multi-show pack (Steins;Gate + Steins;Gate 0)
    legitimately files the same episode number into two different show folders. The replay
    over the journal's 820 accepted plans rejected zero of them keyed this way.

    Two exemptions, both from accepted library content rather than theory:

      * SEASON 00 -- a special split across `- part1.mkv`/`- part2.mkv` is a real shape in
        this library (Kaguya-sama S00E06); the serial collapse this stops was in a regular
        season, and every multi-part REGULAR episode already in the library (The Office,
        The Bad Batch, Star Wars Rebels) is numbered consecutively.
      * no numeric season/episode on the entry -- nothing to compare.

    Deliberately not keyed on `episode_title` or `(1)`/`(2)` markers: the Doctor Who parts
    and a legitimately-split special carry the same shape, and only the library's
    established scheme tells them apart. That is precisely what the refusal asks for.
    """
    seen = {}
    for f in files:
        rel = Path(f.get("dst_rel") or "")
        if len(rel.parts) < 2 or rel.parts[0] != "Shows" \
                or rel.suffix.lower() not in config.VIDEO_EXTENSIONS:
            continue
        try:
            season, episode = int(f.get("season")), int(f.get("episode"))
        except (TypeError, ValueError):
            continue
        if season < 1:
            continue
        key = (rel.parts[1], season, episode)
        prev = seen.get(key)
        if prev:
            raise PlanError(
                f"two video files both claim S{season:02d}E{episode:02d} of "
                f"{rel.parts[1]!r}: {prev!r} and {rel.name!r}. One episode number resolves "
                f"to one episode: if these are consecutive PARTS of a story/serial, give "
                f"each part its OWN consecutive episode number, continuing the library's "
                f"existing run (its other multi-parters are numbered that way); if one is "
                f"an alternate cut, drop it or give it its own destination. Never repeat "
                f"one episode number across a regular season.")
        seen[key] = rel.name


def _reject_season_gap(plan, files, sibling_seasons=None):
    """Fail closed on a drop filed under the WRONG season number.

    Three checks, in descending order of how much they know:

      1. THE CEILING (hard, and it runs for every plan including an owned one). A show
         that has aired four seasons cannot receive a Season 05. That is a fact from the
         provider, not a numbering convention, so no plan may override it. This is the
         check that was missing when a pack whose own files are named
         `Dawn.of.the.Croods.S04E01E02-...` was filed into `Season 05` (§4.91).
      2. THE SOURCE'S OWN NUMBERING (hard, also for an owned plan). When the release's
         filenames name exactly one season, and that season is itself within the
         provider's ceiling, a plan that files them under a DIFFERENT season is wrong --
         the §4.49/§4.54 class, where a Season 03 pack was filed as Season 02, the real
         Season 02 pack then found every episode "already present", applied 0 files and
         completed with its download freed. Content was lost, not merely misplaced.
      3. THE GAP HEURISTIC (soft, and still skipped for an owned plan). A new season
         whose immediate predecessor exists neither on disk nor in the plan is probably
         the parts-vs-aired-seasons mis-numbering: a Netflix "Part 5" of ~10 episodes
         filed as `Season 05` over a library whose Seasons 01-02 hold two Parts each.
         TheTVDB resolves nothing at Season 5 and every episode ships blank --
         Disenchantment, exactly.

    WHY THE CEILING IS WHAT SEPARATES 2 FROM 3. Check 2 and the Parts case look identical
    from the plan's side -- in both, the source names a season the plan did not use. They
    are told apart by WHOSE number is impossible. In the Parts case the SOURCE's "S05"
    exceeds a 3-season provider ceiling, so the source is the unreliable one and the
    plan's remap to Season 03 is correct and must be allowed. In the §4.49 case the
    source's "S03" sits comfortably inside the ceiling, so it is the PLAN that invented a
    number. Check 2 therefore only fires when the source's own season is plausible.
    Without the ceiling these two cannot be distinguished and a hard rule would break one
    of them; with it, both are decided from evidence.

    WHY `owned` NO LONGER SKIPS EVERYTHING. Marking a plan `owned` means it authors real
    titles and plots, so an un-resolvable NUMBERING cannot silently blank the episodes --
    that is a good reason to waive the heuristic in check 3, and it stays waived. It was
    never a reason to waive a FACT. `owned: True` is exactly how the Dawn of the Croods
    S04 pack walked past a guard that would otherwise have contradicted it on its own
    filenames.

    `sibling_seasons` closes check 3's one structural blind spot: a chunked torrent is
    filed a WAVE at a time, so a season-pack's seasons land in disk-budget order and the
    library is legitimately full of holes mid-pack. A wave carrying S04 over a library
    holding only S01 and S06 is not the Disenchantment mistake -- the intervening seasons
    are still in the torrent, waiting for a wave. So the caller passes the seasons the
    SOURCE itself advertises, and they count as known alongside the on-disk and in-plan
    ones.
    """
    sibling = {int(s) for s in (sibling_seasons or ()) if int(s) > 0}
    # Planned content seasons per existing show folder.
    planned = {}                                     # folder name -> set of seasons
    srcs = {}                                        # folder name -> [source basenames]
    for f in files:
        dst_rel = f.get("dst_rel")
        parts = Path(dst_rel or "").parts
        if len(parts) < 2 or parts[0] != "Shows":
            continue
        season = f.get("season")
        try:
            season = int(season) if season is not None else _season_of(dst_rel)
        except (TypeError, ValueError):
            season = _season_of(dst_rel)
        if season is None or season <= 0:
            continue
        planned.setdefault(parts[1], set()).add(season)
        srcs.setdefault(parts[1], []).append(
            (Path(f.get("src") or "").name, season, dst_rel))

    # --- check 1: the ceiling. A fact, so it binds an owned plan too -------------
    for folder, planned_seasons in planned.items():
        entries = srcs.get(folder) or []
        ceiling = _provider_max_season(folder)
        if ceiling is None:
            continue
        for n in sorted(planned_seasons):
            if n <= ceiling:
                continue
            said = _source_seasons(entries, n)
            if not said or n <= max(said):
                # The RELEASE ITSELF claims this season (or claims nothing at all), so the
                # provider is merely behind -- which it very often is for anime, where each
                # cour is frequently a separate TVMaze entry, and always is for a fan re-cut
                # like One Pace that carries its own arc numbering. Only the plan inventing
                # a number that NEITHER the provider NOR the release supports is a fault.
                continue
            sample = ", ".join(nm[:44] for nm, sea, _d in entries if sea == n)[:180]
            raise PlanError(
                f"season above the provider ceiling: the plan files '{folder}' into "
                f"Season {n:02d}, but the episode guide lists only {ceiling} season(s) for "
                f"this show AND the source filenames themselves go no higher than Season "
                f"{max(said):02d}. Nothing supports Season {n:02d}, so it does not exist. "
                f"Re-file against the real season numbers (the source files are: {sample}).")

    # --- check 2: the source's own numbering. A fact, so it binds an owned plan too --
    #
    # This check was DOCUMENTED above and never implemented, and the gap was not academic:
    # on 2026-08-31 a `The Croods Family Tree S05` pack was filed into `Season 03` as an
    # owned plan, and Season 03's real content -- which the library did not yet hold -- was
    # left with its slot occupied. That is the §4.49/§4.54 content-loss shape exactly: the
    # genuine Season 03 pack then finds every episode "already present", applies 0 files and
    # completes with its download freed.
    #
    # THREE CONDITIONS, each of which the replay proved necessary. Measured over all 748
    # historical plans, a check without them rejects 46 -- and 44 of those 46 are correct
    # work:
    #
    #   * the source must name exactly ONE season, and it must be a real one. Season 00 is
    #     specials and never counts (a `Carnival Phantasm - S00E06` file says nothing about
    #     where the plan filed it).
    #   * that season must be one the provider ACTUALLY HAS -- `real in shape`, never
    #     `real <= max`. TVMaze numbers Bleach's seasons 2004..2012, so `Bleach.S17E42`
    #     names a season outside the provider's vocabulary entirely; `17 <= 2012` is a
    #     comparison between two different numbering systems and is worth nothing. 22 of
    #     the 46 are this case.
    #   * the source must not be numbering episodes ABSOLUTELY. `Pocket.Monsters.2023.S01E78`
    #     against a 45-episode season 1 is a whole-run number, so the plan's remap to
    #     Season 03 is the right reading. 6 more of the 46 are this case.
    #   * and the plan must have changed ONLY the season, keeping the episode numbers. A
    #     plan that also renumbers is asserting a deliberate mapping -- the library merges
    #     anime cours into one continuous season (Komi's S02E01 -> S01E13) and follows
    #     production order for some cartoons (Powerpuff's S06E01 -> S05E25) -- and both of
    #     those are correct work this check rejected before the condition was added.
    #
    # What survives all three is a release that states a season the provider agrees exists,
    # numbered the way that provider numbers it, filed somewhere else. That is the plan
    # inventing a number, and the two survivors are the two plans already known to be wrong.
    for folder, planned_seasons in planned.items():
        entries = srcs.get(folder) or []
        shape = _provider_season_shape(folder)
        if not shape:
            continue
        for n in sorted(planned_seasons):
            said = {x for x in _source_seasons(entries, n) if x > 0}
            if len(said) != 1:
                continue
            real = said.pop()
            if real == n or real not in shape:
                continue
            if _source_numbers_absolutely(entries, n, real, shape):
                continue
            if not _plan_only_changed_the_season(entries, n, real):
                continue
            sample = ", ".join(nm[:44] for nm, sea, _d in entries if sea == n)[:180]
            raise PlanError(
                f"season-number mismatch: the plan files '{folder}' into Season {n:02d}, but "
                f"the SOURCE FILENAMES name Season {real:02d} -- and the episode guide lists "
                f"Season {real:02d} as a real season of this show ({shape[real]} episodes), so "
                f"the release's own numbering is the one to trust. Filing it elsewhere leaves "
                f"Season {real:02d} missing and Season {n:02d}'s real content with its slot "
                f"taken. Re-file against the source's numbers (the source files are: {sample}).")

    # --- check 3: the gap heuristic ---------------------------------------------
    if plan.get("owned"):
        # An owned plan authors real titles+plots, so an unresolvable season NUMBER
        # cannot blank the episodes -- which is the only harm this heuristic guards
        # against. The two checks above already bound it on the facts.
        return

    for folder, planned_seasons in planned.items():
        show_dir = config.SHOWS_ROOT / folder
        if not show_dir.is_dir():
            continue                                 # fresh show -> not our case
        existing = set()
        for sub in show_dir.iterdir():
            if sub.is_dir() and not sub.name.startswith("."):
                m = re.search(r"(\d+)", sub.name)
                if m and int(m.group(1)) > 0:
                    existing.add(int(m.group(1)))
        if not existing:
            continue
        known = existing | planned_seasons | sibling
        new_seasons = planned_seasons - existing
        for n in sorted(new_seasons):
            if n > 1 and (n - 1) not in known:
                # Before refusing, ASK. A release whose files are named by episode TITLE
                # rather than by number gives the model nothing to number them with, and
                # this guard then turned its guess into a confident wrong answer: a Dawn of
                # the Croods pack of SEASON 3 episodes was filed as Season 02 because
                # Season 02 did not exist on disk yet, and the Season 4 pack was refused
                # outright. `epguide` is the authoritative season/episode list the fleet
                # never had (free, no key), so the gap is now judged against the real show
                # rather than against what happens to be on disk.
                verdict = _season_verdict(folder, srcs.get(folder) or [], n)
                if verdict == "confirmed":
                    continue                 # a REAL missing season, not a mis-numbering
                if isinstance(verdict, str) and verdict.startswith("contradicted:"):
                    raise PlanError(
                        f"season-number mismatch: {verdict[len('contradicted:'):]} "
                        f"Re-file against those numbers.")
                raise PlanError(
                    f"season-number gap: plan files '{folder}' into Season {n:02d} "
                    f"but Season {n - 1:02d} exists neither on disk nor in the plan "
                    f"(existing seasons: {sorted(existing)}). A real show does not "
                    f"skip a season number — this is almost always the parts-vs-"
                    f"aired-seasons mis-numbering (e.g. a Netflix 'Part 5' filed as "
                    f"Season 5 over a library whose Seasons 01-02 hold two Parts "
                    f"each, so it should be the NEXT aired season). Re-check the "
                    f"season number against the library's per-season episode counts "
                    f"and the provider's real season list, or OWN the show with real "
                    f"titles+plots if the numbering genuinely can't resolve."
                )


def existing_show_tmdb_id(series_name):
    """The TMDB id already pinned in an existing show's `tvshow.nfo`, or None.

    This is the ONLY place the harness reliably knows which TMDB entry Jellyfin has
    matched a show to -- `_seed_tvshow_nfo` wrote it, and Jellyfin has been scraping
    against it ever since. Used to ask TMDB which episode slots it can actually render
    (`tmdbguide`), never to decide placement.

    None for a show not on disk yet, which is the right answer: there is no established
    match to disagree with, so nothing needs owning on its account.
    """
    if not series_name:
        return None
    try:
        root = config.SHOWS_ROOT
        if not root.is_dir():
            return None
        want = re.sub(r"[^a-z0-9]+", "", str(series_name).lower())
        for d in root.iterdir():
            if not d.is_dir():
                continue
            have = re.sub(r"[^a-z0-9]+", "", re.sub(r"\s*\(\d{4}\)\s*$", "", d.name).lower())
            if have != want:
                continue
            text = _read_text(d / "tvshow.nfo")
            if not text:
                return None
            m = re.search(r"<tmdbid>\s*(\d+)\s*</tmdbid>", text, re.IGNORECASE)
            return int(m.group(1)) if m else None
    except (OSError, ValueError):
        return None
    return None


def _series_name_of(folder: str) -> str:
    """The bare series name behind a library show-folder name (drops the `(year)`)."""
    return re.sub(r"\s*\(\d{4}\)\s*$", "", folder or "").strip()


def find_show_folder(series_name):
    """The library folder for a show, on the MOUNT first (the complete view), or None.

    YEAR-AWARE: `Doctor Who (2005)` and `Doctor Who (1963)` normalize to one bare name,
    and a first-match lookup returns the wrong series (it did). When the requested name
    states a year, a folder whose own `(year)` differs by more than one is skipped.
    """
    if not series_name:
        return None
    want = normalize_folder_name(_series_name_of(str(series_name)))
    if not want:
        return None
    want_year = _folder_year(str(series_name))
    for root in (config.MEDIAFS_MOUNT / "Shows", config.SHOWS_ROOT):
        try:
            if not root.is_dir():
                continue
            for d in sorted(root.iterdir()):
                if not d.is_dir():
                    continue
                if normalize_folder_name(_series_name_of(d.name)) != want:
                    continue
                have_year = _folder_year(d.name)
                if want_year and have_year and abs(want_year - have_year) > 1:
                    continue
                return d
        except OSError:
            continue
    return None


_SPECIALS_SCHEME_FILE = config.STATE_DIR / "specials_schemes.json"


def specials_scheme(show_folder):
    """`[{slot, title, plot, file}]` for a show's Season-00 episodes, or [].

    THE SLOT IS THE FILENAME'S, NEVER THE `.nfo`'s. A library that owns its specials
    scheme has, by construction, `.nfo`s whose `<episode>` is a FOREIGN provider number:
    Doctor Who (2005)'s S00E04 *The Return Of Doctor Mysterio* carries `<episode>149` and
    its S00E04 companion *The End Of Time (1)* carries `<episode>16` (HANDOFF 15.5).
    Reading the sidecar as the scheme is what made the shelf look self-contradictory.
    The filename's `S00Exx` is the library's own slot; the `.nfo` supplies the title/plot.

    The scheme is PERSISTED to `state/specials_schemes.json` so the free AI can be told
    it as fact (the locked nfos are the authority, but a prompt cannot read the mount).
    Fail open: an unreadable folder is [].
    """
    folder = Path(show_folder) if show_folder else None
    if folder is None or not folder.is_dir():
        folder = find_show_folder(show_folder)
    out = []
    if folder is None or not folder.is_dir():
        return out
    season_dir = folder / "Season 00"
    try:
        entries = sorted(season_dir.iterdir())
    except OSError:
        return out
    for p in entries:
        if not p.is_file() or p.suffix.lower() not in config.VIDEO_EXTENSIONS:
            continue
        m = re.search(r"[Ss]00[Ee](\d{1,3})", p.name)
        if not m:
            continue
        slot = int(m.group(1))
        text = _read_text(episode_nfo_path(p)) or ""
        title = _xml_tag(text, "title")
        plot = _xml_tag(text, "plot")
        out.append({"slot": slot, "title": title or _filename_episode_title(p.name),
                    "plot": plot, "file": p.name})
    out.sort(key=lambda e: e["slot"])
    try:
        blob = {}
        if _SPECIALS_SCHEME_FILE.exists():
            blob = json.loads(_SPECIALS_SCHEME_FILE.read_text(encoding="utf-8")) or {}
        blob[folder.name] = {"computed_at": time.time(), "scheme": out}
        _SPECIALS_SCHEME_FILE.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(_SPECIALS_SCHEME_FILE, json.dumps(blob, indent=1))
    except OSError:
        pass
    return out


def specials_scheme_block(show_folder, limit=14):
    """The computed Season-00 scheme as prompt text, or "" when the show has none.

    Told to the free AI as fact so it stops treating a provider's special number as the
    library's slot (HANDOFF 15.5). Bounded: at most `limit` rows plus a count.
    """
    scheme = specials_scheme(show_folder)
    if len(scheme) < 3:
        return ""
    rows = [f"  E{int(e['slot']):02d} {str(e['title'])[:52]}"
            for e in scheme[:limit]]
    more = (f"  ... and {len(scheme) - limit} more slot(s)\n"
            if len(scheme) > limit else "")
    return ("======================================================================\n"
            "LIBRARY SPECIALS SCHEME -- COMPUTED (Season 00)\n"
            "======================================================================\n"
            "This show's Season-00 shelf is the LIBRARY'S OWN locked, era-ordered\n"
            "scheme; it routinely differs from the provider's special numbers, which\n"
            "are NOT this scheme. The slots already on disk are:\n"
            + "".join(r + "\n" for r in rows) + more
            + "A NEW special is placed by where its air date falls in this scheme --\n"
              "after the last existing special of its era -- NOT at the provider's\n"
              "number. The harness computes that slot; do not invent one.\n")


def _filename_episode_title(name):
    """A best-effort title from an episode filename (`... - S01E02 - Title.ext`)."""
    stem = Path(name).stem
    m = re.search(r"[Ss]\d{1,3}[Ee]\d{1,4}\s*[-–]\s*(.+)$", stem)
    if m:
        return m.group(1).strip()
    m = re.search(r"[Ss]\d{1,3}[Ee]\d{1,4}\s+(.+)$", stem)
    return m.group(1).strip() if m else ""


def _provider_season_shape(folder: str):
    """`{season: episode_count}` from the provider for a show folder, or None.

    Fails soft in every direction: a lookup error, an unknown show or an import failure all
    return None, which leaves every caller with exactly its pre-existing behaviour. A
    metadata lookup must never be able to block an ingest.
    """
    try:
        import epguide
        return epguide.season_shape(_series_name_of(folder))
    except Exception:                                                 # noqa: BLE001
        return None


def _plan_only_changed_the_season(entries, planned_season: int, said: int) -> bool:
    """Whether the plan kept the source's EPISODE numbers and changed only the season.

    This is what separates a season number the plan INVENTED from one it deliberately
    remapped, and the replay is unambiguous that the difference matters. Both look identical
    from the season alone:

        The Croods Family Tree   S05E01 -> Season 03 / S03E01      invented   (§ this entry)
        Komi Can't Communicate   S02E01 -> Season 01 / S01E13      deliberate (cour merge)
        The Powerpuff Girls      S06E01 -> Season 05 / S05E25      deliberate (production order)

    A plan that renumbers the episodes is asserting a mapping -- the library merges anime
    cours into one continuous season, and follows production order rather than broadcast
    order for some western cartoons, and both are established layouts here. A plan that
    copies `E01` across unchanged and writes a different season in front of it has not
    mapped anything; it has relabelled the season, which is exactly the §4.49/§4.54 fault
    where the real season's content then finds its slots taken.

    Requires EVERY file to preserve its number, not any: a guard that rejects real content
    is worse than the bug it fixes, so ambiguity resolves toward allowing the plan.
    """
    checked = 0
    for name, season, dst in entries:
        if season != planned_season or not name:
            continue
        src_m = _SRC_EPISODE_RE.search(name)
        if not src_m:
            # A sidecar (`2_English.srt`) carries no numbering and so says nothing either
            # way. Letting it veto is not caution, it is silence: every subtitle in a pack
            # would disable the check for the whole release, which is exactly what it did
            # to the Croods Family Tree plan this was written for.
            continue
        dst_m = _SRC_EPISODE_RE.search(Path(dst or "").name)
        if not dst_m or int(src_m.group(1)) != int(dst_m.group(1)):
            return False                      # renumbered, or cannot tell -> do not fire
        checked += 1
    return checked > 0


def _source_numbers_absolutely(entries, planned_season: int, said: int, shape: dict) -> bool:
    """Whether the source is numbering EPISODES from the start of the run, not the season.

    A pack of `Pocket.Monsters.2023.S01E78` files is not claiming that season 1 ran to 78
    episodes; it is numbering the whole series from 1 and parking the season at 1. The
    provider's episode count for the season the source names is what tells the two apart --
    78 against a 45-episode season 1 cannot be a season-relative number.

    This is the distinction that keeps check 2 off legitimate work: 6 of the plans it would
    otherwise reject are exactly this remap, and every one of them is correct.
    """
    count = (shape or {}).get(said)
    if not count:
        return False
    for name, season, _dst in entries:
        if season != planned_season or not name:
            continue
        m = _SRC_SEASON_RE.search(name)
        if m and int(m.group(1)) == said:
            ep = _SRC_EPISODE_RE.search(name)
            if ep and int(ep.group(1)) > count:
                return True
    return False


def _provider_max_season(folder: str):
    """The provider's highest aired season for a show folder, or None.

    Fails soft in every direction: a lookup error, an unknown show or an import failure
    all return None, which leaves the caller with exactly its pre-existing behaviour. A
    metadata lookup must never be able to block an ingest.
    """
    try:
        import epguide
        return epguide.max_season(_series_name_of(folder))
    except Exception:                                                 # noqa: BLE001
        return None


def _source_seasons(entries, planned_season: int) -> set:
    """The seasons the SOURCE FILENAMES themselves name for a planned season's files.

    Empty when the release carries no `SxxExx` numbering at all (the by-title case that
    `_epguide_verdict` exists for).
    """
    said = set()
    for name, season, _dst in entries:
        if season != planned_season or not name:
            continue
        m = _SRC_SEASON_RE.search(name)
        if m:
            said.add(int(m.group(1)))
    return said


_SRC_SEASON_RE = re.compile(r"(?<![A-Za-z0-9])S(\d{1,2})E\d{1,3}", re.IGNORECASE)
_SRC_EPISODE_RE = re.compile(r"(?<![A-Za-z0-9])S\d{1,2}E(\d{1,3})", re.IGNORECASE)


def _season_verdict(folder: str, entries, planned_season: int):
    """What the SOURCE ITSELF says about a planned season, or None when it cannot say.

    Two sources, strongest first:

      1. the source FILENAMES' own SxxExx numbering. This is deterministic and
         authoritative -- the release states its season outright. All 24 files of the Dawn
         of the Croods pack were named `Dawn.of.the.Croods.S03E01-...`, i.e. the input was
         never ambiguous, and the gap guard filed them as Season 02 anyway purely because
         Season 02 did not exist on disk yet. A guard must never override the source's own
         explicit numbering;
      2. failing that, the episode-title lookup (`epguide`), for releases named by title
         with no numbers at all.
    """
    explicit = set()
    for name, season, _dst in entries:
        if season != planned_season or not name:
            continue
        m = _SRC_SEASON_RE.search(name)
        if m:
            explicit.add(int(m.group(1)))
    if explicit:
        if explicit == {planned_season}:
            return "confirmed"
        if planned_season not in explicit and len(explicit) == 1:
            real = explicit.pop()
            sample = ", ".join(n[:44] for n, sea, _d in entries
                               if sea == planned_season)[:180]
            return (f"contradicted:the plan files '{folder}' into Season "
                    f"{planned_season:02d}, but the SOURCE FILENAMES name Season "
                    f"{real:02d} -- {sample}.")
        return None
    return _epguide_verdict(folder, entries, planned_season)


def _epguide_verdict(folder: str, entries, planned_season: int):
    """What the authoritative episode list says about a planned season, or None.

    Returns "confirmed" when the source filenames' episode TITLES really do belong to the
    planned season (so a season-number gap is genuine -- the library is simply missing the
    season in between, which happens whenever season packs land out of order), or
    "contradicted: <detail>" when they belong to a DIFFERENT season, or None when the guide
    cannot say. None keeps the caller's existing behaviour, so a lookup failure can never
    block an ingest.
    """
    try:
        import epguide
    except Exception:                                                 # noqa: BLE001
        return None
    series = re.sub(r"\s*\(\d{4}\)\s*$", "", folder).strip()
    resolved = []
    for name, season, _dst in entries:
        if season != planned_season or not name:
            continue
        hit = epguide.locate(series, name)
        if hit:
            resolved.append((hit[0], hit[1], hit[2], name))
    if not resolved:
        return None
    seasons = {r[0] for r in resolved}
    if seasons == {planned_season}:
        return "confirmed"
    if planned_season not in seasons and len(seasons) == 1:
        real = seasons.pop()
        sample = ", ".join(f"{r[3][:40]!r} is S{r[0]:02d}E{r[1]:02d} ({r[2]})"
                           for r in resolved[:3])
        return (f"contradicted:the plan files '{series}' into Season {planned_season:02d}, "
                f"but the episode list says these files are Season {real:02d} -- {sample}.")
    return None


# --- apply (stage -> atomic move) -------------------------------------------

def _overwrites_preexisting(dst_rel):
    """True when a destination under this path may CLOBBER an existing file.

    The sole exception to the "never overwrite" invariant: One Pace re-releases
    ship better cuts of episodes already on disk, so a repeat drop there replaces
    rather than skips (§ config.ONE_PACE_PREFIX). Everything else is write-once —
    EXCEPT the anime quality-upgrade (§ _upgrade_verdict), which apply_plan checks
    separately and is not folded into this predicate.
    """
    return str(dst_rel).startswith(config.ONE_PACE_PREFIX)


def _ffprobe_bin():
    """Absolute path to ffprobe, or None. The daemon runs under launchd with a minimal
    PATH, so we fall back to config.EXTRA_PATH when `ffprobe` is not already resolvable."""
    exe = shutil.which("ffprobe")
    if exe:
        return exe
    for d in config.EXTRA_PATH:
        p = Path(d) / "ffprobe"
        if p.exists():
            return str(p)
    return None


def _height_rank(height: int) -> int:
    """Map a video stream's pixel height to the same resolution tier the searcher
    scores on (2160p=5, 1080p=4, 720p=3, 480p=2, below=1)."""
    if height >= 2160:
        return 5
    if height >= 1080:
        return 4
    if height >= 720:
        return 3
    if height >= 480:
        return 2
    return 1


def _probe_quality(path):
    """(resolution_rank, dual_audio) for a media file, read live with ffprobe.

    Returns None when quality cannot be determined — an unreadable file, no ffprobe,
    or no video stream. `dual_audio` is True only when the file carries at least two
    audio streams with DISTINCT, tagged languages (a dual-audio release's Jpn+Eng). A
    file with un-tagged audio streams is treated as single-audio: conservative, so we
    never "upgrade" into a file whose audio we cannot verify.
    """
    exe = _ffprobe_bin()
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, "-v", "error", "-print_format", "json", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=30, errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    heights: list[int] = []
    langs: set[str] = set()
    for st in data.get("streams") or []:
        ct = st.get("codec_type")
        if ct == "video":
            try:
                h = int(st.get("height") or 0)
            except (TypeError, ValueError):
                h = 0
            if h:
                heights.append(h)
        elif ct == "audio":
            lang = (st.get("tags") or {}).get("language")
            if lang and str(lang).lower() != "und":
                langs.add(str(lang).lower())
    if not heights:
        return None
    return _height_rank(max(heights)), len(langs) >= 2


def _upgrade_verdict(src, dst):
    """Decide what to do when an anime plan's source collides with an existing file.

    Returns one of:
      * "replace"    — the new file is provably better (Pareto: higher resolution or
                       dual audio, losing neither), OR the existing file cannot be read
                       (a corrupt/unknown file, which any readable new file beats).
      * "skip"       — the new file is provably equal-or-worse; keep the existing one.
      * "bad_source" — the NEW file cannot be read (a corrupt download). It must not
                       clobber a good file, and the caller should surface it rather than
                       silently drop it as "already present".
    """
    if src.suffix.lower() not in config.VIDEO_EXTENSIONS:
        return "skip"
    if dst.suffix.lower() not in config.VIDEO_EXTENSIONS:
        return "skip"
    new = _probe_quality(src)
    if new is None:
        return "bad_source"                 # the download is unreadable/corrupt
    old = _probe_quality(dst)
    if old is None:
        return "replace"                    # existing file is unreadable; readable wins
    new_res, new_dual = new
    old_res, old_dual = old
    if (new_res >= old_res and new_dual >= old_dual
            and (new_res > old_res or new_dual > old_dual)):
        return "replace"
    return "skip"


def _queue_replacement(dst_rel):
    """Record a file this daemon deliberately replaced in place (an anime quality
    upgrade) so Media-Syncer re-uploads it over the stale MEGA copy and empties that
    remote's rubbish bin. Without this, Media-Syncer's write-once sync absorbs the
    mtime drift and the pool keeps the old version forever. Best-effort and never
    fatal: a failed record just means the pool copy lags until a future churn."""
    try:
        q = config.MEDIA_SYNCER_REPLACEMENTS_QUEUE
        q.parent.mkdir(parents=True, exist_ok=True)
        with q.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"path": str(dst_rel)}) + "\n")
    except OSError:
        print(f"[apply_plan] could not record replacement {dst_rel}; Media-Syncer "
              f"will not re-upload it.", flush=True)


def _queue_deletion(dst_rel):
    """Record a comic file this daemon deliberately superseded (a volume covering
    chapters we already held, or a colored volume covering a B/W one) so the reaper
    purges its MEGA copy. The path is library-relative, exactly what the reaper's
    `drain_deletions_queue` expects. Best-effort and never fatal."""
    try:
        q = config.MEDIAFS_DELETIONS_QUEUE
        q.parent.mkdir(parents=True, exist_ok=True)
        with q.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"path": str(dst_rel)}) + "\n")
    except OSError:
        print(f"[apply_plan] could not record deletion {dst_rel}; the remote copy "
              f"will not be purged.", flush=True)


def supersede_paths(paths):
    """Delete already-filed comic files locally and queue their remote purge.

    This is the ONE supersede implementation: `apply_plan` Phase 4 calls it for a plan's
    `supersedes`, and `scripts/chapter_volume_reconcile.py` calls it for the chapters a
    cached volume map proves dead. Both need the identical two steps -- an unlink through
    MEDIA_ROOT (missing_ok: the file may already be evicted to the pool) and a
    `_queue_deletion` line, because the reaper is the only thing that purges the MEGA
    copy and it drains that queue. A second copy of this loop is how a deletion becomes
    real locally while the pool keeps the file forever.

    Idempotent: re-running re-queues the same deletion, and the reaper dedupes by path.
    Returns the number of paths acted on. Never raises -- an OSError on one path must
    not stop the rest of the set.
    """
    n = 0
    for sp in paths or ():
        sp_abs = config.MEDIA_ROOT / Path(sp)
        try:
            sp_abs.unlink(missing_ok=True)
        except OSError:
            pass
        _queue_deletion(sp)
        print(f"[supersede] deleted locally + queued remote purge: {sp}", flush=True)
        n += 1
    return n


def _natural_sort_key(s):
    """Split on digit runs so 'page 9' sorts before 'page 10' (lexicographic sort
    would put '10' first). Used to order loose page images into a readable book.
    Every token is a string, with numeric runs zero-padded to a fixed width, so the
    comparison is always type-safe — a folder mixing digit-leading and text-leading
    filenames can never trip an int-vs-str comparison."""
    out = []
    for t in re.split(r"(\d+)", str(s)):
        out.append(f"{int(t):020d}" if t.isdigit() else t.lower())
    return out


def _page_size(p):
    """Size in bytes of a planned source — a file's own size, or for a directory of
    loose page images the sum of every page image under it. Used for the dedup
    tie-break and the plan's bookkeeping; the applied entry records the *archive's*
    real size instead."""
    p = Path(p)
    if p.is_dir():
        try:
            return sum(f.stat().st_size for f in p.rglob("*")
                       if f.is_file() and f.suffix.lower() in config.LOOSE_PAGE_EXTENSIONS)
        except OSError:
            return 0
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _zip_loose_pages(src_dir, dst_cbz):
    """Package a folder of loose scanned pages into a `.cbz` (a plain ZIP of page
    images, in natural page order). Returns the written archive's size in bytes.

    Deterministic: every file under the folder with a `config.LOOSE_PAGE_EXTENSIONS`
    suffix is included, sorted by a digit-aware natural key so page 2 precedes page
    10; non-page clutter (Thumbs.db, release .txt/.nfo, .DS_Store) is excluded. ZIP
    is written STORED (jpg/png are already compressed), so the archive is ~the size
    of its pages. Raises PlanError if the folder holds no page images at all — an
    empty .cbz would be a broken book YACReader shows as blank.
    """
    src = Path(src_dir)
    rels = sorted(
        (p.relative_to(src) for p in src.rglob("*")
         if p.is_file() and p.suffix.lower() in config.LOOSE_PAGE_EXTENSIONS),
        key=lambda r: _natural_sort_key(str(r)),
    )
    if not rels:
        raise PlanError(f"no page images under {src_dir}")
    dst = Path(dst_cbz)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_STORED) as zf:
        for rel in rels:
            zf.write(src / rel, str(rel))
    return dst.stat().st_size


def apply_plan(plan, info_hash):
    """Copy sources into a dot-staging dir on the media volume, then atomically
    move each file into its final place. Returns a list of applied entries
    [{src, dst, size, preexisting?, replaced?}]. Same-volume os.replace makes each
    final appearance atomic, so Media-Syncer never sees a partial file.

    A destination that ALREADY exists is left untouched (never overwritten) and
    recorded with `preexisting: True` — the pre-existing file is treated as the
    correct one and the local download of it is simply dropped at cleanup. This
    is checked live per file, so it covers both a plain re-ingest of already-owned
    media and a partial-apply resume (files placed before a crash are skipped, the
    rest are moved, and the torrent finishes cleanly instead of needing a manual
    touch). A comic `.zip` is renamed to `.cbz` here purely by copying it to the
    `.cbz` destination the plan specifies.

    THE ONE EXCEPTION is One Pace (§ _overwrites_preexisting): a repeat there is a
    newer re-cut, so an existing destination is REPLACED in place — staged first,
    then `os.replace`d over the old file (atomic within the volume, so the episode
    is never absent), and recorded with `replaced: True`. This is gapless and
    size-verified like any moved file, unlike a skipped pre-existing one.

    A second, narrower exception is the ANIME quality upgrade (§ _upgrade_verdict):
    for a plan flagged `anime: true`, an existing destination is replaced in place
    (same gapless staged os.replace) when — and only when — ffprobe proves the new
    file is a strict Pareto improvement (higher resolution or dual audio, losing
    neither), or when the existing file is unreadable (any readable new file wins). A
    same-quality repeat or a trade is still skipped, never clobbered; a new file that
    cannot be read raises PlanError so the corrupt download is surfaced, not dropped.
    Each anime replacement is recorded to Media-Syncer's replacements queue so the
    pool copy is overwritten in place and the old version's rubbish-bin entry emptied.
    """
    staging_base = config.MEDIA_ROOT / config.STAGING_DIRNAME / info_hash
    if staging_base.exists():
        shutil.rmtree(staging_base, ignore_errors=True)
    staging_base.mkdir(parents=True, exist_ok=True)
    # Novels land on the Google Drive File Provider mount, a different volume from the
    # media SSD, so they need their own staging dir (os.replace must stay same-volume to
    # be atomic). E-books are tiny, so this is a trivial, hidden, self-pruned dot-dir.
    # Created lazily (below) — a dead Google Drive must never fail a show/movie/comic
    # plan that has no Novels files.
    novel_staging_base = config.NOVELS_ROOT / config.STAGING_DIRNAME / info_hash

    def _staging_for(dst_abs):
        """(stage_base, stage_rel) for a resolved destination: the media staging dir, or
        the Google Drive staging dir when the destination lives under NOVELS_ROOT."""
        dst_abs = Path(dst_abs)
        try:
            rel = dst_abs.relative_to(config.NOVELS_ROOT.resolve())
            novel_staging_base.mkdir(parents=True, exist_ok=True)
            return novel_staging_base, rel
        except ValueError:
            return staging_base, dst_abs.relative_to(config.MEDIA_ROOT.resolve())

    applied = []
    try:
        # Phase 1: copy each source into staging (hidden under a dot-dir). A
        # destination already in the library is recorded as pre-existing and
        # skipped (never overwritten) — EXCEPT under One Pace, where a repeat is a
        # newer re-cut that must replace the old file, so it is staged like a fresh
        # file and overwritten in Phase 2. The second exception is the anime quality
        # upgrade: an anime plan whose source is provably strictly better (Pareto —
        # higher definition or dual audio, losing neither) also replaces in place.
        staged = []
        anime = bool(plan.get("anime"))
        for f in plan["files"]:
            dst_abs = Path(f["_dst_abs"])
            src = Path(f["_src_abs"])
            may_replace = _overwrites_preexisting(f["dst_rel"])
            anime_upgrade = False
            if dst_abs.exists() and not may_replace:
                if anime:
                    verdict = _upgrade_verdict(src, dst_abs)
                    if verdict == "replace":
                        may_replace = True
                        anime_upgrade = True
                    elif verdict == "bad_source":
                        raise PlanError(
                            f"anime upgrade: downloaded file is unreadable/corrupt "
                            f"(ffprobe failed) and the library already holds a readable "
                            f"copy at {dst_abs}; re-search for a valid release")
                    else:
                        applied.append({"src": None, "dst": str(dst_abs),
                                        "size": f["_src_size"], "preexisting": True})
                        continue
                else:
                    applied.append({"src": None, "dst": str(dst_abs),
                                    "size": f["_src_size"], "preexisting": True})
                    continue
            rel = Path(f["dst_rel"])
            stage_base, stage_rel = _staging_for(dst_abs)
            stage_path = stage_base / stage_rel
            stage_path.parent.mkdir(parents=True, exist_ok=True)
            # A directory src is the loose-page comic case: the folder of scanned
            # page images is zipped into the .cbz destination here (deterministic —
            # the run only named the folder). A file src is the normal copy.
            if src.is_dir():
                size = _zip_loose_pages(src, stage_path)
            else:
                _copy_verified(src, stage_path, f["_src_size"])
                size = f["_src_size"]
            staged.append((stage_path, dst_abs, size, f, may_replace, anime_upgrade))

        # Phase 2: move each staged file into its final library location. Re-check
        # existence right before the move (TOCTOU / a racing writer): if it is now
        # present, skip rather than clobber — still a success, not a failure. A One
        # Pace file (may_replace) instead overwrites: os.replace is atomic within
        # the volume, so the episode is never absent for an instant (gapless), and
        # the entry is flagged `replaced` so it is still size-verified like a move.
        placed = []
        for stage_path, dst_abs, size, f, may_replace, anime_upgrade in staged:
            existed = dst_abs.exists()
            if existed and not may_replace:
                applied.append({"src": None, "dst": str(dst_abs),
                                "size": size, "preexisting": True})
                continue
            dst_abs.parent.mkdir(parents=True, exist_ok=True)
            # Phase 2a: the LOCKED sidecar goes down BEFORE the video it describes.
            #
            # This ordering is the whole fix, and it is not a tidiness preference.
            # The Shows library runs with `SaveLocalMetadata=True` and
            # `EnableRealtimeMonitor=True`, so the instant a video appears Jellyfin
            # may index it — and Jellyfin decides whether an item is LOCKED from the
            # .nfo it finds AT THAT MOMENT. Find no .nfo and it creates the item
            # unlocked, scrapes it, and then writes its OWN .nfo over the path,
            # destroying the `<lockdata>true</lockdata>` we were about to write. The
            # lock is then gone for good: nothing re-reads a sidecar Jellyfin has
            # already replaced.
            #
            # Writing the video first was therefore a race we lost silently, and lost
            # worst exactly where it costs most — Season-0 specials, whose whole
            # reason for being locked is that the provider's Season-0 ordering is
            # unreliable. Measured 2026-09-11: every Season-0 sidecar under Mushi-Shi
            # (2005) and Monogatari Series (2009) was Jellyfin-authored and
            # `lockdata=false`, carrying scraped titles offset from the files they sat
            # beside, though apply_plan had written all three locked.
            #
            # A sidecar with no video next to it is inert to Jellyfin, so landing it
            # early costs nothing. Phase 3 still re-writes them at the end as a
            # backstop for anything clobbered mid-phase.
            _write_locked_episode_nfo(plan, f, dst_abs)
            os.replace(stage_path, dst_abs)     # atomic within the volume (overwrites)
            entry = {"src": str(stage_path), "dst": str(dst_abs), "size": size}
            if existed:
                entry["replaced"] = True
                if anime_upgrade:
                    # The pool still holds the OLD version under this path; tell
                    # Media-Syncer to overwrite it in place (and empty its rubbish bin).
                    _queue_replacement(f["dst_rel"])
            applied.append(entry)
            placed.append(f)

        # Phase 3: locked EPISODE .nfo, re-written as a BACKSTOP. Each one was
        # already written in Phase 2a, before its own video landed, which is what
        # actually wins the race against Jellyfin's realtime monitor. This second
        # pass costs nothing (identical bytes) and restores any sidecar Jellyfin
        # clobbered while the REST of a long multi-file Phase 2 was still running.
        # It cannot by itself repair Jellyfin's DB — an item already created
        # unlocked stays unlocked until something re-reads the sidecar — so it is a
        # backstop, not the guarantee. Only for files we actually placed, so a
        # pre-existing episode keeps its own .nfo untouched. Safe for any plan: the
        # writer only touches show videos, so a mixed plan's movie/comic files are
        # skipped and a comic/movie-only plan writes nothing. It locks two classes:
        # every episode of an OWNED show, AND every Season-0 SPECIAL regardless of
        # the owned flag (specials are always locked — validate_plan guarantees each
        # carries title+plot — because the provider's Season-0 order mis-scrapes an
        # un-owned special, e.g. a separate movie's entry landing on it).
        if placed:
            _write_owned_nfo(plan, placed)
        # Phase 3a: seed an UNLOCKED tvshow.nfo pinning the series' provider ids for
        # EVERY placed show — owned OR un-owned. This is what stops Jellyfin's
        # scraper fuzzy-matching a sequel/spin-off folder onto its parent series and
        # scraping the parent's episodes onto it (the Fairy Tail: 100 Years Quest ->
        # Fairy Tail merge that duplicated the parent's first 25 episodes). An
        # un-owned show with no seed at all leaves the match entirely to Jellyfin's
        # title search, which merges same-family titles. No-op if a tvshow.nfo already
        # exists.
        if placed:
            _seed_tvshow_nfo(plan, placed)
        # Phase 3b: seed an UNLOCKED movie .nfo pinning the TMDB id for any placed
        # movie, so Jellyfin identifies the exact film and never fuzzy-matches it
        # to a sibling in the same collection. No-op when a movie carries no id or
        # already has a .nfo; harmless for show/comic-only plans (no movie files).
        if placed:
            _write_movie_nfo(plan, placed)
        # Phase 4: manga supersede — a new volume that makes previously-filed chapters
        # redundant (or a colored volume that covers a B/W one). The shared
        # `supersede_paths` does the local unlink and the remote-purge queue line; the
        # reaper drains it. Idempotent: re-running a wave re-queues the same deletion,
        # and the reaper dedupes by path.
        supersede_paths(plan.get("supersedes") or [])
    finally:
        shutil.rmtree(staging_base, ignore_errors=True)
        _prune_empty(config.MEDIA_ROOT / config.STAGING_DIRNAME)
        shutil.rmtree(novel_staging_base, ignore_errors=True)
        _prune_empty(config.NOVELS_ROOT / config.STAGING_DIRNAME)

    # Mirror the filed files + superseded chapters into the shared library DB, so the
    # searcher's master manifest matches what actually landed.
    dbhook.record_plan(plan)
    return applied


def _copy_verified(src, dst, expected_size):
    """Materialize `src` at staging path `dst`, then confirm the size.

    `src` is now ALWAYS on the same volume as the staging dir — torrents download
    straight onto the library drive (INCOMING_DIR is on the SSD) — so a plain
    hardlink is used instead of a byte copy: instantaneous, costs no extra disk
    (both names share one inode), and crash-safe (the original src still exists, so
    a resume that wipes the staging dir can re-apply). The subsequent os.replace of
    this staged link into its final place is atomic within the volume, so
    Media-Syncer never sees a partial file. (If `src` were ever cross-device — an
    older record, or a manual move — os.link raises and we fall back to the real
    copy, exactly as before.)
    """
    try:
        os.link(src, dst)                       # same-volume: instant, zero extra space
    except OSError:
        shutil.copy2(src, dst)                  # cross-device (or no-hardlink FS): real copy
    actual = dst.stat().st_size
    if actual != expected_size:
        raise PlanError(f"copy size mismatch for {dst}: {actual} != {expected_size}")


def verify_applied(applied):
    """Confirm every applied file exists at its destination. Files we moved are
    also size-checked against the source; files that already existed are accepted
    as-is (existence is enough — the pre-existing copy is the source of truth and
    may legitimately differ in size), so an all-already-present torrent verifies.
    """
    for entry in applied:
        dst = Path(entry["dst"])
        if not dst.exists():
            return False, f"missing after apply: {dst}"
        if not entry.get("preexisting") and dst.stat().st_size != entry["size"]:
            return False, f"size mismatch after apply: {dst}"
    return True, "ok"


def _prune_empty(path):
    try:
        if path.exists() and not any(path.iterdir()):
            path.rmdir()
    except OSError:
        pass


# --- .nfo generation for owned shows ----------------------------------------

def _locks_episode_nfo(plan, f, dst):
    """Whether this placed file gets a LOCKED episode .nfo, decided PER FILE:
    every episode of an OWNED show, and every Season-0 SPECIAL regardless of the
    owned flag. Anything that is not a show video (a movie half of a mixed
    torrent, a Novels/ e-book, a comic) is skipped."""
    rel = _media_rel(dst)
    if (rel is None or rel.parts[0] != "Shows"
            or dst.suffix.lower() not in config.VIDEO_EXTENSIONS):
        return False
    try:
        is_special = int(f.get("season")) == 0
    except (TypeError, ValueError):
        is_special = False
    # PER-FILE `owned`, falling back to the plan's flag. The owner's instruction
    # (2026-09-12): "let TMDB work for all the individual files it will work for -- only
    # own what is necessary." A whole-plan flag cannot express that. For Monogatari the
    # split is 10 files against 93: TMDB serves Seasons 02 and 03 perfectly, and cannot
    # render S01E13-15, S04E13 or S05E01-06 at all (`tmdbguide`). Owning a file is not
    # free -- a locked sidecar is the fleet's word forever and Jellyfin will never improve
    # on it -- so the narrower the ownership, the better.
    return bool(f.get("owned", plan.get("owned"))) or is_special


def _write_locked_episode_nfo(plan, f, dst):
    """Write one file's locked episode .nfo now, if it gets one. No-op otherwise.

    Called from Phase 2 BEFORE the video is moved into place — see the comment
    there. Returns True if a sidecar was written.
    """
    dst = Path(dst)
    if not _locks_episode_nfo(plan, f, dst):
        return False
    _atomic_write(dst.with_suffix(".nfo"), _episode_nfo_xml(f, plan.get("title", "")))
    return True


def _write_owned_nfo(plan, files=None):
    """Write locked EPISODE .nfo so Jellyfin keeps our per-episode layout instead
    of scraping those episodes. Honors optional airs-before ordering so interleaved
    specials slot into the right watch position without a playlist.

    `files` restricts the write to the episodes we actually placed this run
    (defaults to the whole plan); a pre-existing episode keeps its own .nfo and is
    never overwritten.

    Two classes of episode are locked, decided PER FILE:
      * every episode, when the whole show is OWNED (plan["owned"]); and
      * every Season-0 SPECIAL, ALWAYS — even in an un-owned plan — because the
        provider's Season-0 ordering mis-scrapes an un-owned special (wrong title,
        or a separate movie's entry pulled onto it). validate_plan guarantees each
        Season-0 file carries a title+plot, so locking it can never blank it.
    A non-special episode of an un-owned show is left un-written, so Jellyfin still
    scrapes the main series exactly as before.

    Only episodes are locked here. The UNLOCKED series seed is written separately
    by `_seed_tvshow_nfo` (for owned AND un-owned shows), so Jellyfin still scrapes
    rich series-level metadata but is pinned to the right series id.
    """
    for f in (files if files is not None else plan["files"]):
        # Owned .nfo are a show-only concept: in a mixed torrent, skip any file
        # that isn't a show video (e.g. the movie half of a Steins;Gate torrent, or
        # a Novels/ e-book). Lock owned-show episodes and ALL specials; leave a
        # plain un-owned episode to Jellyfin's scraper.
        _write_locked_episode_nfo(plan, f, Path(f["_dst_abs"]))


def _seed_tvshow_nfo(plan, files=None):
    """Seed an UNLOCKED tvshow.nfo pinning the series' provider ids (TMDB, plus
    TVDB when the plan supplies one) for any placed show, if the folder has none
    yet. This is what makes Jellyfin identify the EXACT series instead of running
    its own title search — the search merges a sequel/spin-off whose name contains
    its parent's (Fairy Tail: 100 Years Quest onto Fairy Tail, scraping the
    parent's episode metadata onto the sequel). Runs for owned AND un-owned shows;
    left unlocked so Jellyfin still scrapes plot/poster/cast.

    When a tvshow.nfo already exists we never clobber it (it is often Jellyfin's own
    richer scraped copy), but we DO fill in a provider id it is missing: an existing
    tvshow.nfo with no `<tmdbid>`/`<tvdbid>` is exactly the hole that lets Jellyfin's
    scraper title-merge a sequel/spin-off onto its parent (the Fairy Tail: 100 Years
    Quest failure), so leaving a half-pinned file untouched would re-open the merge.
    A missing id is ADDED; an id already present (even one that differs) is left
    alone — a genuine id conflict is a placement bug the audit surfaces, not
    something to silently rewrite here."""
    show_folder = None
    for f in (files if files is not None else plan["files"]):
        dst = Path(f["_dst_abs"])
        rel = _media_rel(dst)
        if rel is None or rel.parts[0] != "Shows" or dst.suffix.lower() not in config.VIDEO_EXTENSIONS:
            continue
        show_folder = config.MEDIA_ROOT / rel.parts[0] / rel.parts[1]
        break
    if show_folder is None:
        return
    tmdb_id, tvdb_id = plan.get("tmdb_id"), plan.get("tvdb_id")
    tvshow_nfo = show_folder / "tvshow.nfo"
    if not tvshow_nfo.exists():
        _atomic_write(tvshow_nfo, _tvshow_nfo_xml(
            plan.get("title", ""), plan.get("year"), tmdb_id, tvdb_id))
        return

    # Exists: fill only the provider ids that are absent, preserving the rest.
    try:
        text = tvshow_nfo.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return
    add = []
    if tmdb_id and "<tmdbid>" not in text:
        add.append(f"  <tmdbid>{escape(str(tmdb_id))}</tmdbid>")
        if 'type="tmdb"' not in text:
            add.append(f'  <uniqueid type="tmdb">{escape(str(tmdb_id))}</uniqueid>')
    if tvdb_id and "<tvdbid>" not in text:
        add.append(f"  <tvdbid>{escape(str(tvdb_id))}</tvdbid>")
        if 'type="tvdb"' not in text:
            add.append(f'  <uniqueid type="tvdb" default="true">{escape(str(tvdb_id))}</uniqueid>')
    m = re.search(r"</tvshow>\s*$", text)
    if add and m:
        _atomic_write(tvshow_nfo, text[:m.start()] + "\n".join(add) + "\n" + text[m.start():])


def _episode_nfo_xml(f, show_title):
    season = f.get("season")
    episode = f.get("episode")
    title = f.get("episode_title") or f"Episode {episode}"
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        "<episodedetails>",
        f"  <title>{escape(str(title))}</title>",
        f"  <showtitle>{escape(str(show_title))}</showtitle>",
        f"  <season>{int(season)}</season>",
        f"  <episode>{int(episode)}</episode>",
        "  <lockdata>true</lockdata>",
    ]
    if f.get("airs_before_season") is not None:
        parts.append(f"  <airsbefore_season>{int(f['airs_before_season'])}</airsbefore_season>")
    if f.get("airs_before_episode") is not None:
        parts.append(f"  <airsbefore_episode>{int(f['airs_before_episode'])}</airsbefore_episode>")
    if f.get("airs_after_season") is not None:
        parts.append(f"  <airsafter_season>{int(f['airs_after_season'])}</airsafter_season>")
    if f.get("plot"):
        parts.append(f"  <plot>{escape(str(f['plot']))}</plot>")
    parts.append("</episodedetails>")
    return "\n".join(parts) + "\n"


def _tvshow_nfo_xml(show_title, year, tmdb_id=None, tvdb_id=None):
    """Seed file for the SERIES. Deliberately UNLOCKED (lockdata=false) so
    Jellyfin scrapes the show's plot/poster/cast, with the provider ids pinned
    (when known) so it identifies the right series and never merges two shows.
    Pin the TVDB id too whenever the plan carries one: many anime libraries scrape
    TV via TheTVDB first, and the TMDB id alone does not stop the TVDB agent from
    title-matching a sequel onto its parent. Only the episode .nfo carry
    lockdata=true; the series is never ours to freeze."""
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        "<tvshow>",
        f"  <title>{escape(str(show_title))}</title>",
        f"  <showtitle>{escape(str(show_title))}</showtitle>",
    ]
    if year:
        parts.append(f"  <year>{int(year)}</year>")
    if tmdb_id:
        parts.append(f"  <tmdbid>{escape(str(tmdb_id))}</tmdbid>")
        parts.append(f'  <uniqueid type="tmdb">{escape(str(tmdb_id))}</uniqueid>')
    if tvdb_id:
        parts.append(f"  <tvdbid>{escape(str(tvdb_id))}</tvdbid>")
        parts.append(f'  <uniqueid type="tvdb" default="true">{escape(str(tvdb_id))}</uniqueid>')
    parts.append("  <lockdata>false</lockdata>")
    parts.append("</tvshow>")
    return "\n".join(parts) + "\n"


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# --- .nfo seeding for movies (pins identity so Jellyfin can't mis-match) ------

def _parse_movie_title_year(stem):
    """Split a `Title (YYYY)` movie filename stem into (title, year|None)."""
    m = re.search(r"^(.*?)\s*\((\d{4})\)\s*$", stem)
    if m:
        return m.group(1).strip(), int(m.group(2))
    return stem, None


def _movie_nfo_xml(title, year, tmdb_id, imdb_id=None):
    """Minimal UNLOCKED <movie> seed. The point is the pinned provider id(s):
    Jellyfin identifies the exact film and still scrapes plot/art/cast for it."""
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        "<movie>",
        f"  <title>{escape(str(title))}</title>",
    ]
    if year:
        parts.append(f"  <year>{int(year)}</year>")
    if tmdb_id:
        parts.append(f"  <tmdbid>{escape(str(tmdb_id))}</tmdbid>")
        parts.append(f'  <uniqueid type="tmdb" default="true">{escape(str(tmdb_id))}</uniqueid>')
    if imdb_id:
        parts.append(f"  <imdbid>{escape(str(imdb_id))}</imdbid>")
        parts.append(f'  <uniqueid type="imdb">{escape(str(imdb_id))}</uniqueid>')
    parts.append("  <lockdata>false</lockdata>")
    parts.append("</movie>")
    return "\n".join(parts) + "\n"


def _owned_movie_nfo_xml(title, year, plot, studio=None, premiered=None, genres=()):
    """LOCKED <movie> for a film that exists on NO metadata provider (§ validate_plan's
    owned-movie branch): a YouTube long-form video filed as a standalone film, a fan
    edit, an original work. There is no id to pin, so there is nothing for Jellyfin to
    scrape — and letting it TRY is the whole hazard: a title search on an off-provider
    film matches some unrelated real movie and serves its poster, plot, and cast.

    So this is the movie analogue of the owned EPISODE .nfo: `lockdata=true`, with the
    title and plot the pipeline authored. validate_plan guarantees both are non-empty
    before we ever get here, so locking can never freeze a blank."""
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        "<movie>",
        f"  <title>{escape(str(title))}</title>",
        f"  <originaltitle>{escape(str(title))}</originaltitle>",
    ]
    if year:
        parts.append(f"  <year>{int(year)}</year>")
    if premiered:
        parts.append(f"  <premiered>{escape(str(premiered))}</premiered>")
    parts.append(f"  <plot>{escape(str(plot))}</plot>")
    parts.append(f"  <outline>{escape(str(plot))}</outline>")
    if studio:
        parts.append(f"  <studio>{escape(str(studio))}</studio>")
    for g in genres or ():
        parts.append(f"  <genre>{escape(str(g))}</genre>")
    parts.append("  <lockdata>true</lockdata>")
    parts.append("</movie>")
    return "\n".join(parts) + "\n"


def _write_movie_nfo(plan, files):
    """Write a <movie> .nfo next to each placed movie video. Two flavours, mirroring
    the two ways a film's identity can be secured:

      * PROVIDER-BACKED (the normal case) — an UNLOCKED seed pinning the TMDB id.
        Movie analogue of the tvshow.nfo seed: with the id pinned Jellyfin identifies
        the exact film and can never fuzzy-match it to a sibling in the same
        collection — the failure that filed One Piece 3D: Straw Hat Chase under One
        Piece Film: Strong World's TMDB entry, so both showed as 'Strong World'.
        Left unlocked so Jellyfin still scrapes plot/art/cast for it.
      * OWNED (no provider entry exists at all) — a LOCKED .nfo carrying the title
        and plot the pipeline authored, so Jellyfin serves exactly that and never
        title-searches an off-provider film onto some unrelated real movie.

    Never clobbers an existing .nfo. A movie with neither an id nor owned metadata is
    skipped (validate_plan already refuses that combination on the live path).

    The id comes from the file's own `tmdb_id`, or the plan's top-level `tmdb_id`
    when the plan places exactly one movie (so the top-level id is unambiguous).
    Title/year default to the destination filename (`Title (YYYY)`), which the
    plan already formats.
    """
    def _is_movie(f):
        dst = Path(f["_dst_abs"])
        rel = _media_rel(dst)
        return (rel is not None and rel.parts[0] == "Movies"
                and dst.suffix.lower() in config.VIDEO_EXTENSIONS)

    n_movies = sum(_is_movie(f) for f in plan["files"])
    single_movie_id = plan.get("tmdb_id") if n_movies == 1 else None
    for f in files:
        if not _is_movie(f):
            continue
        dst = Path(f["_dst_abs"])
        nfo = dst.with_suffix(".nfo")
        if nfo.exists():
            continue
        title, year = _parse_movie_title_year(dst.stem)
        tmdb_id = f.get("tmdb_id") or single_movie_id
        if tmdb_id:
            _atomic_write(nfo, _movie_nfo_xml(f.get("title") or title,
                                              f.get("year") or year, tmdb_id,
                                              f.get("imdb_id")))
            continue
        # Owned: no provider id, so lock our own metadata in. Mirrors the id
        # resolution above — a plan-level title is trusted only for a single-movie
        # plan, so a mixed plan's show title can never land on a film.
        if not bool(f.get("owned", plan.get("owned"))):
            continue
        owned_title = (f.get("movie_title") or f.get("title")
                       or (plan.get("title") if n_movies == 1 else None) or title)
        plot = f.get("plot")
        if not str(plot or "").strip():
            continue                       # nothing to lock; leave Jellyfin alone
        _atomic_write(nfo, _owned_movie_nfo_xml(
            owned_title, f.get("year") or year, plot,
            studio=f.get("studio"), premiered=f.get("premiered"),
            genres=f.get("genres") or ()))


def write_locked_episode_nfo(video_path, show_title, entry):
    """Write a locked episode .nfo next to an already-placed episode video, using
    the exact same XML the owned-ingest path produces (so a repaired episode is
    byte-identical to one owned at ingest time). `entry` is a dict with `season`,
    `episode`, a non-empty `episode_title` and `plot`, and optional airs-before
    fields. Callers MUST validate title/plot are non-empty first — this writer is
    deliberately dumb, mirroring how validate_plan guards the live path.
    """
    _atomic_write(episode_nfo_path(video_path), _episode_nfo_xml(entry, show_title))
