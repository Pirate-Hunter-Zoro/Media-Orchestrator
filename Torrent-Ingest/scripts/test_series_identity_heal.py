#!/usr/bin/env python3
"""Series identity + title art self-heal (HANDOFF 10.3, the Twilight Zone (2019)).

THE INCIDENT. The shelf carried `<tmdbid>83135</tmdbid>` (correct) beside
`<tvdbid>325542</tvdbid>`, `<premiered>2013-01-30</premiered>` and
`<originaltitle>萌宠成长记（精编版）</originaltitle>` -- the remnant of a plan that used
*Too Cute*'s ids. `folder.jpg` was byte-identical to TMDB 80979's poster, and because
local art outranks remote the wrong cover survived every Jellyfin refresh.
`_scan_episode_art` only inspects episode stills, so nothing ever looked at series
identity fields or title-level art. The fault could not self-heal.

This pins the whole repair, offline (every provider answer is stubbed):
  Part 1 -- the contaminated shape fires; the computed trigger is `premiered` vs TMDB
            `first_air_date`, with the tvdb/originaltitle disagreement as evidence.
  Part 2 -- the repair rewrites tvshow.nfo (identity, and the -1 season/episode ghost
            keys removed), recomputes and locks each season.nfo year, and overwrites
            the contaminated title art with the verified identity's own bytes.
  Part 3 -- the false-positive shape a per-year check would hit: a stale `<year>` with
            a `premiered` that matches TMDB (Assassination Classroom / Hazbin Hotel /
            Star vs. / Yamato 2205, measured across 299 live nfos) does NOT fire.
  Part 4 -- offline (no provider answer) is a no-op, and no file is touched.
  Part 5 -- the episode-slot writer inserts <season>/<episode> into a sidecar that
            lacks them (Jellyfin's null-index S02 saver) and locks it.
  Part 6 -- a tvdb id pinned by TWO series and uncorroborated by TMDB (the
            2011/2025 Gumball mixup) fires on the side that does not own it, and
            the repair removes the tag; the corroborated side never fires and an
            uncollided id is left alone.

    python3 scripts/test_series_identity_heal.py

Read-only against the live library; fixtures are temporary. Exit 0 = all checks passed.
"""

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import library                                                       # noqa: E402
import media_doctor as md                                            # noqa: E402
import tmdbguide                                                     # noqa: E402

failures: list[str] = []


def check(label, cond):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        failures.append(label)


TZ_NFO = """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<tvshow>
  <plot>An updated version of the classic anthology series.</plot>
  <lockdata>true</lockdata>
  <title>The Twilight Zone</title>
  <originaltitle>萌宠成长记（精编版）</originaltitle>
  <year>2019</year>
  <tmdbid>83135</tmdbid>
  <premiered>2013-01-30</premiered>
  <releasedate>2013-01-30</releasedate>
  <tvdbid>325542</tvdbid>
  <id>325542</id>
  <episodeguide><url cache="325542.xml">http://www.thetvdb.com/api/X/series/325542/all/en.zip</url></episodeguide>
  <enddate>2013-03-06</enddate>
  <season>-1</season>
  <episode>-1</episode>
  <uniqueid type="tmdb">83135</uniqueid>
  <uniqueid type="tvdb" default="true">325542</uniqueid>
</tvshow>"""

GOOD_IDENT = {"name": "The Twilight Zone", "original_name": "The Twilight Zone",
              "year": 2019, "first_air_date": "2019-04-01",
              "last_air_date": "2020-06-25", "tvdb_id": "358915",
              "tmdb_id": "83135"}


def build(tmp):
    show = Path(tmp) / "The Twilight Zone (2019)"
    (show / "Season 01").mkdir(parents=True)
    (show / "Season 02").mkdir(parents=True)
    (show / "tvshow.nfo").write_text(TZ_NFO, encoding="utf-8")
    (show / "Season 01" / "season.nfo").write_text(
        '<season>\n  <title>Season 1</title>\n  <year>2013</year>\n'
        '  <premiered>2013-01-30</premiered>\n  <lockdata>false</lockdata>\n'
        '</season>', encoding="utf-8")
    (show / "Season 02" / "season.nfo").write_text(
        '<season>\n  <title>Season 2</title>\n  <year>2013</year>\n'
        '  <lockdata>false</lockdata>\n</season>', encoding="utf-8")
    for name in ("folder.jpg", "landscape.jpg", "season01-poster.jpg",
                 "season02-poster.jpg"):
        (show / name).write_bytes(b"TOO-CUTE-ART")
    return show


def with_stubs(fn):
    old = (tmdbguide.show_identity, tmdbguide.art_urls, tmdbguide.season_info,
           md._download_bytes)
    tmdbguide.show_identity = lambda _id: dict(GOOD_IDENT)
    tmdbguide.art_urls = lambda _id: {"poster": "POSTER-URL", "backdrop": "BACK-URL"}
    tmdbguide.season_info = lambda _id, s: {"year": 2019 if s == 1 else 2020,
                                            "air_date": "2019-04-01" if s == 1
                                            else "2020-06-25",
                                            "poster_url": f"SEASON-{s}-URL",
                                            "name": f"Season {s}"}
    md._download_bytes = lambda url, timeout=60: (f"CORRECT:{url}".encode()
                                                  if url else None)
    try:
        return fn()
    finally:
        (tmdbguide.show_identity, tmdbguide.art_urls, tmdbguide.season_info,
         md._download_bytes) = old


# --- Part 1: the contaminated shape fires ------------------------------------
print("Part 1 -- detection")
tmp = tempfile.TemporaryDirectory()
try:
    show = build(tmp.name)
    hit = with_stubs(lambda: md._series_identity_problem(show, {}))
    check("contaminated identity is detected", bool(hit))
    detail, ident = hit or ("", {})
    check("the verified tvdb id is carried into the repair",
          ident.get("tvdb_id") == "358915" and ident.get("tmdb_id") == "83135")
    check("the evidence names the premiere and the wrong tvdb id",
          "2013-01-30" in detail and "325542" in detail)
    check("all four title images are listed",
          len(md._title_art_files(show)) == 4)

    # --- Part 2: the repair --------------------------------------------------
    print("Part 2 -- repair through the tool")
    with_stubs(lambda: (md._rewrite_series_identity(show, ident),
                        md._rewrite_season_identity(show, "83135")))
    nfo = (show / "tvshow.nfo").read_text("utf-8")
    check("tvdbid rewritten", "<tvdbid>358915</tvdbid>" in nfo)
    check("premiered/releasedate rewritten",
          nfo.count("<premiered>2019-04-01</premiered>") == 1
          and nfo.count("<releasedate>2019-04-01</releasedate>") == 1)
    check("originaltitle rewritten", "<originaltitle>The Twilight Zone</originaltitle>" in nfo)
    check("the stale enddate is replaced",
          "<enddate>2020-06-25</enddate>" in nfo and "2013-03-06" not in nfo)
    check("uniqueid tvdb rewritten", "<uniqueid type=\"tvdb\" default=\"true\">358915"
          "</uniqueid>" in nfo)
    check("the -1 ghost keys are gone",
          "<season>-1</season>" not in nfo and "<episode>-1</episode>" not in nfo)
    check("legacy <id>/<episodeguide> gone",
          "<id>325542</id>" not in nfo and "<episodeguide>" not in nfo)
    check("unrelated fields survive",
          "An updated version of the classic anthology series." in nfo
          and "<lockdata>true</lockdata>" in nfo)
    s1 = (show / "Season 01" / "season.nfo").read_text("utf-8")
    s2 = (show / "Season 02" / "season.nfo").read_text("utf-8")
    check("season 1 year corrected from its own air date",
          "<year>2019</year>" in s1 and "2013" not in s1)
    check("season 2 year corrected", "<year>2020</year>" in s2)
    check("season nfos locked so a refresh cannot re-stamp",
          "<lockdata>true</lockdata>" in s1 and "<lockdata>true</lockdata>" in s2)

    fixed, failed = with_stubs(
        lambda: md._replace_title_art(show, "83135",
                                      [str(p) for p in md._title_art_files(show)]))
    check("all four images replaced", fixed == 4 and failed == 0)
    check("folder.jpg carries the verified poster bytes",
          (show / "folder.jpg").read_bytes() == b"CORRECT:POSTER-URL")
    check("landscape.jpg carries the verified backdrop bytes",
          (show / "landscape.jpg").read_bytes() == b"CORRECT:BACK-URL")
    check("season poster carries the season poster bytes",
          (show / "season01-poster.jpg").read_bytes() == b"CORRECT:SEASON-1-URL")
    check("the Too Cute bytes are gone from every image",
          all(b"TOO-CUTE" not in p.read_bytes() for p in md._title_art_files(show)))
finally:
    tmp.cleanup()

# --- Part 3: the false-positive shape does not fire ---------------------------
print("Part 3 -- no false positive on a stale <year>")
tmp = tempfile.TemporaryDirectory()
try:
    show = Path(tmp.name) / "Assassination Classroom (2015)"
    show.mkdir(parents=True)
    (show / "tvshow.nfo").write_text(
        "<tvshow><title>Assassination Classroom</title>"
        "<originaltitle>暗殺教室</originaltitle><year>2013</year>"
        "<premiered>2015-01-10</premiered><tmdbid>62110</tmdbid>"
        "<tvdbid>283947</tvdbid></tvshow>", encoding="utf-8")
    ident = dict(GOOD_IDENT, name="Assassination Classroom", original_name="暗殺教室",
                 year=2015, first_air_date="2015-01-10", tvdb_id="283947")
    old = tmdbguide.show_identity
    tmdbguide.show_identity = lambda _id: ident
    hit = md._series_identity_problem(show, {})
    tmdbguide.show_identity = old
    check("stale year + matching premiere is not touched", hit is None)

    # --- Part 4: offline -----------------------------------------------------
    print("Part 4 -- offline fails open")
    tmdbguide.show_identity = lambda _id: None
    hit = md._series_identity_problem(show, {})
    tmdbguide.show_identity = old
    check("no provider answer -> no opinion", hit is None)
finally:
    tmp.cleanup()

# --- Part 5: the episode-slot writer -----------------------------------------
print("Part 5 -- <season>/<episode> are always emitted")
tmp = tempfile.TemporaryDirectory()
try:
    show = Path(tmp.name) / "The Twilight Zone (2019)"
    season = show / "Season 02"
    season.mkdir(parents=True)
    video = season / "The Twilight Zone (2019) - S02E01.mkv"
    video.write_bytes(b"x")
    nfo = season / "The Twilight Zone (2019) - S02E01.nfo"
    nfo.write_text(
        '<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n'
        '<episodedetails>\n  <plot>Meet in the Middle.</plot>\n'
        '  <lockdata>true</lockdata>\n  <title>Meet in the Middle</title>\n'
        '</episodedetails>', encoding="utf-8")
    check("the missing slot is detected", md._nfo_episode_slot(video) is None)
    md._write_nfo_title(video, "Meet in the Middle", 1, season=2)
    text = nfo.read_text("utf-8")
    check("season and episode inserted", "<season>2</season>" in text
          and "<episode>1</episode>" in text)
    check("the slot is now readable", md._nfo_episode_slot(video) == (2, 1))
    check("the title and plot survive",
          "Meet in the Middle" in text and "lockdata>true" in text)
    # Existing keys are replaced, not duplicated.
    md._write_nfo_title(video, "Meet in the Middle", 1, season=2)
    check("a second pass does not duplicate tags",
          text.count("<season>") == 1 and nfo.read_text("utf-8").count("<season>") == 1)
finally:
    tmp.cleanup()

# --- Part 6: two series sharing one tvdb id (the Gumball mixup) ----------------
print("Part 6 -- a shared, uncorroborated tvdb id is stripped from the non-owner")
tmp = tempfile.TemporaryDirectory()
try:
    show = Path(tmp.name) / "The Wonderfully Weird World of Gumball (2025)"
    (show / "Season 01").mkdir(parents=True)
    (show / "tvshow.nfo").write_text(
        "<tvshow><title>The Wonderfully Weird World of Gumball</title>"
        "<year>2025</year><premiered>2025-10-06</premiered>"
        "<tmdbid>291904</tmdbid><tvdbid>248482</tvdbid>"
        "<uniqueid type=\"tvdb\">248482</uniqueid></tvshow>", encoding="utf-8")
    GOOD_2025 = {"name": "The Wonderfully Weird World of Gumball",
                 "original_name": "The Wonderfully Weird World of Gumball",
                 "year": 2025, "first_air_date": "2025-10-06",
                 "last_air_date": "2025-12-22", "tvdb_id": None}
    OTHER = {"show": "The Amazing World of Gumball (2011)",
             "path": str(Path(tmp.name) / "The Amazing World of Gumball (2011)"),
             "tmdb": "37606"}
    SELF = {"show": show.name, "path": str(show), "tmdb": "291904"}
    old = tmdbguide.show_identity
    tmdbguide.show_identity = lambda _id: dict(GOOD_2025)
    hit = md._series_identity_problem(show, {"Tmdb": "291904"},
                                      {"248482": [OTHER, SELF]})
    check("an uncorroborated shared tvdb id fires", bool(hit))
    detail, ident = hit or ("", {})
    check("the evidence names the other owner and TMDB's own answer",
          "The Amazing World of Gumball (2011)" in detail
          and "records no TVDB id" in detail)
    check("the verified identity carries no tvdb id to re-pin",
          ident.get("tmdb_id") == "291904" and not ident.get("tvdb_id"))
    # No other series pins the id -> not a collision, not touched.
    check("an id nobody else pins is left for the collision check",
          md._series_identity_problem(show, {"Tmdb": "291904"}, {}) is None)
    md._rewrite_series_identity(show, ident)
    nfo = (show / "tvshow.nfo").read_text("utf-8")
    check("the disowned tvdbid is removed", "<tvdbid>" not in nfo)
    check("the tvdb uniqueid is removed",
          'type="tvdb"' not in nfo)
    check("the verified tmdbid survives", "<tmdbid>291904</tmdbid>" in nfo)
    check("the premiere is untouched", "<premiered>2025-10-06</premiered>" in nfo)
    # A half-done repair: the nfo is clean but Jellyfin still pins the id (a
    # re-match that timed out). The trigger must survive on Jellyfin's own
    # ProviderIds so the next pass finishes the job.
    check("a still-pinned Jellyfin tvdb id keeps the trigger alive",
          md._series_identity_problem(
              show, {"Tmdb": "291904", "Tvdb": "248482"},
              {"248482": [OTHER, SELF]}) is not None)

    # The id's real owner is corroborated by TMDB -- it must NOT be stripped.
    GOOD_2011 = dict(GOOD_2025, name="The Amazing World of Gumball",
                     year=2011, first_air_date="2011-05-03",
                     tvdb_id="248482")
    tmdbguide.show_identity = lambda _id: dict(GOOD_2011)
    owner = Path(tmp.name) / "The Amazing World of Gumball (2011)"
    owner.mkdir()
    (owner / "tvshow.nfo").write_text(
        "<tvshow><title>The Amazing World of Gumball</title>"
        "<premiered>2011-05-03</premiered><tmdbid>37606</tmdbid>"
        "<tvdbid>248482</tvdbid></tvshow>", encoding="utf-8")
    hit = md._series_identity_problem(owner, {"Tmdb": "37606"},
                                      {"248482": [SELF, {"show": owner.name,
                                                         "path": str(owner),
                                                         "tmdb": "37606"}]})
    check("the corroborated owner is never stripped", hit is None)
    tmdbguide.show_identity = old
finally:
    tmp.cleanup()

print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    raise SystemExit(1)
print("all checks passed")
