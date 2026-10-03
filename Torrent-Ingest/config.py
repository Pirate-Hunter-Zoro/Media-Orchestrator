"""Central configuration for the Torrent-Ingest daemon.

Torrent-Ingest watches an iCloud folder for `.torrent` files, downloads each
(one at a time) to the local disk via qBittorrent, hands the finished files to a
headless AI run that decides how they map onto the Jellyfin library, applies
that plan onto the SSD library root, verifies it landed, and only then deletes the
local copy and files the source `.torrent` away in a `finished/` folder (so it is
never re-ingested, but can be re-dropped to redownload).

Every tunable lives here so the engine modules stay declarative.
"""

from pathlib import Path
from datetime import datetime, timezone
import os
import re
import sys
import time

# Machine-local paths/credentials live in the repo-root `.env` (untracked; see
# `.env.example`). Load it BEFORE any constant below reads os.environ, so a daemon
# started by hand and one started by launchd see the same configuration. Appended, not
# prepended: Torrent-Ingest's own modules must keep winning the `scripts` import.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.append(str(_REPO_ROOT))
import fleet_env                                                    # noqa: E402

fleet_env.load()

# --- Log timestamps: LOCAL time, one helper, no exceptions -------------------
#
# Every human-facing log line in this repo goes through `log_stamp()`, and it
# returns LOCAL time to match the sibling Media-Syncer daemons, whose `logging`
# handlers use `%(asctime)s` (local by default).
#
# This exists because it drifted. Five daemons here (`ingest`, `getcomics`,
# `db_guardian`, `library_supervisor`, `drive_ingest`) formatted their log lines
# as `datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")` -- UTC, printed
# with NO offset, so it is indistinguishable from local time by eye. Meanwhile
# Media-Syncer's `media_sync.log`/`predownload.log` were local, and db_guardian
# was internally inconsistent: its log lines were UTC while the backup FILENAMES
# it logged about were local, so a line reading
#   [2026-08-04 18:55:25] pushed jellyfin-db-20260804-135324.sqlite
# names a file stamped 13:53 in an entry stamped 18:55 -- the same instant, five
# hours apart on the page. Reconciling a fleet-wide incident across both repos'
# logs meant silently applying a 5-hour offset to half the evidence, and events
# read out of chronological order when interleaved.
#
# MACHINE records deliberately stay UTC (`journal.py`, the reaper's marker files,
# `repair_metadata`'s dated dirs): those are data, not prose, and they use full
# ISO-8601 with a `+00:00` offset, so they are self-describing and unambiguous.
# The bug was never "UTC" -- it was a BARE UTC clock time wearing local time's
# clothes.

def log_stamp() -> str:
    """`YYYY-MM-DD HH:MM:SS` in LOCAL time -- the fleet's human log format."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_stamp_iso() -> str:
    """Local time as full ISO-8601 WITH its UTC offset, for audit trails that are
    read by humans but may also be parsed. Self-describing, so it cannot be
    misread as the wrong zone."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


# --- Roots -------------------------------------------------------------------

# This repo.
PROJECT_ROOT = Path(__file__).resolve().parent

# iCloud Drive folder watched for new `.torrent` files. Dropped here from any
# machine (e.g. the MacBook Air); the Mini picks them up once iCloud syncs.
# Machine-specific: `TORRENTS_DIR` in `.env`.
TORRENTS_DIR = fleet_env.env_path(
    "TORRENTS_DIR",
    Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Torrents",
)

# Where a fully-ingested `.torrent` is filed once its media is safely in the
# library — a subfolder of the watch folder, NOT scanned for new work. Filing the
# source here (instead of deleting it) means a completed torrent is never marked
# "done forever" in a way that blocks a future redownload: drop the same
# `.torrent` back into TORRENTS_DIR and it ingests again from scratch.
FINISHED_DIR = TORRENTS_DIR / "finished"

# Where a FAILED `.torrent` is filed. Like FINISHED_DIR it is a subfolder of the
# watch folder (so it is NOT scanned for new work), but its purpose is the
# opposite: it gets a dead torrent OUT of the watch folder so a failure is never
# mistaken for something still "in the queue." A failed source lands here for
# inspection and stays re-droppable (drop it back into TORRENTS_DIR to retry).
FAILED_DIR = TORRENTS_DIR / "failed"

# The four state subfolders partition the watch folder so its top level holds only
# the human-facing documents (new.txt, library_health.txt, mega_free_space.txt):
#   * QUEUED_DIR    -- a `.torrent` waiting for a free pipeline slot / disk space.
#   * INGESTING_DIR -- a `.torrent` actively DOWNLOADING..VERIFIED.
#   * FINISHED_DIR  -- COMPLETED (above).
#   * FAILED_DIR    -- FAILED (above).
# A new `.torrent` dropped at the TOP of the watch folder is moved into QUEUED_DIR
# on registration; the daemon then promotes it INGESTING -> FINISHED/FAILED as its
# journal status advances, so the folder layout mirrors the state machine. Moving a
# `.torrent` back to the top of TORRENTS_DIR is still the re-drop / retry signal.
QUEUED_DIR = TORRENTS_DIR / "queued"
INGESTING_DIR = TORRENTS_DIR / "ingesting"

# How long a `.torrent` whose bencode will not parse must have been sitting
# untouched before it is filed into FAILED_DIR as truncated/corrupt. A drop that
# iCloud is still materializing can be readable-but-incomplete for a few seconds,
# and filing it away mid-sync would condemn a perfectly good torrent. Once its
# mtime is this old and it still will not parse, the bytes on disk are what iCloud
# is going to give us and the file is genuinely broken.
UNPARSEABLE_GRACE_SEC = 120

# Local fast disk we download onto. qBittorrent writes here; nothing stays here
# after a successful ingest.
DOWNLOADS_DIR = Path.home() / "Downloads"

# The local library root on the Mac SSD -- where ingest lands files. It is the `lower`
# beneath the mediafs mount: Jellyfin and YacReader read the mount at ~/MediaLibrary
# while metadata sidecars live here and freshly-ingested media lands here; Media-Syncer
# uploads it to the MEGA pool and KEEPS the local copy (the pool copy is durability, not
# the serving path). This is the internal SSD library root; external drives are an optional
# extra tier of local cache. Keep in step with Media-Syncer's SSD_LIBRARY_ROOT.
MEDIA_ROOT = fleet_env.env_path("MEDIA_ROOT", Path.home() / "Media")
SHOWS_ROOT = MEDIA_ROOT / "Shows"
MOVIES_ROOT = MEDIA_ROOT / "Movies"
# Comics/manga, served by YACReader (not Jellyfin). Manga lives under the
# `Manga/` subtree; western comics sit directly under Comics/. Naming is
# `<Series>/<Series> vNN.cbz` — title + volume, no metadata sidecar.
COMICS_ROOT = MEDIA_ROOT / "Comics"

# Comic/manga franchises: related series nest under ONE master folder (the Star Wars
# model) instead of each being a top-level `Comics/` or `Comics/Manga/` entry. `name` is
# the franchise's master folder, `kind` is "western" (Comics/) or "manga" (Comics/Manga/),
# and `members` maps a series's normalized name to the sub-folder it lives in under the
# master. EVERY member names a real sub-folder, the franchise's own main series included:
# `"battle angel alita": "Battle Angel Alita"` puts the original run in a folder that is a
# SIBLING of Last Order and Mars Chronicle, rather than loose in the master root.
#
# It used to map the main series to None, meaning "the master folder itself", and that is
# what the owner reported on 2026-08-31: "Battle Angel Alita raw volumes are in the same
# folder as the other Alita comics. The original Alita volumes should have their own
# subfolder inside like all the other Alita series do." It was not only untidy. A master
# root that holds files is a numbered namespace with no owner, so anything the table did
# not recognise was dropped into it and collided with the main run -- which is how a
# standalone ElfQuest story became "ElfQuest v06" and then "ElfQuest v10", and how the
# owner came to see "ElfQuest repeats" that were not repeats at all (§4.92).
#
# The invariant this buys, enforced in `library.validate_plan`: NO comic file is ever
# filed directly at a franchise master root. Every one lives in a named member folder.
# A series whose normalized name equals a member — or merely PREFIXES one ("Attack on Titan:
# Before the Fall") — is filed under the franchise rather than top-level. This is consulted
# by `library.comic_franchise` and injected into the identify prompt's digest so the AI and
# the deterministic fast-path agree on nesting.
COMIC_FRANCHISES = [
    {
        # STAR WARS -- the model this whole invariant is named after ("Franchise spinoffs
        # nest under one master folder (the Star Wars model)") and, until 2026-09-02, the
        # one franchise NOT IN THIS TABLE. `Comics/Star Wars Comics/` existed as a folder,
        # so releases whose filename literally said "Star Wars" landed under it and looked
        # fine; `The_Story_of_Darth_Vader.cbr` does not say it, so identify had nothing to
        # place it with and created a new top-level `Comics/Darth Vader/`.
        #
        # THIS IS THE CLASS THE TABLE EXISTS FOR. A western franchise is held together by
        # CHARACTER and UNIVERSE, not by a shared title prefix, so no amount of model
        # cleverness recovers it from the filename -- "Darth Vader" contains no evidence
        # that it is Star Wars. Only deterministic knowledge does, and this is where the
        # fleet keeps it. `scripts/build_comic_franchises.py` cannot generate these rows
        # (its prefix heuristic is manga-shaped -- see the note there), so western
        # franchises are curated by hand and that is correct, not a shortcut.
        "name": "Star Wars Comics",
        "kind": "western",
        "members": {
            "star wars": "Star Wars",
            "darth vader": "Darth Vader",
            "star wars darth vader": "Darth Vader",
            "the story of darth vader": "Darth Vader",
            "star wars darth vader and the ghost prison": "Darth Vader and the Ghost Prison",
            "darth vader and the ghost prison": "Darth Vader and the Ghost Prison",
            "star wars legends epic collection": "Star Wars Legends Epic Collection",
            "star wars modern era epic collection": "Star Wars Modern Era Epic Collection",
            "star wars darth vader modern era epic collection":
                "Star Wars Modern Era Epic Collection",
            "star wars omnibus": "Omnibuses",
            "darth maul": "Darth Maul",
            "obi wan kenobi": "Obi-Wan Kenobi",
            "star wars obi wan kenobi": "Obi-Wan Kenobi",
            "doctor aphra": "Doctor Aphra",
            "star wars doctor aphra": "Doctor Aphra",
            "star wars bounty hunters": "Bounty Hunters",
            "star wars the high republic": "The High Republic",
            "the high republic": "The High Republic",
            "star wars thrawn": "Thrawn",
            "star wars kanan": "Kanan",
            "star wars poe dameron": "Poe Dameron",
            "star wars lando": "Lando",
            "star wars han solo": "Han Solo",
            "star wars chewbacca": "Chewbacca",
            "star wars leia princess of alderaan": "Leia",
            "star wars princess leia": "Leia",
            "star wars shadow of the sith": "Shadow of the Sith",
            "star wars war of the bounty hunters": "War of the Bounty Hunters",
            "star wars crimson reign": "Crimson Reign",
            "star wars hidden empire": "Hidden Empire",
            "star wars dark droids": "Dark Droids",
        },
    },
    {
        "name": "Invincible",
        "kind": "western",
        "members": {
            "invincible": "Invincible",
            "invincible presents atom eve rex splode": "Invincible Presents - Atom Eve & Rex Splode",
            "guarding the globe": "Guarding the Globe",
            "tech jacket": "Tech Jacket",
            "the astounding wolf man": "The Astounding Wolf-Man",
        },
    },
    # ElfQuest row REMOVED 2026-09-05 (purged then), and deliberately NOT restored when
    # the owner re-acquired the franchise on 2026-09-13. Without a row, `Comics/ElfQuest/`
    # is an ordinary western series root: the Complete ElfQuest v01-v08 volumes sit
    # directly under it with `The Final Quest/` as a subseries, which is the layout the
    # owner asked for ("just a normal comic"). A franchise row would force every member
    # -- the main run included -- into its own subfolder.
    #
    # The `Comics/Manga/ElfQuest/` split measured on 2026-09-13 was NOT the model's
    # judgement: `dbhook` recorded every comic as kind `manga`, so library.db held a
    # duplicate `manga` ElfQuest series beside the seeded `comic` one and the placement
    # digest showed both. Fixed at the source 2026-09-14 (`dbhook._plan_kind` reads the
    # destination), the history is folded by the reaper's `reconcile_comics`, and a
    # future ElfQuest drop needs no nudge.
    {
        "name": "Attack on Titan",
        "kind": "manga",
        "members": {
            "attack on titan": "Attack on Titan",
            "attack on titan before the fall": "Before the Fall",
            "attack on titan no regrets": "No Regrets",
            "attack on titan lost girls": "Lost Girls",
            "attack on titan junior high": "Junior High",
            "attack on titan the harsh mistress of the city": "The Harsh Mistress of the City",
            "attack on titan choose your path": "Choose Your Path",
            "attack on titan spoof on titan": "Spoof on Titan",
            "attack on titan kuinaki sentaku": "No Regrets",
            "shingeki no kyojin": "Attack on Titan",
            "shingeki no kyojin before the fall": "Before the Fall",
            "shingeki no kyojin no regrets": "No Regrets",
            "shingeki no kyojin lost girls": "Lost Girls",
        },
    },
    {
        "name": "Battle Angel Alita",
        "kind": "manga",
        "members": {
            "battle angel alita": "Battle Angel Alita",
            "battle angel alita last order": "Last Order",
            # The omnibus is its OWN folder, not the main line. Without this exact entry
            # the prefix fallback below matches "battle angel alita last order " and files
            # it at the franchise root, mixed in with the main series' volumes.
            "battle angel alita last order omnibus": "Last Order Omnibus",
            "battle angel alita mars chronicle": "Mars Chronicle",
            "battle angel alita holy night and other stories": "Holy Night and Other Stories",
            "battle angel alita ashen victor": "Ashen Victor",
            "gunnm": "Battle Angel Alita",
            "gunnm last order": "Last Order",
            "gunnm mars chronicle": "Mars Chronicle",
        },
    },
    {
        "name": "BanG Dream!",
        "kind": "manga",
        "members": {
            "bang dream": "BanG Dream!",
            "bang dream girls band party": "Girls Band Party!",
            "bang dream girls band party roselia stage": "Girls Band Party! Roselia Stage",
            "bang dream star beat": "Star Beat",
            "bang dream it's mygo": "It's MyGO!!!!!",
            "bang dream ave mujica": "Ave Mujica",
            # The manga's actual title carries the "-manuscriptus-" suffix, which the
            # prefix fallback would otherwise collapse onto the franchise root.
            "bang dream ave mujica manuscriptus": "Ave Mujica -manuscriptus-",
        },
    },
    # ---------------------------------------------------------------------------------
    # The rows below were GENERATED by scripts/build_comic_franchises.py, not typed in.
    # Its two inputs are the library's own sibling-folder layout and AniList (free,
    # key-less); every member here is either a folder that exists on disk today or a
    # series AniList lists under the same franchise. Re-run the script after the library
    # grows and paste what it prints -- do not hand-edit these, and never add a franchise
    # from memory, which is the hard-coding the owner objected to on 2026-08-31.
    #
    # Adding a row changes placement for NEW arrivals only. The already-flat folders were
    # migrated MEGA-side by scripts/migrate_comic_franchises.py in the same change; a row
    # added WITHOUT that migration splits a series across two paths.
    # ---------------------------------------------------------------------------------
    # One Piece (manga): 1 sibling folder(s) on disk
    #     on disk: One Piece - Ace's Story
    # `Wanted! Eiichiro Oda Before One Piece` is deliberately NOT a member: it is an
    # Oda one-shots collection, not main-continuity One Piece, and the generator left it
    # out for that reason (owner decision recorded 2026-09-20, HANDOFF §10.5e).
    {
        "name": "One Piece",
        "kind": "manga",
        "members": {
            "one piece": "One Piece",
            "one piece aces story": "Ace's Story",
        },
    },
    {
        "name": "A Certain Magical Index",
        "kind": "manga",
        "members": {
            "a certain magical index": "A Certain Magical Index",
            "a certain scientific accelerator": "A Certain Scientific Accelerator",
            "a certain scientific railgun": "A Certain Scientific Railgun",
            "a certain scientific railgun astral buddy": "A Certain Scientific Railgun - Astral Buddy",
            "a certain magical index ss": "SS",
            "a certain magical index nt": "NT",
        },
    },
    {
        "name": "Akame ga KILL!",
        "kind": "manga",
        "members": {
            "akame ga kill": "Akame ga KILL!",
            "akame ga kill zero": "ZERO",
            "akame ga kill 1 5": "1.5",
        },
    },
    {
        # Citrus+ is the SEQUEL of Citrus, and the generator missed the pair for a whole
        # release cycle (owner report 2026-10-03): `normalize_folder_name` deleted the
        # trailing '+', so `Citrus+` and `Citrus` collapsed to the same key and the
        # member was unrepresentable. The normalization now keeps it as "plus", and the
        # relation-confirmed weak-prefix detector in `build_comic_franchises.py` finds
        # this shape from AniList's own SEQUEL/PREQUEL evidence. Values are the live
        # folder names, so `Comics/Manga/Citrus/Citrus+/` is where a drop must land.
        "name": "Citrus",
        "kind": "manga",
        "members": {
            "citrus": "Citrus",
            "citrus plus": "Citrus+",
        },
    },
    {
        "name": "Ashita no Joe",
        "kind": "manga",
        "members": {
            "ashita no joe": "Ashita no Joe",
            "ashita no joe fighting for tomorrow": "Fighting for Tomorrow",
            "ashita no joe ni akogarete": "ni Akogarete",
        },
    },
    {
        "name": "Blue Lock",
        "kind": "manga",
        "members": {
            "blue lock": "Blue Lock",
            "blue lock episode nagi": "Episode Nagi",
        },
    },
    {
        "name": "Cowboy Bebop",
        "kind": "manga",
        "members": {
            "cowboy bebop": "Cowboy Bebop",
            "cowboy bebop shooting star": "Shooting Star",
        },
    },
    {
        "name": "Dragon Ball",
        "kind": "manga",
        "members": {
            "dragon ball": "Dragon Ball",
            "dragon ball super": "Super",
            "dragon ball heya son goku and his friends return": "Heya! Son Goku and His Friends Return!!",
            "dragon ball sd": "SD",
            "dragon ball episode of bardock": "Episode of Bardock",
            "dragon ball heroes victory mission": "Heroes: Victory Mission",
            "dragon ball minus the departure of the fated child": "Minus: The Departure of the Fated Child",
            "dragon ball z fukkatsu no f": "Z: Fukkatsu no \"F\"",
            "dragon ball xenoverse 2 the manga": "Xenoverse 2 The Manga",
            "dragon ball that time i got reincarnated as yamcha": "That Time I Got Reincarnated as Yamcha!",
            "dragon ball super broly": "Super: Broly",
            "dragon ball super divers": "Super Divers",
            "dragon ball super divers lets super dive": "Super Divers: Let\u2019s Super Dive!!",
        },
    },
    {
        "name": "Fairy Tail",
        "kind": "manga",
        "members": {
            "fairy tail": "Fairy Tail",
            "fairy tail 100 years quest": "100 Years Quest",
            "fairy tail blue mistral": "Blue Mistral",
            "fairy tail fairy girls": "Fairy Girls",
            "fairy tail ice trail": "Ice Trail",
            "fairy tail lightning gods": "Lightning Gods",
            "fairy tail rhodonite": "Rhodonite",
            "fairy tail twin dragons of saber tooth": "Twin Dragons of Saber Tooth",
            "fairy tail s": "S",
            "fairy tail zero": "Zero",
            "fairy tail s tales from fairy tail": "S: Tales from Fairy Tail",
            "fairy tail x rave": "x Rave",
            "fairy tail houou no miko hajimari no asa": "Houou no Miko - Hajimari no Asa",
            "fairy tail x the 7 deadly sins christmas special": "x The 7 Deadly Sins Christmas Special",
            "fairy tail matsuri": "Matsuri",
            "fairy tail zer": "Zer\u00f8",
            "fairy tail ouedo fairy tail": "Ouedo Fairy Tail",
            "fairy tail shousetsu fushigi no kuni no fairy tail": "Shousetsu Fushigi no Kuni no Fairy Tail",
            "fairy tail happys heroic adventure": "Happy's Heroic Adventure",
            "fairy tail city hero": "City Hero",
            "fairy tail the path you believe in": "The Path You Believe In",
            "fairy tail re fantasia": "RE:FANTASIA",
        },
    },
    {
        "name": "Ghost in the Shell",
        "kind": "manga",
        "members": {
            "ghost in the shell": "Ghost in the Shell",
            "ghost in the shell stand alone complex": "Stand Alone Complex",
            "ghost in the shell the human algorithm": "The Human Algorithm",
        },
    },
    {
        "name": "Goblin Slayer",
        "kind": "manga",
        "members": {
            "goblin slayer": "Goblin Slayer",
            "goblin slayer side story year one": "Side Story - Year One",
            "goblin slayer brand new day": "Brand New Day",
            "goblin slayer gaiden 2 tsubanari no daikatana": "Gaiden 2: Tsubanari no Daikatana",
            "goblin slayer side story ii dai katana": "Side Story II: Dai Katana",
            "goblin slayer a day in the life": "A Day in the Life",
        },
    },
    {
        "name": "I've Been Killing Slimes for 300 Years and Maxed Out My Level",
        "kind": "manga",
        "members": {
            "ive been killing slimes for 300 years and maxed out my level": "I've Been Killing Slimes for 300 Years and Maxed Out My Level",
            "ive been killing slimes for 300 years and maxed out my level spin off the red dragon academy for girls": "Spin-off - The Red Dragon Academy for Girls",
        },
    },
    {
        # Generated 2026-10-03 by `build_comic_franchises.py --relations`: Inuyasha and
        # Yashahime share NO title prefix, so only AniList's own SEQUEL/PREQUEL relations
        # can tie them together -- and they were sitting as two top-level folders. The
        # same audit class as Citrus/Citrus+ (one parent, one sequel, two roots); the
        # `--relations` scan is how the next one is found instead of reported.
        "name": "Inuyasha",
        "kind": "manga",
        "members": {
            "inuyasha": "Inuyasha",
            "yashahime princess half demon": "Yashahime - Princess Half-Demon",
            "inuyasha special": "Special",
        },
    },
    {
        "name": "Magic Knight Rayearth",
        "kind": "manga",
        "members": {
            "magic knight rayearth": "Magic Knight Rayearth",
            "magic knight rayearth 2": "2",
            "magic knight rayearth gaiden": "Gaiden",
        },
    },
    {
        "name": "Mushoku Tensei - Jobless Reincarnation",
        "kind": "manga",
        "members": {
            "mushoku tensei jobless reincarnation": "Mushoku Tensei - Jobless Reincarnation",
            "mushoku tensei jobless reincarnation eris sharpens her fangs": "~Eris Sharpens Her Fangs~",
            "mushoku tensei jobless reincarnation recollections": "\u2013 Recollections",
            "mushoku tensei jobless reincarnation a journey of two lifetimes": "\u2013 A Journey of Two Lifetimes",
        },
    },
    {
        "name": "My Hero Academia",
        "kind": "manga",
        "members": {
            "my hero academia": "My Hero Academia",
            "my hero academia school briefs": "School Briefs",
            "my hero academia team up missions": "Team-Up Missions",
            "my hero academia vigilantes": "Vigilantes",
            "my hero academia smash": "Smash!!",
            "my hero academia all might rising": "All Might: Rising",
            "my hero academia team up missions one shot whos prince charming": "Team-Up Missions One-Shot: Who's Prince Charming?",
            "my hero academia league of villains undercover": "League of Villains: Undercover",
            "my hero academia endeavors mission": "Endeavor's Mission",
            "my hero academia special one shot connect to the day": "Special One-Shot: Connect to the Day",
        },
    },
    {
        "name": "Rurouni Kenshin",
        "kind": "manga",
        "members": {
            "rurouni kenshin": "Rurouni Kenshin",
            "rurouni kenshin restoration": "Restoration",
            "rurouni kenshin voyage to the moon world": "Voyage to the Moon World",
            "rurouni kenshin yahikos battle": "Yahiko's Battle",
            "rurouni kenshin haru ni sakura": "Haru ni Sakura",
            "rurouni kenshin yahikos reversed edge sword": "Yahiko's Reversed-Edge Sword",
            "rurouni kenshin master of flame": "Master of Flame",
            "rurouni kenshin side story the ex con ashitaro": "Side Story: The Ex-Con Ashitaro",
            "rurouni kenshin meiji kenkaku romantan hokkaido hen": "Meiji Kenkaku Romantan - Hokkaido-hen",
        },
    },
    {
        "name": "Shaman King",
        "kind": "manga",
        "members": {
            "shaman king": "Shaman King",
            "shaman king flowers": "Flowers",
            "shaman king marcos": "Marcos",
            "shaman king red crimson": "Red Crimson",
            "shaman king the super star": "The Super Star",
            "shaman king zero": "Zero",
            "shaman king mentalite": "mentalite",
            "shaman king remix track": "REMIX TRACK",
            "shaman king faust8 eien no eliza": "FAUST8: Eien no Eliza",
            "shaman king a garden": "& a Garden",
        },
    },
    {
        "name": "Soul Eater",
        "kind": "manga",
        "members": {
            "soul eater": "Soul Eater",
            "soul eater not": "NOT!",
        },
    },
    {
        "name": "The Seven Deadly Sins",
        "kind": "manga",
        "members": {
            "the seven deadly sins": "The Seven Deadly Sins",
            "the seven deadly sins four knights of the apocalypse": "Four Knights of the Apocalypse",
            "the seven deadly sins pilot story": "s\" - Pilot Story",
            "the seven deadly sins seven days": "Seven Days",
            "the seven deadly sins seven scars they left behind": "Seven Scars They Left Behind",
            "the seven deadly sins seven colored recollections": "Seven-Colored Recollections",
            "the seven deadly sins original sins short story collection": "Original Sins Short Story Collection",
        },
    },
    {
        "name": "Tokyo Ghoul",
        "kind": "manga",
        "members": {
            "tokyo ghoul": "Tokyo Ghoul",
            "tokyo ghoul re": "re",
            "tokyo ghoul days": "Days",
            "tokyo ghoul jack": "Jack",
            "tokyo ghoul void": "Void",
            "tokyo ghoul joker": "Joker",
            "tokyo ghoul past": "Past",
            "tokyo ghoul re quest": "re: quest",
        },
    },
    # Added 2026-09-04 at owner instruction: same-series manga sitting as sibling top-level
    # folders get their own franchise root, exactly as Cowboy Bebop and Shaman King already
    # do. A table row only changes placement for NEW arrivals, so `scripts/migrate_comics.sh`
    # has to be run to move what is already filed (§4.86/§4.87).
    {
        "name": "Boruto",
        "kind": "manga",
        "members": {
            "boruto": "Naruto Next Generations",
            "boruto naruto next generations": "Naruto Next Generations",
            "boruto two blue vortex": "Two Blue Vortex",
        },
    },
    {
        "name": "20th Century Boys",
        "kind": "manga",
        "members": {
            "20th century boys": "20th Century Boys",
            "21st century boys": "21st Century Boys",
        },
    },
    {
        "name": "Durarara!!",
        "kind": "manga",
        "members": {
            "durarara": "Durarara!!",
            "durarara re dollars arc": "RE;DOLLARS Arc",
            "durarara saika arc": "Saika Arc",
            "durarara yellow scarves arc": "Yellow Scarves Arc",
        },
    },
    {
        "name": "Magi",
        "kind": "manga",
        "members": {
            "magi the labyrinth of magic": "The Labyrinth of Magic",
            "magi sinbad no bouken": "Sinbad no Bouken",
        },
    },
    # Parasyte was held OUT of this table until 2026-09-13: its 1-volume "Parasyte" folder
    # was purged as a duplicate of the Full Color Collection and was queued for deletion, and
    # it sat directly in what would be the franchise root -- so the migrator's "loose in the
    # master root" branch would have moved it out from under the reaper's queued path and
    # orphaned the pool copy. The purge completed 2026-09-05 and the file is gone, so the
    # entry is restored; the migration into `Parasyte/Full Color Collection/` and
    # `Parasyte/Reversi/` ran the same day.
    {
        "name": "Parasyte",
        "kind": "manga",
        "members": {
            "parasyte": "Parasyte",
            "parasyte full color collection": "Full Color Collection",
            "full color collection": "Full Color Collection",
            "parasyte reversi": "Reversi",
            "reversi": "Reversi",
        },
    },
    {
        "name": "Trigun",
        "kind": "manga",
        "members": {
            "trigun": "Trigun",
            "trigun maximum": "Trigun Maximum",
        },
    },
    {
        "name": "Spice & Wolf",
        "kind": "manga",
        "members": {
            "spice wolf": "Spice & Wolf",
            "wolf parchment": "Wolf & Parchment",
        },
    },
    {
        "name": "Land of the Lustrous",
        "kind": "manga",
        "members": {
            # "land of the lustrous colored" deliberately absent -- purged as a 2-volume
            # duplicate of Minimalist Color and queued for deletion (see Parasyte above).
            "land of the lustrous minimalist color": "Minimalist Color",
        },
    },
    {
        "name": "Witch Hat Atelier",
        "kind": "manga",
        "members": {
            "witch hat atelier": "Witch Hat Atelier",
            "witch hat atelier kitchen": "Kitchen",
        },
    },
]

# Light novels / e-books are NOT part of the media library: they are not served by
# YACReader or Jellyfin. They land in a Google Drive folder the owner reads on an
# e-reader, so the identify step plans them into a `Novels/` top-dir that the applier
# routes to this tree instead of MEDIA_ROOT.
NOVELS_ROOT = fleet_env.env_path("NOVELS_ROOT", Path.home() / "Novels")

# The Google Drive macOS app (com.google.drivefs), kept running by gdrive_supervisor.
GDRIVE_APP_NAME = "Google Drive"
GDRIVE_BUNDLE_ID = "com.google.drivefs"
GDRIVE_PROC_PATTERN = "Google Drive.app/Contents/MacOS"

# Loose-file direct ingest: already-downloaded media (comics, e-books, and — since
# 2026-09-13 — raw video files/folders) that arrives without a torrent. The owner drops
# it here and `direct_ingest.py` runs it through the same identify -> validate -> apply
# -> verify pipeline the torrent flow uses. Local, NOT iCloud: raw media is large and
# movement is the bridge's job (`direct_ingest_bridge.py`), below.
DIRECT_INGEST_DIR = Path.home() / "Downloads" / "DirectIngest"

# The iCloud drop mirror for direct ingest. `direct_ingest_bridge.py` watches this
# subfolder of TORRENTS_DIR, materializes each drop (iCloud may hand it over as a
# dataless placeholder), waits for it to settle, and MOVES it into DIRECT_INGEST_DIR,
# where the ordinary direct-ingest daemon files it. A drop point, not a second library:
# the iCloud copy is removed once the local copy is verified, so the folder empties.
ICLOUD_DIRECT_INGEST_DIR = TORRENTS_DIR / "DirectIngest"

# Dedicated, dot-prefixed subdir where in-flight torrents download. It lives on the
# MAC SSD (the Downloads volume), NEVER on the SSD. A torrent's heavy random I/O on
# the SSD starves the directory reads mediafs serves to Jellyfin/
# YacReader and wedges the mount -- the July 2026 One Piece outage. So torrents
# download onto the SSD, and apply_plan then copies the finished files cross-device
# onto the SSD via the staging dir (_copy_verified falls back from hardlink to a real
# copy on EXDEV). The library still LANDS on the SSD; it is just never WRITTEN there
# by the torrent client. A torrent too large to fit the SSD is REFUSED outright, never
# spilled outside the SSD (§ admit_downloads). Dot-prefixed for symmetry with the staging
# dirs. (Do not move this onto the library volume to win a same-volume hardlink: the I/O
# contention costs far more than the hardlink saves.)
INCOMING_DIR = DOWNLOADS_DIR / ".torrent-ingest"

# Overflow download dir on the LIBRARY drive itself, for the one torrent class
# that cannot use INCOMING_DIR: a SINGLE torrent so large it can't fit the local
# Downloads volume even if that volume were completely empty (need + MIN_FREE >
# the volume's total capacity). Rather than failing it "too large to ever fit"
# (the old behavior), we download it straight onto the SSD — which is measured in
# terabytes — and let apply_plan do the rename/move there. It is dot-prefixed for
# the same reason STAGING_DIRNAME is: Media-Syncer's scan and the reaper's scan
# both prune dot-dirs, so an in-flight overflow download is invisible to the
# uploader (never half-uploaded) and to the reaper (its cleanup delete is never
# mistaken for a library file vanishing). apply_plan hardlinks within this volume
# instead of copying (same device as the library), so routing here costs no extra
# transient space beyond the download itself.
LIBRARY_INCOMING_DIR = MEDIA_ROOT / ".torrent-ingest-incoming"

# The lone overwrite exception to the "never clobber a pre-existing file"
# invariant. One Pace ships newer re-cut versions of episodes it has already
# released — a repeat drop under this prefix is a *better* cut (higher quality,
# a re-edit) OR an EXTENDED cut of an episode already present, not a duplicate, so
# it must REPLACE what's on disk (in the same season/episode slot) rather than be
# dropped as already-present or filed as a new episode. Every other path in the
# library stays write-once. One Pace is also always OWNED — a fan recut absent from
# every metadata provider, so the pipeline (the identify run) writes its per-episode titles
# and plots itself (§ identify.md "One Pace"; repair keys on arc + the mkv's own
# embedded title, never a global absolute One Piece anime episode number).
# Mirrors Media-Syncer's identical `ONE_PACE_PREFIX` churn-class carve-out (its
# README, "One Pace as the lone churn class"); keep the two strings in step.
ONE_PACE_PREFIX = "Shows/One Pace (2013)/"

# Dot-prefixed staging dir created *inside* the target show/movie root while a
# plan is being applied. Files are assembled here and then atomically renamed
# into their final place. The leading dot keeps the half-applied tree invisible
# to Media-Syncer's uploader scan (which prunes dot-dirs), exactly as the
# YouTube-Downloader's `.incoming` trick does, so a partial ingest is never
# uploaded to MEGA.
STAGING_DIRNAME = ".ingest-staging"

# --- State (all gitignored) --------------------------------------------------

STATE_DIR = PROJECT_ROOT / "state"
TMP_DIR = STATE_DIR / "tmp"                      # per-torrent scratch (plans, prompts)
# A LOCAL mirror of every registered `.torrent`'s bytes (keyed by infohash), kept so a
# chunked pack — whose lifetime is measured in days — can always be resumed even after the
# iCloud watch-folder copy is evicted to a dataless placeholder that `brctl` cannot
# materialize (§ diagnosis 4.4 "source .torrent is missing"). The watch folder stays the
# authority for re-drop/retry; this mirror is only a read-back fallback.
TORRENT_SOURCE_MIRROR = STATE_DIR / "torrent_sources"
JOURNAL_FILE = STATE_DIR / "journal.jsonl"       # append-only per-torrent state machine
DECISIONS_LOG = STATE_DIR / "decisions.log"      # human-readable record of every call the AI makes
LOCK_FILE = STATE_DIR / "torrent_ingest.lock"    # single-instance guard
PLAYLISTS_DIR = STATE_DIR / "playlists"          # curated playlist manifests (source of
                                                 # truth; backed up to MEGA with all of state/)
LOG_FILE = PROJECT_ROOT / "torrent_ingest.log"   # engine log


# --- Log rotation ------------------------------------------------------------
# ENGINE logs only. These are working logs -- read to see what a daemon is doing now --
# so they are capped and the oldest slice is allowed to fall off the end.
#
# THE AUDIT TRAIL AND THE JOURNAL ARE NOT LOGS. Never point this at:
#   * state/journal.jsonl    -- operational STATE, not output. It is the crash-resumable
#                               per-torrent state machine; dropping its oldest slice loses
#                               the record of what was already applied and verified, which
#                               is what stops a re-ingest from re-doing destructive cleanup.
#   * state/decisions.log    -- the audit trail: every call the AI makes. Documented as an
#                               audit ethos precisely because it must be complete.
#   * state/reap_purges.log  -- the audit of every remote purge (per file, which remotes).
#                               It is the only record of what was DELETED from the fleet.
# The same rule that keeps ~/Library/Logs/MediaSync.err unbounded in Media-Syncer: a file
# whose value is being COMPLETE cannot be rotated by dropping the oldest part of it. Cap
# an audit trail only by archiving (gzip the old slice and keep it).
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

# --- Journal compaction ------------------------------------------------------
# state/journal.jsonl is append-only last-writer-wins, so a torrent re-snapshotted every
# cycle (a DEFERRED one waiting on disk space) adds a line per cycle forever. Compaction
# rewrites it as one line per torrent -- lossless for the state machine, because
# journal.load_records() already keeps only the last line per info hash.
#
# This is NOT the rotation above and must never become it: rotation drops the oldest slice,
# which for the journal would mean losing torrents entirely. Compaction keeps EVERY info
# hash and only discards superseded snapshots of the same torrent.
JOURNAL_COMPACT_MIN_BYTES = 5 * 1024 * 1024   # below this the rewrite is not worth the I/O
JOURNAL_COMPACT_MIN_RATIO = 2.0               # ...and only if snapshots outnumber torrents 2:1


def rotate_log_if_large(path, max_bytes: int = None, backups: int = None) -> None:
    """Rename-rotate an ENGINE log once it exceeds max_bytes.

    Safe for this repo's writers because they all open the path in append mode per write,
    so the next write recreates the file; nothing holds a stale descriptor. (That is why
    Media-Syncer needs RotatingFileHandler instead -- its logging handler keeps the file
    open, and a rename there would leave the daemon writing to an orphaned inode.)

    Args:
        path (Path): engine log to bound -- never an audit trail or the journal
        max_bytes (int, optional): rotate above this size. Defaults to LOG_MAX_BYTES.
        backups (int, optional): how many .1/.2/... slices to keep. Defaults to LOG_BACKUP_COUNT.
    """
    max_bytes = LOG_MAX_BYTES if max_bytes is None else max_bytes
    backups = LOG_BACKUP_COUNT if backups is None else backups
    try:
        if not path.exists() or path.stat().st_size < max_bytes:
            return
        # Drop the oldest, then shuffle each slice down one: log.2 -> log.3, log.1 -> log.2
        oldest = path.with_suffix(path.suffix + f".{backups}")
        if oldest.exists():
            oldest.unlink()
        for i in range(backups - 1, 0, -1):
            src = path.with_suffix(path.suffix + f".{i}")
            if src.exists():
                src.rename(path.with_suffix(path.suffix + f".{i + 1}"))
        path.rename(path.with_suffix(path.suffix + ".1"))
    except OSError:
        # Logging must never take the daemon down: a failed rotation just means the log
        # keeps growing, which is strictly better than an unhandled error in the log path.
        pass

# --- qBittorrent Web API -----------------------------------------------------

# WebUI endpoint. Port 8090 is used because 8080 is taken by YACReader on this
# machine. `startup.sh` enables the WebUI on this port with localhost auth
# bypassed, so no credentials are needed for calls originating on 127.0.0.1.
QBT_HOST = "127.0.0.1"
QBT_PORT = 8090
QBT_USERNAME = ""   # unused while localhost auth is bypassed
QBT_PASSWORD = ""

# All torrents we add are tagged with this category so the daemon only ever
# touches its own torrents and never anything the user added by hand.
QBT_CATEGORY = "torrent-ingest"

# macOS bundle id, used to (re)launch the GUI app if the WebUI is unreachable.
QBT_BUNDLE_ID = "org.qbittorrent.qBittorrent"

# --- Disk space policy -------------------------------------------------------

# Never let free space on the Downloads volume drop below this floor (bytes).
MIN_FREE_BYTES = 20 * 1024**3            # 20 GB headroom for the OS and everything else

# Floor kept free on the LIBRARY drive (the SSD library root) when admitting an overflow
# download onto it (§ LIBRARY_INCOMING_DIR). The library drive is not the OS disk,
# but it is written by Media-Syncer and the ingests themselves, so keep real
# headroom before committing a giant overflow torrent to it. A torrent whose
# `need` + this floor exceeds the SSD's TOTAL capacity can never fit even here and
# is failed; one that merely doesn't fit right now stays QUEUED and retries as the
# library drains.
LIBRARY_MIN_FREE_BYTES = 50 * 1024**3    # 50 GB headroom on the library drive

# Require free space >= torrent_size * this factor before starting a download,
# to cover qBittorrent's piece overhead and the transient during move into the library root.
SPACE_SAFETY_FACTOR = 1.15

# --- Chunked download of oversized torrents ----------------------------------
# A torrent bigger than the SSD's whole usable capacity is downloaded in space-bounded
# WAVES using qBittorrent file priorities rather than refused: fetch a batch of
# files whose sizes sum to <= CHUNK_BYTES, ingest each completed file into the library
# (Media-Syncer uploads it, then the local copy is freed), then enable the next batch, and
# so on until every file is done. This lets a huge pack flow through a small SSD. A torrent
# with a SINGLE file larger than MAX_SINGLE_FILE_BYTES still can't be chunked and is refused.
CHUNKED_TORRENTS_ENABLED = True

# The per-wave download budget is a FRACTION of the Mac's storage, not a fixed number, so it
# scales to any machine. Of the disk, 1/10 is breathing room; of the remaining 9/10, this
# fraction is dedicated to torrent ingesting (download -> rename -> upload a wave, then the
# next). The rest of the 9/10 is free/predictive download space (managed by Media-Syncer's
# predictive cache), which is also why torrents "eat away" at that space only up to this cap.
TORRENT_CHUNK_FRACTION = 1.0 / 6.0       # 1/6 of the usable (post-breathing) disk

# A MEGA free account holds ~20 GiB, and the pool files each archive onto ONE account (a
# single file cannot span accounts). So any torrent whose *largest single file* exceeds this
# cap can never be stored and is refused up front. This is a FIXED, MEGA-based number -- NOT
# a fraction of local disk -- and it is the single-file ceiling that the wave budget above
# cannot rescue (a file this big can't be chunked either).
MAX_SINGLE_FILE_BYTES = 10 * 1024 ** 3   # 10 GiB

# How long a QUEUED torrent may fail to fit the SSD's *free* space before it stops waiting
# and switches to chunked waves. The trigger above ("bigger than the whole disk") only
# catches torrents larger than total capacity; a torrent smaller than the disk but larger
# than the headroom another daemon leaves free fits neither branch and waits forever. This
# deadline is what closes that gap, so it must stay well under "a human notices": a torrent
# that fits will normally be admitted within a cycle or two of registration.
CHUNK_AFTER_DEFERRED_SEC = 2 * 3600      # 2 hours queued without fitting -> chunk it

# How long a torrent that has fetched NOTHING may sit before it is failed and its disk
# reservation released. This is the ONLY stall deadline, and it applies ONLY to a torrent
# that has never moved a byte.
#
# ONCE A TORRENT HAS FETCHED ANYTHING IT IS NEVER ABANDONED (owner decision, 2026-09-26).
# A public/DHT-only swarm's seeder gaps are measured in hours or days; a pack whose swarm
# went quiet at 70% has PROVEN it can serve bytes, and the partial payload is the one thing
# a retry cannot recreate cheaply. The owner's instruction: "give a torrent a week to start,
# and if it makes no progress by then, at THAT point we can kill it. But once it makes
# progress, give it all the time in the world." The grace window is what stops a
# never-starting drop from pinning the download budget forever; past it, a stalled
# download only ever costs disk, never bytes, because every abandon keeps the payload.
#
# The clock is qBittorrent's own `added_on` for the torrent, with the record's creation
# time as the fallback, so a daemon restart cannot re-arm it (an in-memory clock used to).
#
# THERE IS DELIBERATELY NO SHORTER "NO COMPLETE COPY" DEADLINE. One existed, selected by
# qBittorrent's `availability < 1`, on the premise that availability is a swarm-wide fact.
# It is not: it is the pieces held by the peers THIS client is currently connected to plus
# our own, so during a stall it collapses to our own completion fraction and reads < 1 even
# in a swarm full of seeders. Every stall therefore took the 4h path, and four slow-but-
# alive packs whose partial payloads were then deleted with the torrent (2026-09-23) are
# the measured cost.
STALL_FIRST_PROGRESS_GRACE_SEC = 7 * 24 * 3600   # one week with zero bytes -> abandon

# How long a freshly-added magnet waits for qBittorrent to fetch its metadata (the `.torrent`
# info dict) from the swarm. A magnet has no metadata of its own, so until DHT/PEX/trackers
# answer there is no way to know its total size; the wait is bounded so a dead magnet does not
# stall admission of the torrents behind it. Resolving a live magnet usually takes seconds.
MAGNET_METADATA_WAIT_SEC = 90

# How long a magnet may keep failing to resolve its metadata before it is abandoned.
# Without a bound, a magnet whose swarm is dead is re-added, waited on, removed and left
# QUEUED every cycle forever -- it never fails, never drains, holds a queue slot, and
# reprints its "unresolved" line every couple of minutes. Generous enough that a genuinely
# slow swarm (a rare pack with one intermittent seeder) still gets many hours of retries.
MAGNET_METADATA_ABANDON_SEC = int(os.environ.get("MAGNET_METADATA_ABANDON_SEC", str(12 * 3600)))
# ...and it must ALSO have been genuinely tried this many times. The abandon clock runs
# from the record's creation, so before the rotation fix a magnet that finally reached the
# head of a 114-deep queue at hour 12 was failed on the spot as a "dead swarm/DHT" having
# never once been offered to the swarm. Time alone is not evidence that a swarm is dead.
MAGNET_METADATA_MIN_ATTEMPTS = int(os.environ.get("MAGNET_METADATA_MIN_ATTEMPTS", "3"))

# Run the DB acceptance gate on EVERY admission this daemon makes -- magnet and
# `.torrent` alike (§4.120).
#
# The searcher's gate reads a torrent's FILE LIST and refuses one whose every file is
# already owned at equal-or-better quality. It only ever existed on the `.torrent` drop
# path, and when every `.torrent` cache began serving truncated files, 100% of drops
# became `.magnet` and the fleet's authoritative acceptance check went dark for four days
# with nothing reporting it. A magnet has no file list at drop time -- but qBittorrent
# pulls one from the swarm for a few kilobytes before any content is fetched, and that is
# where the magnet half runs.
#
# The `.torrent` half was added on 2026-09-07, when the same fault reappeared mirrored:
# the searcher was quarantined, so its gate -- the only one a `.torrent` ever passed --
# stopped being reached, and 100% of a day's drops were `.torrent`. A gate that lives on
# ONE drop path is a gate one traffic shift away from dark, whichever path it is on. This
# daemon is the single point every admission passes through, so the gate belongs here too.
#
# A kill switch rather than a constant because this is the one check on the fleet's
# acquisition path that can decline to acquire something: if it ever starts refusing work
# it should not, `ACCEPTANCE_GATE=0` restores the previous behaviour without a deploy.
# Verdicts are recorded to the shared gate heartbeat either way.
ACCEPTANCE_GATE = os.environ.get(
    "ACCEPTANCE_GATE",
    os.environ.get("MAGNET_ACCEPTANCE_GATE", "1"),      # the pre-2026-09-07 name
) not in ("0", "false", "")

# Retained so an out-of-tree caller reading the old name still sees the live value.
MAGNET_ACCEPTANCE_GATE = ACCEPTANCE_GATE

# How many times a single file from a chunked wave may fail to be filed into the library
# before it is given up on. Attempts are cheap (the bytes are already downloaded and the
# retry is just another identify), and one more attempt distinguishes a transient failure
# from a deterministic one; beyond that the wave would loop forever holding the disk.
CHUNK_FILE_MAX_ATTEMPTS = 2

# Upper bound on files per wave, independent of the byte budget. A wave is ingested in one
# blocking pass -- one identify run per file -- so a 140-file wave holds the daemon inside
# a single torrent for hours while nothing else is admitted or advanced. Bounding the file
# count keeps each pass to roughly half an hour and returns the cycle to the other torrents
# in between, at the cost of a few more priority flips per pack.
CHUNK_MAX_FILES_PER_WAVE = 32

# How long a chunked pack must stay parked (out of qBittorrent) before re-adoption is even
# considered. Without this a pack whose smallest remaining file sits right at the live
# budget boundary is re-added and re-parked every cycle (the budget check in _unpark_chunked
# passes, then the wave-selection check in _advance_chunked fails against a slightly
# different snapshot), which churns qBittorrent with hundreds of pointless add/remove pairs
# a day and never actually fetches a wave. The cooldown makes the parked state sticky enough
# that a pack only comes back when the disk has genuinely, durably freed its wave. Chunked
# packs are measured in days, so a half-hour of additional waiting is immaterial to them.
CHUNK_PARK_COOLDOWN_SEC = 30 * 60

def torrent_chunk_bytes() -> int:
    """Per-wave chunk size = TORRENT_CHUNK_FRACTION of the usable Downloads-volume capacity
    (total minus a 1/10 breathing reserve). Computed live so it tracks the actual disk."""
    import shutil
    try:
        total = shutil.disk_usage(DOWNLOADS_DIR).total
    except OSError:
        total = 512 * 1024**3
    usable = total - total // 10          # keep 1/10 breathing room
    return max(20 * 1024**3, int(usable * TORRENT_CHUNK_FRACTION))

# --- Preconditions -----------------------------------------------------------

# VPN gate: the daemon checks for a Tailscale CGNAT address (100.64/10) on a
# local interface rather than shelling out to this CLI — the Mac app's socket
# isn't reachable by the Homebrew CLI under launchd. Kept only as a reference to
# where the CLI lives; tailscale_up() does not invoke it.
TAILSCALE_BIN = "/opt/homebrew/bin/tailscale"
BRCTL_BIN = "/usr/bin/brctl"                     # materializes dataless iCloud placeholders

# --- Headless identify step (the free-model chain agent) ---------------------------------
#
# The identify step is an AGENT run, not a chat completion: it lists the download,
# ffprobes the ambiguous files, looks the show up on an episode guide, and writes a
# JSON plan to a known path. `ai_runner.py` is that agent (see `ai_client.py` for the
# loop and the tool set); every daemon in the fleet spawns it the same way.
#
# A LIST, not a path, and the first element is `sys.executable` on purpose. The three
# repos run under three different conda envs, launchd hands an agent a PATH with none
# of them on it, and a runner started through a `#!/usr/bin/env python3` shebang would
# resolve to whichever interpreter that minimal PATH happened to find -- typically one
# without this repo's dependencies. Spawning the runner with the CALLER'S OWN
# interpreter makes "the daemon can import it" and "the runner can import it" the same
# question, permanently.
AI_BIN = [sys.executable, str(PROJECT_ROOT / "ai_runner.py")]
IDENTIFY_PROMPT_FILE = PROJECT_ROOT / "prompts" / "identify.md"

# The SHORT prompt, used only when the harness has computed an unambiguous arc->season
# mapping for the release (see `arcmap`). It is not a trimmed identify.md -- it is a
# different task: confirm a computed answer rather than derive one, which is why it can
# leave out every rule about a case that mapping rules out.
#
# Its size is the point. groq's measured ceiling is ~26,400 characters against a floor
# identify prompt of 63,177, so groq could never file a single torrent -- and groq is the
# provider most often still up when the other two have hit their daily caps. A confirm
# prompt fits under it, which is how the bench widens without a paid model (§4, free-only).
# Groq's real budget is 200,000 tokens a DAY (~30 agent turns), measured 2026-09-12 from
# its own refusal; small, so `IDENTIFY_CONFIRM_MAX_TURNS` keeps one run from eating it.
CONFIRM_PROMPT_FILE = PROJECT_ROOT / "prompts" / "confirm_placement.md"
# Identify timeout SCALES with the number of media files, because a flat ceiling
# was a treadmill: 900s failed on a Phineas & Ferb S1-4 pack, then 1800s failed on
# a ~110-file complete-series Billy & Mandy pack (which also bundled a big decoy
# folder of unrelated shows that ate turns). A 20-file cour needs a fraction of
# what a 250-file complete-series pack does, so budget per media file instead of
# guessing one number that fits all.
#   timeout = clamp(BASE + PER_MEDIA_FILE * media_count, BASE, MAX)
IDENTIFY_TIMEOUT_BASE_SEC = 900
IDENTIFY_TIMEOUT_PER_MEDIA_FILE_SEC = 30
IDENTIFY_TIMEOUT_MAX_SEC = 5400          # 90 min hard ceiling on a single run
IDENTIFY_TIMEOUT_SEC = IDENTIFY_TIMEOUT_BASE_SEC   # back-compat alias / floor
# Transient-failure retry. A complete-series pack runs the agent for many minutes over
# dozens of turns; a network/server blip on one of those turns fails the whole run. That
# is transient, not a bad plan, so we retry the whole run with linear backoff rather
# than failing the torrent on the first hiccup. A DETERMINISTIC failure (invalid
# plan, validation error) does NOT match these signatures and is not retried.
#
# `ai_client` already retries the individual HTTP call four times before it gives up, so
# anything reaching here has survived that: a blip long enough to outlast the inner
# retry, or a failure between turns. This outer budget re-runs the agent from scratch.
IDENTIFY_MAX_ATTEMPTS = 4
IDENTIFY_RETRY_BACKOFF_SEC = 20          # * attempt number → 20s, 40s, 60s between tries
IDENTIFY_TRANSIENT_SIGNATURES = (
    "connection error", "connection closed", "connection reset", "timed out",
    "overloaded", "rate limit", "too many requests", "temporarily unavailable",
    "internal server error", "bad gateway", "service unavailable",
    "eof", "429", "500", "502", "503", "504", "529",
)

# THE BUSY SUBSET of the transient class -- "this PROVIDER is overloaded right now".
#
# Why it needs its own name. A transient failure is retried against the SAME provider up to
# IDENTIFY_MAX_ATTEMPTS times, and for a genuine blip (a reset connection, an EOF mid-stream)
# that is right -- though `ai_client._post` already retries the transport four times inside a
# single run, so the caller-level retry is the second line of defence, not the first.
#
# A 503 "this model is currently experiencing high demand" is a different claim. It is the
# provider telling us IT is busy, and retrying it is the one thing that cannot help. Worse,
# each caller-level retry is a WHOLE FRESH AGENT RUN: the conversation is not resumable, so
# every turn of investigation already done is thrown away.
#
# Measured 2026-09-12, and it is what prompted this. The chain had just grown to six
# providers, and gemini -- first in the chain -- answered 503 three times in a row:
#
#     15:41:40  gemini-3.5-flash       -> 503 after 5m21s -> retry the SAME model
#     15:52:40  gemini-3.5-flash       -> 503 after 5m19s -> retry the SAME model
#     16:00:45  gemini-3.5-flash       -> 429             -> next model
#     16:08:24  gemini-3-flash-preview -> 503 after 7m38s -> retry the SAME model
#
# Two models x four attempts x ~6 minutes is roughly FORTY-EIGHT MINUTES before the chain
# reaches OpenRouter -- while five other providers with budget sat idle behind it. The whole
# point of a chain is that a bad provider is skipped; a busy one must be skipped too.
#
# So: busy => move to the next provider immediately. Everything else transient keeps its
# retries.
IDENTIFY_BUSY_SIGNATURES = (
    "503", "service unavailable", "overloaded", "high demand",
    "temporarily unavailable", "502", "bad gateway", "504", "gateway timeout", "529",
)


def identify_provider_busy(detail):
    """Whether a failure says the PROVIDER is overloaded, not that the request was bad.

    Narrower than `IDENTIFY_TRANSIENT_SIGNATURES` on purpose: a connection reset or an EOF
    mid-stream is OUR side or the wire, and retrying the same provider is reasonable. This
    is only the shapes where the provider itself has said "not right now".
    """
    low = (detail or "").lower()
    return any(sig in low for sig in IDENTIFY_BUSY_SIGNATURES)


# A THIRD failure class, distinct from both transient and deterministic: the run never
# HAPPENING. Two things put it here, and they behave identically from the engine's side:
#
#   * The account has no money on it. the free-model chain answers HTTP 402 "Insufficient Balance"
#     for every request until it is topped up -- a human action with no fixed horizon.
#   * The credential is missing or rejected: the key file is absent or empty, or the API
#     answers 401/403. Also a human action with no fixed horizon.
#
# Neither is deterministic -- the plan was never attempted, and the identical input
# succeeds once the API answers -- but neither is what the TRANSIENT retry is for, because
# that budget is a few tries over ~two minutes. Retrying inside the run just burns the
# budget and then reports a hard failure.
#
# Misclassifying either is expensive: a single miss quarantines every remaining file in
# the run; on the torrent path the same exception routes through _fail(), which sends the
# .torrent to failed/ and abandons an already-completed download; and on the chunked path
# it burns CHUNK_FILE_MAX_ATTEMPTS per file and then frees the bytes UNFILED -- and for a
# chunked torrent those bytes are the only copy.
#
# So it gets its own exception (identify.IdentifyUnavailable) and every caller DEFERS
# instead of failing: the comic stays in the watch folder, the torrent record stays at
# DOWNLOADED, and the next pass picks each up untouched.
#
# Match lowercased, and keep these broad: a signature that fails to match costs content,
# while one that matches too eagerly only costs a deferral. `ai_runner` also exits 2 for
# exactly this class, which is the cheap unambiguous signal -- these strings are the
# belt to that pair of braces, and they still have to cover a provider that decides to
# phrase a spend cap in prose rather than a status code.
IDENTIFY_UNAVAILABLE_SIGNATURES = (
    "insufficient balance",
    "insufficient credit",
    "insufficient_quota",
    "quota exceeded",
    "balance is too low",
    "no free-model credential",
    "invalid api key",
    "authentication_error",
    "authentication fails",
    "authentication failed",
    "unauthorized",
    "forbidden",
    "account is suspended",
    "http 401", "http 402", "http 403",
    "status 401", "status 402", "status 403",
    # OpenRouter free-tier daily/rate caps. These are NOT transient blips — the door opens
    # on a clock (next UTC day), so retrying the SAME provider with a 20/40/60s backoff is
    # pure waste. Treat them as "this provider cannot run now" and move down the chain.
    "free-models-per-day",
    "free model requests per day",
    "rate limit exceeded",
    # Cloudflare Workers AI phrases its free cap entirely differently -- "you have used up
    # your daily free allocation of 10,000 neurons" -- and matched NOTHING above: not
    # "quota exceeded", not "rate limit exceeded", not the "hit your ... limit" shape. It
    # was reaching the deferral path only because the runner happens to exit 2 for it. A
    # provider that ever returned this text with any other exit code would have had its
    # content treated as a BAD PLAN rather than a deferral, which is precisely the
    # "content deleted UNFILED" divergence this list exists to prevent.
    "daily free allocation",
    # ...and 429 itself. The list carried 401/402/403 but not the one status code that
    # actually means "out of budget", so every 429 depended on the exit-code path alone.
    "http 429", "status 429",
)


# An ACCOUNT-level cap: the provider is refusing because the whole free allocation for the
# day is spent, not because this one model is unhappy. Every other model on that provider
# shares the same account, so trying them is guaranteed to fail -- the identify chain was
# walking all five OpenRouter models after the first had already answered "Rate limit
# exceeded: free-models-per-day", and both Cloudflare models after "you have used up your
# daily free allocation of 10,000 neurons". Deliberately NARROW: a per-model problem (a
# deprecated or overloaded model) must still fall through to that provider's siblings,
# because they really can serve it.
_ACCOUNT_CAP_SIGNATURES = (
    "free-models-per-day",
    "daily free allocation",
    "daily limit",
    "daily quota",
    "quota exceeded for the day",
)


def identify_account_capped(detail):
    """True when `detail` says the PROVIDER ACCOUNT is out of budget for the day."""
    low = (detail or "").lower()
    return any(sig in low for sig in _ACCOUNT_CAP_SIGNATURES)


# "Request too large" is a per-ORGANISATION tokens-per-minute ceiling, not a per-model
# quirk, so every model on that provider will refuse the same prompt. Groq answered
# `http 413: Request too large for model openai/gpt-oss-120b in organization ... service
# tier on_demand on tokens per minute (TPM)` and the chain then spent another 33 seconds
# asking gpt-oss-20b -- which shares that org limit -- the identical oversized question.
# 98 such attempts produced 0 plans.
#
# NOT added to IDENTIFY_UNAVAILABLE_SIGNATURES on purpose: "unavailable" means the run
# could not happen ANYWHERE and the content should be deferred. This is one provider
# declining one prompt size; another provider can and does serve it, so the chain must
# fall through rather than defer.
_REQUEST_TOO_LARGE_SIGNATURES = (
    "request too large",
    "http 413",
    "status 413",
    "tokens per minute",
)


def identify_request_too_large(detail):
    """True when the provider refused because the PROMPT exceeds its per-minute ceiling."""
    low = (detail or "").lower()
    return any(sig in low for sig in _REQUEST_TOO_LARGE_SIGNATURES)


# A 413 from an OpenAI-compatible provider states the ceiling it enforced and what we
# asked for: `on tokens per minute (TPM): Limit 8000, Requested 30399`. That number is
# the ONLY honest input for "can this provider serve this prompt" -- everything else is
# guesswork about a limit the provider publishes in the refusal itself.
_TOKEN_LIMIT_RE = re.compile(r"limit\s+(\d[\d,]*)", re.I)
_TOKEN_REQUESTED_RE = re.compile(r"requested\s+(\d[\d,]*)", re.I)


def identify_token_limit(detail):
    """`(limit, requested)` in tokens parsed out of a too-large refusal, or (None, None).

    Both halves matter. `limit` is the provider's ceiling; `requested` is what our prompt
    actually cost, and dividing the two gives a measured chars-per-token for THIS prompt
    rather than the usual 4-ish guess -- which is what lets the caller decide whether a
    smaller prompt could fit instead of banning the provider outright.
    """
    text = detail or ""
    lim = _TOKEN_LIMIT_RE.search(text)
    req = _TOKEN_REQUESTED_RE.search(text)
    def _n(m):
        try:
            return int(m.group(1).replace(",", "")) if m else None
        except ValueError:
            return None
    return _n(lim), _n(req)


# A provider's measured prompt ceiling, in characters, persisted so the fleet does not
# re-pay a 413 to relearn it after every daemon restart -- and so `identify_capacity.py`
# can report the real answer instead of an empty in-memory dict.
#
# It expires, because a ceiling is a fact about a PLAN, not about physics: the owner
# upgrading a tier, or a provider raising its free limit, must be picked up without anyone
# remembering this file exists. A week is long enough that no daily churn re-pays the
# refusal, short enough that a tier change is noticed on its own.
AI_PROMPT_CEILINGS_FILE = STATE_DIR / "ai_prompt_ceilings.json"
AI_PROMPT_CEILING_TTL_SEC = int(os.environ.get("AI_PROMPT_CEILING_TTL_SEC",
                                               str(7 * 24 * 3600)))


def load_prompt_ceilings(now=None):
    """`{provider: chars}` for every ceiling still inside its TTL. Fails OPEN (empty)."""
    import json as _json
    now = float(now or time.time())
    try:
        raw = _json.loads(AI_PROMPT_CEILINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for name, rec in (raw or {}).items():
        try:
            if (now - float(rec["at"])) < AI_PROMPT_CEILING_TTL_SEC:
                out[name] = int(rec["chars"])
        except (TypeError, ValueError, KeyError):
            continue
    return out


def save_prompt_ceiling(provider, chars, now=None):
    """Record `provider`'s measured ceiling. Keeps the SMALLEST seen within the TTL."""
    import json as _json
    now = float(now or time.time())
    try:
        raw = _json.loads(AI_PROMPT_CEILINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    prev = raw.get(provider) or {}
    try:
        fresh = (now - float(prev.get("at", 0))) < AI_PROMPT_CEILING_TTL_SEC
        chars = min(int(chars), int(prev["chars"])) if fresh else int(chars)
    except (TypeError, ValueError, KeyError):
        chars = int(chars)
    raw[provider] = {"chars": int(chars), "at": now}
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        AI_PROMPT_CEILINGS_FILE.write_text(_json.dumps(raw, indent=1), encoding="utf-8")
    except OSError:
        pass                       # an unwritable ceiling only costs us one more refusal
    return chars


# Fallback chars-per-token when a refusal does not state what it measured. Deliberately
# CONSERVATIVE (real English + JSON runs ~3.5-4.5): over-estimating the token cost of a
# prompt only makes us skip a provider we might have squeaked past, while under-estimating
# it spends a real request to be told 413 again.
IDENTIFY_CHARS_PER_TOKEN = 3.5


def identify_unavailable(detail):
    """Whether a failed AI run means the run could not HAPPEN, rather than a bad plan.

    Lives here rather than beside either caller because BOTH identify paths -- this repo's
    and the YouTube ingest's -- must agree. A signature the two spell differently is a
    silent content-deleting divergence, and the YouTube path re-exports this along with
    the signature list.

    Two matchers. The literal list covers fixed phrasings; the structural clause covers
    spend caps, where a provider names whichever cap was hit ("you've hit your monthly
    limit", "hit your rate limit for this key") and only that shape holds across all of
    them. Enumerating caps alone misses any cap not spelled in the list -- and a miss is
    not a deferral, it is content deleted UNFILED.
    """
    low = (detail or "").lower()
    if "hit your" in low and "limit" in low:
        return True
    return any(sig in low for sig in IDENTIFY_UNAVAILABLE_SIGNATURES)

# How long to idle the GetComics scan after hitting the wall. Sized against the reset
# horizon of a session limit (tens of minutes), not against a network blip -- the point is
# to stop hammering a door that opens on a clock. The daemon is not wedged: a scan that
# finds nothing to do is cheap, and every deferred file is still sitting in the watch
# folder untouched.
IDENTIFY_UNAVAILABLE_BACKOFF_SEC = 900   # 15 min

# NOTE: `IDENTIFY_TOO_LARGE_COOLDOWN_SEC` lived here and is GONE. It skipped a provider
# for 30 minutes after one "request too large", on the stated reasoning that "the prompt
# is the same shape for every record". It is not: prompt size is dominated by the library
# digest and the file listing, which differ by an order of magnitude between a 1-file
# comic and a 522-file pack. `identify._TOO_LARGE_CEILING` replaces it with the number
# the provider states in its own refusal, so a prompt under that ceiling is still tried.

# Cap on how many files the identify prompt lists inline. A pack with thousands of
# entries would blow the prompt up and tempt the run to inspect each one; beyond
# this we list a capped sample and a summary so the run stays focused on placement.
IDENTIFY_MAX_LISTING_FILES = 500

# Above this many release files the harness hands the model a deterministic skeleton
# (every file enumerated with its computed slot) and requires the plan to cover it,
# because one `Write` cannot hold a plan that size. The Smurfs pack is 409 files; the
# run investigated for 36 turns and wrote a 24-file prefix (HANDOFF 10.9). Sized below
# that so a 178-file season pack also gets the skeleton.
IDENTIFY_SKELETON_MIN_FILES = int(
    os.environ.get("TORRENT_INGEST_SKELETON_MIN_FILES", "150"))

# Deterministic placement fast-path (§ diagnosis 6.4): skip the AI identify entirely when
# the searcher's stored file→item map is complete and the destination is derivable. Gated
# inside fastpath.py on (a) unambiguous folder resolution and (b) confirmed numbering, so
# a fast-path miss is free (the AI still runs) and a fast-path hit is never riskier than
# the plan the harness validates identically. Env-tunable so it can be flipped off without
# a redeploy if it ever needs to be.
FAST_PATH_ENABLED = os.environ.get("TORRENT_INGEST_FAST_PATH", "1") == "1"

# When a stored plan is present (the searcher already settled the file→item numbering),
# the identify run is capped to this many turns and stripped of its web tools — it only
# fills in destinations, never re-derives numbering or does TMDB lookups (§ diagnosis
# 6.3.3). Applied only to comics/novels (their volume/chapter numbering is deterministic);
# shows and movies keep the full 120-turn web-enabled run whenever the fast-path cannot
# fire, because their numbering judgment is exactly what the web lookups resolve.
IDENTIFY_SETTLED_MAX_TURNS = int(os.environ.get("TORRENT_INGEST_SETTLED_MAX_TURNS", "4"))

# Turn ceiling for a CONFIRM-mode run (see `identify._confirm_prompt`). Much tighter than
# the 120 a deriving run gets, and the reason is budget rather than patience.
#
# Confirm mode exists for a provider whose per-request ceiling blocks the full prompt, and
# such a provider is small in every other dimension too: groq serves 8,000 tokens a minute
# against 200,000 a DAY, which is about thirty agent turns. A run that investigates instead
# of writing does not merely fail -- it takes the provider off the board until the next
# reset. One did, on 2026-09-12: nineteen turns of web search, no plan, 195,693 of 200,000
# tokens gone in twenty minutes.
#
# Twelve is chosen against what the job actually needs. The mapping is computed and handed
# over, and so are the specials' titles and plots, so what is genuinely left is a couple of
# TMDB lookups for any film in the pack and the Write. A run that has not written by turn
# twelve is not converging, and stopping it leaves budget for the next attempt -- which,
# unlike this one, may reach a provider with room.
IDENTIFY_CONFIRM_MAX_TURNS = int(os.environ.get("TORRENT_INGEST_CONFIRM_MAX_TURNS", "12"))

# Intra-torrent duplicate collapse (library.validate_plan). A single pack can ship
# the SAME episode more than once — a main line plus lower-quality alternates in
# sibling folders (a complete-series One Piece pack bundling "Episode 001-206
# Uncropped (480p)" and a colour-distorted 1080p upscale alongside the real BD/CR
# line). Every copy maps to the identical destination. When several sources target
# one destination we keep exactly one and drop the rest: prefer a source path that
# carries NONE of these markers (case-insensitive substring match), then the
# largest file (the higher-bitrate cut). So a path tagged as an upscale/uncropped/
# lower-resolution alternate loses to the untagged main-line copy. Tune this list
# to steer which of two same-destination copies survives; it only ever decides
# between duplicates that would otherwise collide, never drops a unique file.
DUPLICATE_DEPRIORITIZE_MARKERS = (
    "uncropped", "upscale", "480p", "360p", "720p",
)
# Model for every AI run in the fleet. Left unset uses ai_client.DEFAULT_MODEL, the
# free default. There is no paid tier any more; this knob only exists to pin a different
# free model for a test, and is read per-run, so a daemon picks it up on its next restart.
AI_MODEL = os.environ.get("TORRENT_INGEST_AI_MODEL", "").strip()

# --- Free AI providers (the identify fallback chain; § diagnosis 6.5) --------
#
# The identify step no longer runs ONE paid model. It walks a CHAIN of FREE models in
# order; when a provider writes a plan the harness rejects, the rejection + that plan are
# handed to the next provider as "fix exactly this" context, so each successive model
# corrects the last one's specific mistake. The fleet's identify is free-only by design:
# every judgment call runs on FREE tiers across SEVERAL independent providers, so a rate
# limit / daily cap on one shared pool does not starve the fleet. OpenRouter's `:free`
# tier is the primary; Groq and Cloudflare Workers AI free tiers are the independent
# fallbacks, each with its own daily quota, so the fleet never runs out of a single one.
# the free-model chain, Gemini and every other paid API are deliberately NOT in this list.
#
# THE FREE-ONLY RULE, AS IT NOW STANDS (owner decision, 2026-09-12)
# ------------------------------------------------------------------
# It used to read "no paid model, no API key with a balance." The second half is no longer
# true and must not be restated as though it were: **the owner put a one-off $10 on the
# OpenRouter account on 2026-09-12.** That is deliberate and it is not a subscription.
#
# What it bought is a GATE, not usage. OpenRouter's `:free` tier allows 50 requests a day
# under 10 credits and 1,000 a day at or above it, so the ten dollars raises the free
# allowance twentyfold and is never drawn down -- every model id in this file still carries
# `:free`, and a `:free` request is billed at zero whatever the balance says.
#
# So the invariant worth enforcing changed shape, and `scripts/audit_free_only.py` enforces
# the new one: **no request is BILLED.** That is checkable (every OpenRouter slug ends in
# `:free`; no paid endpoint or paid model family appears in fleet code) where "no account
# holds a balance" no longer is. Do not "restore" the old wording -- it would make the
# audit assert something the owner has deliberately changed, and the next session would
# read it and undo a twentyfold capacity increase.
#
# A provider is enabled simply by having its key present (env var first, then a key file
# under API_KEYS_DIR), so dropping a key file auto-enables that provider as a fallback.
# Each provider lists its models in preference order; the chain is (provider, model) in
# the order given here. All are OpenAI-compatible (the agent loop needs function calling).
# Model ids drift; when you add a provider, confirm its current model names.
API_KEYS_DIR = Path.home() / ".config" / "api-keys"

AI_PROVIDERS = (
    # Google AI Studio, through Google's own OpenAI-compatible shim. FIRST in the chain
    # because it is the only free tier that verifiably takes a WHOLE identify prompt: probed
    # 2026-09-12 with real tool definitions at 85,000 characters and it answered with a tool
    # call, which is the exact shape this fleet needs and the exact thing groq and cloudflare
    # cannot do. Free tier, no card, per-model RPM/RPD rather than a shared token pool.
    #
    # MODEL IDS HERE ARE MEASURED, NOT CHOSEN. `gemini-3.8-flash` and `gemini-flash-latest`
    # both answered 503 "experiencing high demand" on the same probe, so they are NOT listed
    # -- a slug that 503s under load is worse than absent, because the chain spends an
    # attempt on it. Re-probe with `scripts/identify_capacity.py --probe` when this rots.
    {
        "name": "gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "key_env": "GEMINI_API_KEY",
        "key_file": "gemini_api_key",
        "models": (
            "gemini-3.5-flash",
            "gemini-3-flash-preview",
        ),
    },
    {
        "name": "openrouter",
        "base_url": "https://openrouter.ai/api/v1/chat/completions",
        "key_env": "OPENROUTER_API_KEY",
        "key_file": "openrouter_key",
        # `minimax/minimax-m3:free` and `minimax/minimax-m2.7:free` were dropped
        # 2026-09-07: OpenRouter retired both free slugs and now answers each with
        # `http 404 "This model is unavailable for free. The paid version is available
        # now - use this slug instead: <paid slug>"`. Probed live twice before removing,
        # and they are NOT to be replaced by the paid slugs the error offers (§4.3). The
        # other three answered the same probe, so this list is now three live models --
        # which is also the measurement that says OpenRouter is not out of daily budget.
        "models": (
            "nvidia/nemotron-3-super-120b-a12b:free",
            "dots-studio/dots-3-note-preview:free",
            "cohere/north-mini-code:free",
        ),
    },
    # Groq free tier: OpenAI-compatible, function calling, no credit card. Free plan is
    # ~30 RPM / ~1K requests/day, but the BINDING limit is tokens: 8,000 per minute and
    # 200,000 per day across the org (measured 2026-09-12 from its own 413/429 bodies).
    # Key: https://console.groq.com/keys
    {
        "name": "groq",
        "base_url": "https://api.groq.com/openai/v1/chat/completions",
        "key_env": "GROQ_API_KEY",
        "key_file": "groq_key",
        "models": (
            "openai/gpt-oss-120b",
            "openai/gpt-oss-20b",
        ),
    },
    # NVIDIA NIM (build.nvidia.com). Free developer program, no card, OpenAI-compatible.
    # All four slugs below were probed 2026-09-12 with tool definitions AND an 85,000-char
    # prompt; all four answered with a tool call, which makes this the widest single bench
    # the fleet has. Four INDEPENDENT model families on one key is the point -- when one
    # returns empty text (the failure mode that has cost this fleet the most), the next is
    # not the same model wearing a different name.
    #
    # Model ids here rot FAST and loudly: `meta/llama-3.3-70b-instruct`,
    # `openai/gpt-oss-120b` and `qwen/qwen2.5-coder-32b-instruct` all answered HTTP 410
    # "reached its end of life" on the very probe that found these. List only what answered.
    {
        "name": "nvidia",
        "base_url": "https://integrate.api.nvidia.com/v1/chat/completions",
        "key_env": "NVIDIA_API_KEY",
        "key_file": "nvidia_api_key",
        "models": (
            "nvidia/nemotron-3-super-120b-a12b",
            "moonshotai/kimi-k3",
            "deepseek-ai/deepseek-v4-flash-0731",
            "openai/gpt-oss-20b",
        ),
    },
    # Mistral La Plateforme, "Experiment" (free) tier: no card, and an account with no
    # billing configured CANNOT be charged -- which is the whole basis for including it.
    #
    # ONE model, deliberately. Probed 2026-09-12: `open-mistral-nemo` answered with a tool
    # call at both small and 85,000-char prompts, while `mistral-small-latest` and
    # `open-mixtral-8x22b` answered HTTP 429 "Rate limit exceeded" on every attempt, small
    # and large alike. The free tier clearly does not serve those two, so listing them would
    # spend two attempts per record to learn nothing.
    {
        "name": "mistral",
        "base_url": "https://api.mistral.ai/v1/chat/completions",
        "key_env": "MISTRAL_API_KEY",
        "key_file": "mistral_api_key",
        "models": (
            "open-mistral-nemo",
        ),
    },
    # Cloudflare Workers AI free tier: 10,000 neurons/day, no card, resets daily. Needs
    # TWO values: an account id (goes into the URL) and an API token. Both read from
    # files under API_KEYS_DIR (`cloudflare_account`, `cloudflare_token`). Small daily
    # budget, so it is the last resort behind Groq. Function calling is supported on the
    # models listed. Setup: https://dash.cloudflare.com → Workers AI → "Use REST API".
    {
        "name": "cloudflare",
        "base_url": "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/chat/completions",
        "key_env": "CLOUDFLARE_API_TOKEN",
        "key_file": "cloudflare_token",
        "account_env": "CLOUDFLARE_ACCOUNT_ID",
        "account_file": "cloudflare_account",
        "models": (
            "@cf/openai/gpt-oss-20b",
            "@cf/google/gemma-4-26b-a4b-it",
        ),
    },
)


def _provider_key(provider):
    """The key for a provider: env var first, then its file under API_KEYS_DIR."""
    key = os.environ.get(provider["key_env"], "").strip()
    if key:
        return key
    try:
        key = (API_KEYS_DIR / provider["key_file"]).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return key


def _provider_account_id(provider):
    """The account id for a provider that needs one (Cloudflare), else ""."""
    acct = os.environ.get(provider.get("account_env", ""), "").strip()
    if acct:
        return acct
    fname = provider.get("account_file", "")
    if not fname:
        return ""
    try:
        return (API_KEYS_DIR / fname).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _provider_base_url(provider):
    """The provider's base URL with any `{account_id}` placeholder filled in."""
    url = provider["base_url"]
    if "{account_id}" in url:
        acct = _provider_account_id(provider)
        if not acct:
            return ""
        url = url.replace("{account_id}", acct)
    return url


def ai_provider(name):
    """`{name, base_url, key}` for a named provider, or None when its key is absent.

    The runner subprocess resolves a provider through here so the key never touches argv
    or the environment -- the same "key stays on disk" guarantee `ai_env()` makes."""
    for p in AI_PROVIDERS:
        if p["name"] == name:
            key = _provider_key(p)
            base_url = _provider_base_url(p)
            if not key or not base_url:
                return None
            return {"name": p["name"], "base_url": base_url, "key": key}
    return None


def enabled_ai_attempts():
    """The ordered identify attempts `[{provider, base_url, key, model}]`, free-first.

    Empty when no free provider has a key. `TORRENT_INGEST_AI_PROVIDERS` overrides the
    whole chain for testing/pinning: a comma-separated `name:model` list."""
    out = []
    override = os.environ.get("TORRENT_INGEST_AI_PROVIDERS", "").strip()
    if override:
        for tok in override.split(","):
            name, _, model = tok.strip().partition(":")
            if not name or not model:
                continue
            prov = ai_provider(name)
            if prov:
                out.append({"provider": prov["name"], "base_url": prov["base_url"],
                            "key": prov["key"], "model": model})
        return out
    # Runtime substitutions for model ids the provider has RETIRED (`ai_models`). The
    # config tuple below stays the human's declared preference and is never rewritten; this
    # only redirects a slug the provider has told us is gone, so the chain heals on the next
    # cycle instead of spending an attempt per record on a 404 until someone notices.
    try:
        import ai_models
        overrides = ai_models.load_overrides()
    except Exception:                                                 # noqa: BLE001
        overrides = {}
    for p in AI_PROVIDERS:
        key = _provider_key(p)
        base_url = _provider_base_url(p)
        if not key or not base_url:
            continue
        sub = overrides.get(p["name"]) or {}
        for model in (sub.get(m, m) for m in p["models"]):
            out.append({"provider": p["name"], "base_url": base_url,
                        "key": key, "model": model})
    return out

# --- Jellyfin rescan (optional) ---------------------------------------------

# If set, the daemon triggers a targeted library scan after a successful ingest
# so new media is watchable in seconds rather than waiting for the nightly scan.
# Leave URL empty to skip. API key is created in the Jellyfin dashboard.
JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "").strip()          # e.g. http://127.0.0.1:8096
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "").strip()
# Owner of any playlist we build (playlists are per-user in Jellyfin, and Infuse
# only sees the playlists of the user it logs in as). Empty = use the first user,
# which is correct for a single-user home server.
JELLYFIN_USER_ID = os.environ.get("JELLYFIN_USER_ID", "").strip()

# --- Auto-maintained playlists (playlist_watch.py) --------------------------
# Some curated playlists must keep growing as new episodes ingest. One Piece is
# the canonical case: it airs weekly forever, and this pipeline grabs each new
# episode -- so the "watchable" cut must auto-extend. For each show listed here,
# after a successful ingest the daemon judges every newly-placed episode with a
# headless AI run ("is this episode worth watching, or draggy stall/filler?")
# and appends the keepers to the show's manifest, then rebuilds the playlist.
# Maps a library-relative show folder -> the manifest slug under PLAYLISTS_DIR.
PLAYLIST_AUTO_SHOWS = {
    "Shows/One Piece (1999)": "one-piece-watchable",
}
# Safety valve: if a single ingest places MORE than this many new episodes of an
# auto-show (i.e. a bulk/complete-series pack, not a weekly drop), skip the inline
# per-episode judging -- judging hundreds synchronously would stall the daemon.
# Backfill those deliberately with `python3 playlist_watch.py --show <name>`.
PLAYLIST_AUTO_MAX_INLINE = 12
# Per-episode judge budget (one headless run). Small: one episode, a web lookup,
# a one-line verdict.
PLAYLIST_JUDGE_TIMEOUT_SEC = 240
PLAYLIST_JUDGE_MAX_TURNS = 20

# --- Metadata backup (sidecars + state) -------------------------------------

# Media-Syncer replicates only true media (video/subs/comics) to the MEGA pool;
# it deliberately ignores Jellyfin sidecars (.nfo, posters) and knows nothing
# about this repo's state/ dir. That leaves the metadata this pipeline creates --
# most critically the OWNED/locked episode .nfo, which by definition cannot be
# re-scraped -- with no off-machine copy. This step mirrors those sidecars (and
# the state/ audit trail) to a dedicated MEGA path so a lost library root can be
# rebuilt with its exact layout intact. Run nightly by nightly_metadata.sh.

RCLONE_BIN = "/opt/homebrew/bin/rclone"

# rclone config to use. Default is rclone's own machine-local config, which
# Media-Syncer's daemon keeps populated with the MEGA pool credentials. Session
# tokens belong in this file and nowhere else. Machine-specific: `RCLONE_CONFIG`
# in `.env` (legacy env alias: `TORRENT_INGEST_RCLONE_CONFIG`).
RCLONE_CONFIG = Path(
    os.environ.get("TORRENT_INGEST_RCLONE_CONFIG", "").strip()
    or fleet_env.env("RCLONE_CONFIG")
    or (Path.home() / ".config/rclone/rclone.conf")
)
# Fallback source of credentials if the machine-local config is absent: the
# untracked Media-Syncer pool conf (user/pass only, no session tokens). The backup
# COPIES it into RCLONE_CONFIG rather than using it in place, so rclone never
# writes a regenerated session token back into a file that must stay secret-free.
MEDIA_SYNCER_RCLONE_CONF = fleet_env.env_path(
    "MEDIA_SYNCER_RCLONE_CONF",
    Path.home() / "Developer" / "Media-Orchestrator" / "Media-Syncer" / "rclone.conf",
)

# Which MEGA remote (a pool account from rclone.conf) receives the backup, and
# the top-level path on it. The path is deliberately NOT Shows/Movies/Comics:
# Media-Syncer's purge/probe machinery only ever looks at those three prefixes,
# and its sync ignores every non-media extension, so this tree cannot collide
# with the media pool BY PATH.
#
# It can still collide by QUOTA, which is a different problem and the one that
# matters. Three daemons write here on their own schedules -- this repo's
# db_guardian and backup_metadata, plus Media-Syncer's backup_state -- none of
# which can hold Media-Syncer's upload lock. If the media uploader also allocated
# to this account, two independent writers would spend the same 20 GiB and push it
# over quota, which is exactly what happened once.
#
# So Media-Syncer EXCLUDES this remote from media allocation
# (`config.upload_excluded_remotes()`, which reads this very value) while still
# indexing it, so media already stored here stays visible.
#
# CHANGING THIS VALUE MOVES THAT EXCLUSION. Point it at an account you are content
# to remove from the media pool -- ideally one holding little or no media, since
# whatever is already there will simply never be joined by more. Do NOT pick
# "whichever account has the most room": that is the account the media uploader
# most wants, and excluding it wastes the pool's largest opening.
METADATA_BACKUP_REMOTE = (
    os.environ.get("TORRENT_INGEST_BACKUP_REMOTE", "").strip() or "vm_mega1"
)
METADATA_BACKUP_BASE = "metadata-backup"
# How many timestamped version dirs to retain under metadata-backup/_versions/ before
# pruning. The sidecar/state mirror runs nightly, so 14 == two weeks of rollback.
# WITHOUT a bound the version tree grows forever and fills the shared metadata-backup
# account -- which is exactly how it reached "over quota" and the state backup started
# failing. Pruning is followed by an rclone `cleanup`, because MEGA's use_trash parks
# deletes in the rubbish bin where they keep consuming quota until it is emptied.
METADATA_BACKUP_KEEP_VERSIONS = 14

# rclone filter rules for the library-sidecar mirror (first match wins). Keep the
# irreplaceable / expensive-to-regenerate sidecars; drop what Jellyfin trivially
# rebuilds from the video (per-episode `-thumb.jpg` and the `.trickplay` scrub
# tiles) and our own in-progress staging dot-dir.
METADATA_BACKUP_FILTERS = [
    "- .ingest-staging/**",
    "- .torrent-ingest-incoming/**",
    "- *.trickplay/**",
    "- *-thumb.jpg",
    "+ *.nfo",
    "+ *.jpg",
    "+ *.png",
    "- *",
]

# --- Media classification ----------------------------------------------------

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov"}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".sub", ".vtt"}
COMIC_EXTENSIONS = {".cbz", ".cbr", ".cbt", ".cb7", ".pdf"}
# Light-novel / e-book extensions. `.epub` was historically in COMIC_EXTENSIONS; it is
# now its own family so the identify step files e-books into the Google Drive Novels
# tree instead of the YACReader comics library. `.pdf` is included because the SEARCHER
# drops PDF light novels (its NOVEL_EXTENSIONS = {".epub", ".pdf"}) and the two repos
# must stay in step — a PDF-only novel dropped here used to fail "wrong file type for
# Novels/".
NOVEL_EXTENSIONS = {".epub", ".pdf"}
# Plain archive types that ARE comics but carry the wrong extension. A `.cbz` is
# literally a ZIP of page images, so a comic `.zip` is filed by renaming it to
# `.cbz` (the apply step copies it to a `.cbz` destination — no repackaging). The
# plan's dst_rel for such a file must end in `.cbz`; validation enforces that.
COMIC_CONVERT_EXTENSIONS = {".zip"}
# Extensions we actually ingest into the library; everything else in a torrent
# (samples, nfo from the release, .txt, screenshots) is ignored. A `.epub` is the
# light-novel case, routed to Novels/ rather than Comics/.
MEDIA_EXTENSIONS = (
    VIDEO_EXTENSIONS | SUBTITLE_EXTENSIONS | COMIC_EXTENSIONS
    | COMIC_CONVERT_EXTENSIONS | NOVEL_EXTENSIONS
)
# Loose files the direct-ingest daemon picks up: comic archives, e-books/PDFs, and
# video (a raw `.mkv`/`.mp4` movie or episode that did not arrive as a torrent). A
# `.zip` comic archive is picked up and the identify step renames it to `.cbz`.
# A DIRECTORY dropped here is picked up too (a season folder, a loose-pages comic
# story folder) and identify plans the whole tree in one run, exactly as for a torrent.
# Subtitle files are NOT standalone watch entries — a lone `.srt` has no identity —
# but a video's siblings (names beginning with the video's stem) are attached to its
# plan deterministically in `direct_ingest._attach_video_sidecars`.
DIRECT_INGEST_EXTENSIONS = (
    VIDEO_EXTENSIONS | COMIC_EXTENSIONS | COMIC_CONVERT_EXTENSIONS | NOVEL_EXTENSIONS
)
# Loose scanned page images are NOT ingestible as-is (YACReader reads archives,
# not a bare pile of pages), but a folder of them IS packageable into a `.cbz`
# (a `.cbz` is a plain ZIP of page images). The identify run may therefore plan a
# directory `src` — a story/chapter folder of loose pages — with a `.cbz` `dst_rel`,
# and apply_plan zips the folder in natural page order. These are the extensions
# that count as page images for that packaging (everything else in such a folder —
# Thumbs.db, release .txt, .nfo — is left out of the archive).
LOOSE_PAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

# --- Remote-deletion reaper (propagate an Infuse delete to the MEGA fleet) ---
#
# A sibling daemon (`reap.py`, launch agent `com.mikeyferguson.torrentreap`) that
# watches the SSD library root and, when a media file the fleet has *backed up*
# suddenly disappears LOCALLY (you deleted a show/movie in Infuse, which removes
# the file off the SSD), propagates that deletion outward: it pauses Media-Syncer,
# purges every remote copy of the vanished file(s) from the MEGA pool, clears the
# vanished video's Jellyfin sidecars locally AND from the metadata backup on
# METADATA_BACKUP_REMOTE, prunes Media-Syncer's state so nothing resurrects, and
# restarts Media-Syncer. This is the deliberate INVERSE of the pipeline: ingest
# is write-once and never deletes the library; the reaper is the one component
# allowed to delete remote backups — and only ever mirroring a delete you already
# made on the connected drive.
#
# The load-bearing safety property: if the DRIVE ITSELF vanishes (unmount, dead
# disk), we must NOT delete anything — the remote backup is exactly what saves you
# then. So the reaper acts only when the drive is provably healthy and only on
# files that genuinely, persistently disappeared while the rest of the library
# stayed intact (§ circuit breaker below).

# Media-Syncer lives here; we read its inventory/sync-state and control its agent.
# Machine-specific: `MEDIA_SYNCER_DIR` in `.env` (defaults to the monorepo sibling).
MEDIA_SYNCER_DIR = fleet_env.env_path(
    "MEDIA_SYNCER_DIR",
    Path.home() / "Developer" / "Media-Orchestrator" / "Media-Syncer",
)
MEDIA_SYNCER_INVENTORY = MEDIA_SYNCER_DIR / "remote_inventory.json"
# The virtual-library delete signal. When a media file is deleted THROUGH the
# mediafs mount (you remove a title in Jellyfin/Infuse), mediafs appends its
# library-relative path here; the reaper drains this to purge the file from the
# MEGA pool + metadata backup + state. In the virtual model a file vanishing from
# the SSD library root is an EVICTION, not a delete, so the reaper NO LONGER diffs the SSD library root
# snapshots -- it acts ONLY on explicit through-the-mount deletes queued here.
MEDIAFS_DELETIONS_QUEUE = MEDIA_SYNCER_DIR / "mediafs_deletions.jsonl"
# The replace signal, opposite direction to the deletions queue: when this daemon
# deliberately REPLACES a file already on disk (an anime quality upgrade -- higher
# definition or dual audio), it appends the library-relative path here; Media-Syncer
# drains it to overwrite the stale MEGA copy in place and empty that remote's rubbish
# bin. Without this, Media-Syncer's write-once sync absorbs the mtime drift and the
# pool keeps the OLD version forever. Mirror Media-Syncer's config.REPLACEMENTS_QUEUE.
MEDIA_SYNCER_REPLACEMENTS_QUEUE = MEDIA_SYNCER_DIR / "replacements.jsonl"
# Before draining the deletions queue, wait until it has been QUIET this long (no
# new appends). A mass-delete (a whole franchise) has Jellyfin unlinking files for
# several seconds; settling first batches them all into ONE purge -> one fleet
# probe, instead of splitting across drain ticks into several probes. Keyed off the
# queue file's mtime, so it costs nothing when the queue is empty.
REAP_QUEUE_SETTLE_SEC = 12
MEDIA_SYNCER_SYNC_STATE = MEDIA_SYNCER_DIR / "sync_state.json"
MEDIA_SYNCER_APP_LOG = MEDIA_SYNCER_DIR / "media_sync.log"
# Per-host launchd logs record only THIS host's own uploads — the orphan safety
# net for remote discovery (§ reaper: discover). Union both with the app log.
MEDIA_SYNCER_LAUNCHD_LOGS = [
    Path.home() / "Library" / "Logs" / "MediaSync.log",
    Path.home() / "Library" / "Logs" / "MediaSync.err",
]
# launchd agent for Media-Syncer. It has NO KeepAlive, so we pause it with
# `launchctl kill` (it stays down) and resume it with `launchctl kickstart` — the
# agent stays loaded the whole time, which is cleaner than bootout/bootstrap.
MEDIA_SYNCER_LABEL = "com.mikeyferguson.mediasync"
# Process pattern used to confirm the daemon is really down before we purge, and
# as a kill fallback (matches cancel_sync.sh's own pattern).
MEDIA_SYNCER_PROC_PATTERN = "scripts.media_sync"

LAUNCHCTL_BIN = "/bin/launchctl"

# Extensions the reaper tracks MUST mirror what Media-Syncer actually replicates —
# only these reach a remote, so only these have a remote copy to purge. Keep this
# in step with Media-Syncer's `VIDEO_EXTENSIONS | COMICS_EXTENSIONS` and the searcher's
# `VIDEO_EXTENSIONS`; a drift (`.m4v` fell out of all three) strands files that are then
# never uploaded, never evicted, never reaped, and never have their metadata purged.
REAP_TRACKED_EXTENSIONS = {".mkv", ".avi", ".mp4", ".m4v", ".srt", ".ass", ".cbz", ".cbr"}
# A vanished file with one of these extensions ALSO gets Jellyfin sidecar cleanup
# (local + metadata-backup). Subtitles/comics carry no sidecars, so they skip it.
REAP_VIDEO_EXTENSIONS = {".mkv", ".avi", ".mp4", ".m4v"}

# Cadence and debounce. A file must be observed missing across this many
# consecutive HEALTHY scans before it is eligible for purge — this rides out a
# transient FS race, a mid-scan Jellyfin/rename window, or a brief remount hiccup.
REAP_SCAN_INTERVAL_SEC = 60
REAP_DEBOUNCE_SCANS = 2

# --- Circuit breaker ---------------------------------------------------------
# The breaker's one job is to refuse a purge when the loss looks like a DRIVE-SCALE
# event (a power-outage freak wipe, a half-mounted volume dropping a whole subtree)
# rather than a deliberate delete. Such an event takes out a huge FRACTION of the
# library at once, so a single fraction guard is enough: a purge batch is ABORTED
# (logged to REAP_ALERT_FILE, nothing deleted, state untouched so it self-recovers
# when the files return) when more than this fraction of the tracked library goes
# missing in one shot. A fully-unmounted drive is already caught upstream by
# media_healthy(); a large but DELIBERATE delete that trips this is released with
# `reap.py --approve`. (Earlier absolute file-count and title-span caps were dropped
# deliberately — they only ever punished big legitimate deletes below the drive-scale
# fraction, and --approve is the sanctioned path when the fraction genuinely trips.)
REAP_MAX_MISSING_FRACTION = 0.30

# --- Settle-gate: don't wake the syncer mid-deletion --------------------------
# After the reaper pauses Media-Syncer to purge deletions, it keeps MS PAUSED
# across cycles until the library SETTLES — a cycle that finds nothing more
# missing or debouncing. Otherwise MS's download phase (the Mini restores files
# missing locally, Media-Syncer README § Download phase) would re-fetch a file
# that was deleted but not yet purged, and its upload phase would then re-upload
# it: a deletion made while a purge is already running would be resurrected. So a
# fresh deletion that lands mid-purge keeps MS asleep, gets purged in a later
# cycle, and only once no new deletions appear is MS woken exactly once.
# Bounded so an unpurgeable survivor (a remote that won't verify gone) can't strand
# MS offline forever: past this many seconds held, MS is resumed with a warning.
REAP_SETTLE_MAX_HOLD_SEC = 1800   # 30 min hard cap on holding MS down

# Concurrency for the fleet probe (cheap per-account metadata lsf calls). The cap that
# matters is MEGA's per-IP AUTH rate, not any per-account limit: every rclone call logs in,
# so this throttles logins-per-second from one exit node, and overshooting shows up as
# `err` remotes rather than as wrong answers (an errored remote is still deleted from, never
# scored clean).
#
# 6 was too low to be usable for a bulk purge. The probe costs
# `distinct titles x remotes` listings, so an owner-directed purge of 61 titles across 822
# accounts is **50,142 listings** — at 6 workers that is ~3.5 h for ONE 2,146-file batch and
# over 12 h for the four batches a large purge produces, with Media-Syncer held down the
# whole time. 16 keeps the login rate well inside what the VPN exit sustained while cutting
# that to roughly a third. **If `err` remotes start appearing in the reaper log, this is the
# first thing to lower** — correctness is unaffected, but a storm of failed logins wastes
# the window it was raised to save.
REAP_PROBE_WORKERS = 16

# Reaper state (all under state/, gitignored).
REAP_SNAPSHOT_FILE = STATE_DIR / "reap_snapshot.json"   # set of tracked files last seen present
REAP_PENDING_FILE = STATE_DIR / "reap_pending.json"     # {relpath: consecutive-missing count}
REAP_PURGES_LOG = STATE_DIR / "reap_purges.log"         # human-readable audit of every purge
REAP_ALERT_FILE = STATE_DIR / "reap_ALERT.txt"          # written when the circuit breaker trips
# Content-specific approval token for a DELIBERATE mass delete the breaker would
# otherwise refuse. `reap.py --approve` stamps the exact set of currently-vanished
# paths here; the breaker is bypassed on a later cycle ONLY for a confirmed set
# that is a subset of this stamp, and ONLY while the drive is provably healthy —
# then the covered paths are consumed from the token. Because it names exact paths,
# a stale token can never green-light a DIFFERENT future vanish (a drive fault drops
# different paths, so it is never a subset). This is the sanctioned "yes, I meant
# to delete this much" signal that replaces hand-purging.
REAP_APPROVE_FILE = STATE_DIR / "reap_APPROVE.json"     # {"paths": [...], "stamped": iso8601}
REAP_LOG_FILE = PROJECT_ROOT / "torrent_reap.log"       # reaper engine log
# Crash-safe marker: written while WE have Media-Syncer paused for a purge, cleared
# on a verified resume. If a fresh cycle finds it lingering, a prior run was killed
# mid-purge before it could resume Media-Syncer — so the new cycle resumes it. This
# guarantees Media-Syncer is never left paused across a reaper crash/restart.
REAP_PAUSED_MARKER = STATE_DIR / "reap_ms_paused"

# --- Loop timing -------------------------------------------------------------

POLL_INTERVAL_SEC = 20        # download-progress / folder-scan cadence
IDLE_INTERVAL_SEC = 60        # sleep when there is nothing to do

# How often, mid-sweep, a fresh `.torrent` dropped at the top of the watch folder is
# re-queued into queued/. A large chunked backlog makes one advance() sweep take a long
# time; without this a drop sits at the top level for the whole sweep instead of being
# queued promptly. Cheap (one iCloud directory read) and idempotent, so it can run this
# often without cost.
REGISTER_REFRESH_SEC = 20

# --- Rationing the FREE AI budget between critical and auxiliary work ---------
#
# The identify step (file renaming/placement) is the critical-path AI consumer: a torrent
# that has finished downloading cannot be FILED without it, so it must run whenever one
# needs it. Every OTHER consumer -- the media_doctor metadata escalation, the playlist
# curator/autobuild, the searcher's discovery and completeness audits -- is auxiliary and
# spends the SAME free daily budget.
#
# WHAT THIS USED TO BE, AND WHY IT WAS WRONG. This gate was `is_off_peak()`, and it
# deferred auxiliary work to the free-model chain's off-peak *billing* window (01:00-04:00 and
# 06:00-10:00 UTC were peak; everything else was half price). The fleet has not used
# the free-model chain for a long time -- it runs on OpenRouter `:free`, Groq and Cloudflare Workers
# AI, and nothing reads `deepseek_key` -- so the gate was rationing against a price that no
# longer exists. It cost ~7 hours a day of self-expansion and bought nothing.
#
# WHAT THE REAL CONSTRAINT IS. Free tiers meter a DAILY REQUEST COUNT, not an hourly
# price: OpenRouter's ~1000 free-models-per-day, Cloudflare's 10,000 neurons/day, Groq's
# per-day cap. On 2026-08-31 all three hit their ceiling at once and only 4 plans were
# accepted all day (§4.77). A clock cannot see that; the budget can be exhausted at 02:00
# and healthy at 14:00. So the ration is now against the thing that actually runs out.
#
# THE RULE. Auxiliary AI work runs whenever no provider has recently reported an
# ACCOUNT-LEVEL cap. When one has, auxiliary work stands down for
# AI_BUDGET_BACKOFF_SEC and the remaining budget goes to identify, which is the only
# consumer that can lose content by not running. A per-MODEL rate limit deliberately does
# not trip this -- that is one model being busy, not the account being spent (the same
# distinction §4.77 drew when it made the chain skip a capped provider's siblings).

AI_BUDGET_BACKOFF_SEC = int(os.environ.get("AI_BUDGET_BACKOFF_SEC", str(2 * 3600)))

# PER-PROVIDER stamps: one file per provider that reported an account-level cap.
#
# WHAT THIS REPLACED. There was ONE global stamp, `state/ai_budget_capped_at`, so the
# first provider to cap stood the WHOLE auxiliary fleet down -- media_doctor, the playlist
# curator and autobuild, the searcher's discovery and audits -- for two hours, while every
# other provider on the chain was answering normally. Measured 2026-09-05: OpenRouter and
# Cloudflare were both out of daily budget while Groq answered a ping in five seconds.
#
# The three providers meter INDEPENDENTLY (OpenRouter ~1000 free requests/day, Cloudflare
# 10,000 neurons/day, Groq a per-day request count), so "the free budget is spent" is only
# true when it is true of every provider that has a key.
_AI_BUDGET_DIR = STATE_DIR / "ai_budget_capped"
# The old single stamp. Read by nothing now: it names no provider, so it cannot say which
# of the three is out, and standing the fleet down on an unattributable claim is the exact
# failure this replaced. Left documented rather than deleted so a stale file on disk is
# recognisable instead of mysterious.
_AI_BUDGET_STAMP = STATE_DIR / "ai_budget_capped_at"


def _budget_stamp(provider):
    """Path of one provider's cap stamp. `/` and `.` cannot appear in a provider name
    (they are literals in AI_PROVIDERS), so the name is used directly."""
    return _AI_BUDGET_DIR / str(provider).replace("/", "_")


def note_ai_account_capped(provider=None, now=None):
    """Record that `provider`'s account reported being out of budget for the day.

    Called from the identify chain the moment `identify_account_capped` fires. Stored on
    disk, not in memory, because the consumers are SEPARATE PROCESSES -- the searcher, the
    playlist curator and media_doctor each need to see what the ingest daemon just learned,
    and an in-memory stamp would be re-armed by every daemon restart.

    A caller that does not know WHICH provider capped records nothing, on purpose: an
    unattributable cap used to stand every provider down (see above), and this gate's
    documented failure direction is OPEN.
    """
    if not provider:
        return
    try:
        _AI_BUDGET_DIR.mkdir(parents=True, exist_ok=True)
        _budget_stamp(provider).write_text(str(float(now or time.time())),
                                           encoding="utf-8")
    except OSError:
        pass                       # a missing stamp only makes the gate more permissive


def capped_ai_providers(now=None):
    """The names of providers whose cap stamp is still inside the backoff window."""
    now = float(now or time.time())
    out = set()
    try:
        stamps = list(_AI_BUDGET_DIR.iterdir())
    except OSError:
        return out
    for f in stamps:
        try:
            if (now - float(f.read_text(encoding="utf-8").strip())) < AI_BUDGET_BACKOFF_SEC:
                out.add(f.name)
        except (OSError, ValueError):
            continue               # an unreadable stamp cannot stand a provider down
    return out


def ai_budget_healthy(now=None):
    """True when auxiliary (non-identify) AI work may spend the free budget.

    Healthy while ANY provider with a key is uncapped -- the chain walks them all, so one
    that can still answer is enough to do the work.

    Fails OPEN: unreadable stamps, or no providers configured, means "healthy". The cost
    of being wrong in that direction is some wasted free requests; being wrong the other
    way would silently stop the library expanding itself, which is the owner's standing
    requirement.
    """
    configured = {a["provider"] for a in enabled_ai_attempts()}
    if not configured:
        return True
    return bool(configured - capped_ai_providers(now))


# --- identify's reservation on the free budget --------------------------------
#
# `ai_budget_healthy` above answers "is ANY provider uncapped?", and that question stopped
# being the right one once a provider turned out to be structurally unable to run identify.
# Measured 2026-09-07: groq's stated ceiling is 26,367 chars against a floor identify
# prompt of 89,220, and with openrouter and cloudflare both capped `ai_budget_healthy()`
# still read True -- because groq was up, and groq can never file a single torrent.
#
# The policy itself lives in `ai_budget.py`, as pure functions, because Torrent-Searcher
# needs the same answer and a policy written twice is the divergence this fleet keeps
# paying for. Both repos wrap it; `scripts/test_ai_budget_contract.py` fails the build if
# the two wrappers ever disagree.

#: Where identify records the smallest prompt it can build, so a process that must not
#: import `identify` (this one -- it would be circular) can still read the number.
IDENTIFY_FLOOR_FILE = STATE_DIR / "identify_floor_chars"


def save_identify_floor(chars):
    """Record the floor identify prompt size. Called by identify, best-effort."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        IDENTIFY_FLOOR_FILE.write_text(str(int(chars)), encoding="utf-8")
    except (OSError, TypeError, ValueError):
        pass


def identify_floor_chars():
    """The floor identify prompt size, or None if it has never been measured."""
    try:
        return int(IDENTIFY_FLOOR_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def identify_capable_providers(now=None):
    """Providers that could run an identify prompt if they had budget."""
    import ai_budget
    return ai_budget.identify_capable(
        load_prompt_ceilings(now), identify_floor_chars(),
        {a["provider"] for a in enabled_ai_attempts()})


def aux_ai_verdict(now=None):
    """Whether auxiliary (non-identify) AI work may spend right now, and why."""
    import ai_budget
    return ai_budget.evaluate(
        configured={a["provider"] for a in enabled_ai_attempts()},
        capped=capped_ai_providers(now),
        capable=identify_capable_providers(now),
        identify_pending=ai_budget.count_pending_identify(STATE_DIR / "journal.jsonl"))


def aux_ai_attempts(now=None):
    """The provider chain auxiliary work should walk: identify's providers deprioritised,
    and withheld entirely while identify has downloads waiting to be filed.

    An EMPTY list means "yield to identify this pass" -- which is not the same fact as
    "out of budget" and must not be reported as one.
    """
    import ai_budget
    return ai_budget.order_attempts(
        enabled_ai_attempts(),
        capable=identify_capable_providers(now),
        identify_pending=ai_budget.count_pending_identify(STATE_DIR / "journal.jsonl"))


def is_off_peak(now=None):
    """Deprecated name for `ai_budget_healthy`, kept so no caller breaks mid-deploy.

    The name is a leftover from the the free-model chain billing window; the fleet's auxiliary AI work
    is rationed by free-tier budget now, not by the clock. Prefer `ai_budget_healthy`.
    """
    return ai_budget_healthy(now)


# Directories to prepend to PATH so launchd's minimal env finds Homebrew tools.
EXTRA_PATH = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]


def ai_env():
    """The environment every headless AI run must be launched with.

    PATH is the one thing launchd's minimal env does not provide and the run genuinely
    needs: the agent's `Probe` tool shells out to `ffprobe`, and under a LaunchAgent it
    is not on the inherited PATH (EXTRA_PATH).

    The credential is NOT passed through here. `ai_client.api_key()` reads
    `~/.config/api-keys/openrouter_key` off disk, so the key is never in a process
    environment where `ps -E` or a crash dump would show it. `OPENROUTER_API_KEY`, if the
    caller happens to have one set, is inherited by the plain copy below and wins -- that
    is the escape hatch for pointing one run at a different account.

    Every headless invocation in this repo goes through here so the guarantee holds in
    one place instead of five hand-built copies drifting apart.
    """
    env = os.environ.copy()
    env["PATH"] = ":".join(EXTRA_PATH) + ":" + env.get("PATH", "")
    return env

# --- Jellyfin DB guardian (db_guardian.py) -----------------------------------
#
# A standalone KeepAlive daemon that keeps Jellyfin's SQLite library DB from ever
# being lost to corruption. It watches the live DB, and whenever it settles after
# a write burst it takes a NON-BLOCKING snapshot (SQLite's online-backup API, so
# Jellyfin is never locked), integrity-checks the COPY (never the live file), and
# only promotes a snapshot that passes. If a check ever finds the live DB corrupt,
# it stops Jellyfin, restores the most-recent VERIFIED snapshot over the live DB
# (the corrupt one is set aside for forensics), and restarts Jellyfin. Because a
# snapshot is promoted only after passing integrity_check, "the most recent
# backup" is by construction the most recent GOOD one — so a corrupt DB can never
# overwrite the last good copy, the exact failure a naive fixed-time cron has.
#
# Lives in Torrent-Ingest (not Media-Syncer) because this is the Jellyfin-facing
# repo: it already holds JELLYFIN_URL/API_KEY, drives Jellyfin (reaper), and runs
# the nightly metadata backup this reuses for off-machine durability.

# Jellyfin's data dir and the single SQLite library DB inside it. WAL mode means
# -wal/-shm sidecars ride alongside; the online backup folds committed WAL content
# into a consistent single-file snapshot, and a restore removes the stale sidecars.
JELLYFIN_DATA_DIR = Path.home() / "Library" / "Application Support" / "jellyfin" / "data"
JELLYFIN_DB = JELLYFIN_DATA_DIR / "jellyfin.db"

# Jellyfin runs as a GUI app in the user session; a LaunchAgent (user-session) can
# drive it with `osascript quit` / `open -a`, with a pkill fallback.
JELLYFIN_APP_NAME = "Jellyfin"
JELLYFIN_PROC_PATTERN = "Jellyfin.app/Contents/MacOS"   # liveness / kill fallback

DBG_LOG_FILE = PROJECT_ROOT / "db_guardian.log"
DBG_LOCK_FILE = STATE_DIR / "db_guardian.lock"
DBG_ALERT_FILE = STATE_DIR / "db_guardian_ALERT.txt"    # written on any corruption event

# Verified snapshots live NEXT TO the live DB (same SSD volume => instant restore)
# and deliberately OUTSIDE state/, so the nightly state sync doesn't balloon
# re-uploading 200 MB+ DB copies. Off-machine durability is handled by this
# daemon's own throttled push (below), not by the state mirror.
DBG_BACKUP_DIR = JELLYFIN_DATA_DIR.parent / "db-guardian-backups"
DBG_CORRUPT_DIR = DBG_BACKUP_DIR / "corrupt"            # corrupt DBs set aside here
DBG_KEEP_LOCAL = 14                                     # rotating good snapshots retained

# Cadence. Poll cheaply; the DB churns constantly during a library scan, so only
# snapshot once it has SETTLED (signature stable for the quiescent window) or when
# too long has passed since the last snapshot while it keeps changing.
DBG_POLL_INTERVAL_SEC = 60
DBG_QUIESCENT_SEC = 300              # signature stable this long => settled, snapshot
DBG_MAX_BACKUP_INTERVAL_SEC = 3600  # force a snapshot at least this often while busy
DBG_SQLITE_TIMEOUT_SEC = 120        # busy-timeout for the online-backup read

# Repair season/episode rows whose SeriesPresentationUniqueKey has drifted from their
# series' PresentationUniqueKey. That key -- not SeriesId -- is what /Shows/{id}/Episodes
# filters on, and Jellyfin stamps it into a child only at creation time, so a series whose
# provider identity arrives AFTER its children strands every one of them and serves zero
# episodes forever, with no other sign of damage. Runs only right after a verified backup.
DBG_RECONCILE_PRESENTATION_KEYS = True
DBG_HEAL_COOLDOWN_SEC = 1800        # refuse to re-heal within this window (failing-disk guard)

# Off-machine copy: push the newest verified snapshot to the SAME metadata-backup
# remote under its own prefix, at most this often. MEGA is throttled and local
# rotation is the fast-restore tier, so this is durability, kept gentle. 0 = off.
DBG_REMOTE_SUBPATH = "jellyfin-db"
DBG_REMOTE_PUSH_INTERVAL_SEC = 6 * 3600
DBG_KEEP_REMOTE = 7

# Gut-guard: refuse to promote a snapshot whose BaseItems count has collapsed far
# below the high-water mark -- that's a DB Jellyfin gutted after scanning an empty
# (unmounted) library, and promoting it would let a corruption-heal later restore
# the EMPTY one. A gutted-but-valid DB passes integrity_check, so this count check
# is the only thing that catches it. Skips promotion + alerts instead.
DBG_MIN_ITEM_FRACTION = 0.5        # new count must be >= this * high-water-mark
DBG_HWM_FILE = None                # set below, after DBG_BACKUP_DIR
DBG_HWM_FILE = DBG_BACKUP_DIR / ".item_count_hwm"

# --- Library app supervisor (library_supervisor.py) --------------------------
# Jellyfin + YacReader must NOT run before the mediafs mount is up. At boot they
# race the mount (and the SSD under it) and, pointed at an empty ~/MediaLibrary, show no
# media -- and a Jellyfin scan of an empty library can gut its own DB. macOS's
# "reopen apps at login" relaunches them regardless of login-item settings, so the
# supervisor is the authority: it HOLDS them down until the mount is proven
# healthy, then starts them and keeps them up. If Jellyfin comes up gutted (item
# count collapsed while the mount is healthy) it restores db_guardian's last-good
# backup. Runs as its own KeepAlive user-agent, a sibling of db_guardian.
MEDIAFS_MOUNT = fleet_env.env_path("MEDIAFS_MOUNT", Path.home() / "MediaLibrary")  # mediafs mount (== Jellyfin's path, local SSD)
MEDIAFS_HEALTH_DIRS = ("Shows", "Movies", "Comics")   # each must list >=1 entry
YACREADER_APP_NAME = "YACReaderLibrary"
# The bundle id `hide_app` addresses through AppKit. Kept as a constant so the
# permission-free route (NSRunningApplication.hide()) does not depend on resolving the
# app by name at call time.
YACREADER_BUNDLE_ID = "com.yacreader.YACReaderLibrary"
YACREADER_PROC_PATTERN = "YACReaderLibrary.app/Contents/MacOS"
JELLYFIN_MIN_EPISODES = 5000       # below this with a healthy mount => gutted DB
SUPERVISOR_POLL_SEC = 5            # poll often so an app auto-relaunched at login over an
                                   # unready mount is stopped within seconds, not tens of seconds
SUPERVISOR_UNHEALTHY_DEBOUNCE = 3  # (retained for compatibility; the supervisor now stops apps
                                   # IMMEDIATELY when the mount is not ready -- no debounce)
SUPERVISOR_READY_DEBOUNCE = 3      # consecutive HEALTHY polls required before starting apps, so
                                   # the mount must be confirmed primed (~15s at POLL=5) first
# The library-dir content check runs with this timeout. Under heavy FUSE load (a
# Jellyfin scan hammering the mount) a readdir can be slow though the mount is
# perfectly fine -- if the check times out we treat the mount as healthy rather
# than kill the apps. Only a mount that is genuinely NOT mounted (fast, no FUSE)
# or that quickly returns empty dirs counts as unhealthy.
MEDIAFS_CONTENT_TIMEOUT_SEC = 5
SUPERVISOR_GUTTED_DEBOUNCE = 2     # consecutive gutted readings before a DB restore
# Jellyfin running but its authenticated API unanswered for this many consecutive
# SECONDS => the API has HUNG (the failure mode where every api_key request times out
# while unauthenticated ones answer) and is restarted. Time-based, not poll-counted:
# a boot that briefly refuses connections (fast fail) and a real hang (each probe
# waits its 10s timeout) both resolve cleanly against one clock. 90s gives a slow
# boot room while a genuine hang is caught within a minute and a half.
SUPERVISOR_UNRESPONSIVE_SEC = 90
# A SCAN is not a hang, and 90s could not tell them apart -- this is the fix for the
# supervisor restarting Jellyfin 17 times (HANDOFF S6). `/Items/Counts` runs a DB-heavy
# query that a real FUSE scan starves past its 10s timeout, so a scanning Jellyfin looked
# byte-for-byte like a hung one. `/ScheduledTasks` answers from memory and reports the
# `RefreshLibrary` task's State and CurrentProgressPercentage, so the two ARE separable.
#
# A running scan defers the restart -- but NOT forever, because a genuinely wedged scan
# reports Running too. The deferral is bounded by this grace, and the grace only holds
# while the scan's PROGRESS PERCENTAGE is still moving: a scan stuck at the same
# percentage for the whole window is wedged and is restarted like any other hang.
SUPERVISOR_SCAN_GRACE_SEC = 3600   # max deferral while a scan reports real progress
# The scheduled-task keys that mean "the library is being scanned". Jellyfin names
# these in /ScheduledTasks; both are DB-heavy enough to starve /Items/Counts.
SUPERVISOR_SCAN_TASK_KEYS = ("RefreshLibrary", "TaskExtractMediaSegments")
SUPERVISOR_LOG_FILE = PROJECT_ROOT / "library_supervisor.log"
SUPERVISOR_LOCK_FILE = STATE_DIR / "library_supervisor.lock"
SUPERVISOR_ALERT_FILE = STATE_DIR / "library_supervisor_ALERT.txt"

# --- YacReader's SQLite index (yacreader_db.py) ------------------------------
# YacReader's library root is the mediafs MOUNT, so the app opens and WRITES its index
# through FUSE while every fleet tool opens the SAME PHYSICAL FILE on the SSD. mediafs
# implements no `lock` operation, so those two writers' locks are in different domains
# and cannot exclude each other -- SQLite's own locking does not protect this file.
# The exclusion is built one level up, on the lock file below. See yacreader_db.py.
YACREADER_DB = COMICS_ROOT / ".yacreaderlibrary" / "library.ydb"   # the SSD path -- ALWAYS
YACREADER_DB_MOUNT = MEDIAFS_MOUNT / "Comics" / ".yacreaderlibrary" / "library.ydb"
YACREADER_DB_LOCK_FILE = STATE_DIR / "yacreader_db.lock"
# How long a tool waits for the app to actually exit after being asked to quit. YacReader
# stops answering AppleEvents while it is scanning, so a graceful quit can time out; the
# helper escalates to a signal only after this.
YACREADER_STOP_TIMEOUT_SEC = 20
# How long a tool waits to acquire the index lock before giving up rather than blocking a
# session forever behind a stuck holder.
YACREADER_DB_LOCK_TIMEOUT_SEC = 120

# --- YacReader scan-at-startup (the freshness half of the contract) ----------
# YacReader only ever adds NEW comics to its index when it RUNS a library update; the app
# never notices the filesystem on its own. On 2026-09-14 the owner's freshly-filed ElfQuest
# was invisible in the reader because the live ini had BOTH auto-update flags `false`
# (they were `true` in the July and Sep-05 backups), and the app had been up since before
# the files landed. So the flags are now a supervised invariant, not a one-time setting:
# the supervisor patches them before every start and restarts the app if it finds them
# drifted, and `record_plan` drops the refresh marker below whenever it files comics so a
# long-running app is bounced once (rate-limited) instead of staying blind for days.
YACREADER_INI = (Path.home() / "Library" / "Application Support" / "YACReader" /
                 "YACReaderLibrary" / "YACReaderLibrary.ini")
YACREADER_SCAN_SETTINGS = {
    "UPDATE_LIBRARIES_AT_STARTUP": "true",
    "UPDATE_LIBRARIES_PERIODICALLY": "true",
    # The interval is an ENUM INDEX, not a duration: 0=30 min, 1=hourly, 2=2 h, ... 6=daily
    # (`YACReader::LibrariesUpdateInterval` in the app's source). 30 minutes is the finest
    # cadence the app offers, and since 2026-09-19 it is the fleet's refresh mechanism --
    # the supervisor no longer restarts the reader for filed comics (a restart lands it on
    # the library chooser where it never scans). Enforced like the booleans, so a drifted
    # interval cannot silently turn indexing off.
    "UPDATE_LIBRARIES_PERIODICALLY_INTERVAL": "0",
}
# Written by `dbhook.record_plan` when a plan filed anything under Comics/. The supervisor
# CONSUMES it without restarting: the app's own periodic update (30 minutes; interval
# index 0 in UPDATE_LIBRARIES_PERIODICALLY_INTERVAL semantics) indexes new comics, and a
# restart would land the app on its library chooser, where it never scans until a human
# clicks Comics (owner decision 2026-09-19). The marker is kept for the health report and
# for a future in-app trigger.
YACREADER_REFRESH_MARKER = STATE_DIR / "yacreader_refresh_request"
# The app can be up with NO library window (a crash restore), in which case
# `LibrariesUpdateCoordinator::init()` never runs and neither does the startup update --
# the app looks healthy and scans nothing. The supervisor activates it, but only inside
# this window after a start: a healthy app begins its update within seconds of starting
# (the transaction journal appears), so a just-started app that is doing NOTHING is the
# one that lost its window. A long-running quiet app is indistinguishable from an idle
# one through this filesystem, and bumping it to the front forever would be churn.
SUPERVISOR_YAC_INDEX_CHECK_SEC = int(os.environ.get("SUPERVISOR_YAC_INDEX_CHECK_SEC", "60"))
SUPERVISOR_YAC_ACTIVATE_WINDOW_SEC = int(
    os.environ.get("SUPERVISOR_YAC_ACTIVATE_WINDOW_SEC", "600"))
# Activation itself is BOUNDED: it brings the reader to the front, and the app it is
# "repairing" may simply have nothing to scan (on 2026-09-15 a stale-index false positive
# kept this branch firing every 60s for hours -- attempt 89 -- stealing the owner's
# screen). Two attempts are enough for a genuine crash restore to get its window; after
# that the alert stands and nothing else touches the app until its next start.
SUPERVISOR_YAC_ACTIVATE_MAX_ATTEMPTS = int(
    os.environ.get("SUPERVISOR_YAC_ACTIVATE_MAX_ATTEMPTS", "2"))
# How long a fleet-started reader may sit VISIBLE before the supervisor hides it even if
# no library update is observed. The update signal hides the healthy case within a tick;
# this fallback covers the app that opens its window (chooser or library) and never starts
# an update -- the owner still does not want that window on screen. Long enough that the
# launch window has certainly been created (hiding mid-creation would race it), short
# enough that a bounce does not leave a popup up for long.
SUPERVISOR_YAC_HIDE_SETTLE_SEC = int(
    os.environ.get("SUPERVISOR_YAC_HIDE_SETTLE_SEC", "30"))
# How long the index may sit unchanged with shelf files missing and no update running
# before fleet_health escalates it from a warning to an ACTION (and the doctor may
# request a rescan). Long on purpose: a pool-backed scan can spend many minutes reading
# one archive before its next commit, and `update_in_progress()` covers that window.
SUPERVISOR_YAC_STALE_ACTION_SEC = int(
    os.environ.get("SUPERVISOR_YAC_STALE_ACTION_SEC", "3600"))
# A YacReader that dies and comes straight back is crashing, not running. Count restarts
# inside this window; at the limit, stop restarting it, say so, and wait out the backoff
# instead of thrashing (the app crashed twice on 2026-09-11 and once on 2026-09-13).
SUPERVISOR_YAC_CRASH_WINDOW_SEC = int(
    os.environ.get("SUPERVISOR_YAC_CRASH_WINDOW_SEC", "600"))
SUPERVISOR_YAC_CRASH_LIMIT = int(os.environ.get("SUPERVISOR_YAC_CRASH_LIMIT", "3"))
SUPERVISOR_YAC_BACKOFF_SEC = int(
    os.environ.get("SUPERVISOR_YAC_BACKOFF_SEC", str(30 * 60)))

# --- Google Drive supervisor (gdrive_supervisor.py) --------------------------
# Light novels land in a Google Drive folder, so the Google Drive macOS app must be
# running and its File Provider mount healthy or every novel placement stalls. This
# daemon (a sibling of library_supervisor) starts the app if it is down and reports
# a mount that never comes up. It does NOT gate the media apps — novels are an
# auxiliary shelf, and a dead Drive must never block Shows/Movies/Comics.
GDRIVE_POLL_SEC = 10
# The Novels root must list (or resolve) like a live mount; a not-running app leaves
# the CloudStorage folder absent or empty, which reads as "not ready".
GDRIVE_READY_DEBOUNCE = 2
GDRIVE_LOG_FILE = PROJECT_ROOT / "gdrive_supervisor.log"
GDRIVE_LOCK_FILE = STATE_DIR / "gdrive_supervisor.lock"
GDRIVE_ALERT_FILE = STATE_DIR / "gdrive_supervisor_ALERT.txt"

# --- MEGA supervisor + trash daemon ------------------------------------------
# The MEGA desktop app backs up the whole Developer directory to the owner's backup account;
# when files get replaced the old versions pile up in that account's rubbish bin (tens
# of GB). These two daemons (a) keep the MEGA desktop app alive and (b) empty every
# MEGA remote's rubbish bin on a schedule so a replace actually reclaims space.
MEGA_APP_NAME = "MEGAsync"
MEGA_BUNDLE_ID = "mega.mac.mega"
MEGA_PROC_PATTERN = "MEGAsync.app/Contents/MacOS"
MEGA_POLL_SEC = 10
MEGA_LOG_FILE = PROJECT_ROOT / "mega_supervisor.log"
MEGA_LOCK_FILE = STATE_DIR / "mega_supervisor.lock"
MEGA_ALERT_FILE = STATE_DIR / "mega_supervisor_ALERT.txt"
MEGA_TRASH_LOG_FILE = PROJECT_ROOT / "mega_trash.log"
MEGA_TRASH_LOCK_FILE = STATE_DIR / "mega_trash.lock"
# How often the trash daemon empties the bins. Deletes are only queued when media is
# replaced/evicted, so a 6-hour sweep is cheap and keeps the bins near-empty.
MEGA_TRASH_INTERVAL_SEC = int(os.environ.get("MEGA_TRASH_INTERVAL_SEC", str(6 * 3600)))

# How many magnets one admission pass will pay a metadata wait for. Each unresolved magnet
# costs a blocking MAGNET_METADATA_WAIT_SEC, so an unbounded pass over a queue holding
# several dead swarms spends its whole cycle waiting and never reaches the records behind
# them -- which is what left a 135 GB pack sitting past its chunk deadline unevaluated.
MAX_MAGNET_METADATA_WAITS_PER_CYCLE = int(os.environ.get("MAX_MAGNET_METADATA_WAITS_PER_CYCLE", "4"))

# Shortest episode title (in words) the epguide will match on. A one- or two-word title
# ("Home", "The End") appears inside unrelated filenames constantly, and a wrong match here
# would mis-file content -- the exact failure epguide exists to prevent. Three is the point
# where a title is specific enough to be trusted.
EPGUIDE_MIN_TITLE_WORDS = int(os.environ.get("EPGUIDE_MIN_TITLE_WORDS", "3"))

# --- ACQUISITION PAUSE -------------------------------------------------------
# A switch that stops ingest registering drops and starting downloads, while the FILING
# path -- identify, plan, stage, verify, cleanup -- keeps running, so anything already on
# disk still reaches ~/Media and the SSD still drains.
#
# It is a pause, not a purge: the journal, the watch folder and qBittorrent are left
# exactly as they are, so lifting the switch resumes precisely where it stopped.
#
#   Pause:  ACQUISITION_PAUSED=1 in the environment, or flip the default below.
#
# NOTE (2026-09-10): auto-DISCOVERY no longer exists -- torrent and comic search was
# removed from the fleet entirely, so nothing generates drops on its own. The only way
# anything enters is the owner hand-dropping a `.torrent` into the watch folder. This
# switch therefore now pauses the owner's OWN drops, which is still occasionally useful
# (a full disk, a MEGA outage) but is no longer the acquisition brake it was built as.
ACQUISITION_PAUSED = os.environ.get("ACQUISITION_PAUSED", "0") == "1"
