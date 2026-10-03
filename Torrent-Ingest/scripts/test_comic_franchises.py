#!/usr/bin/env python3
"""Franchise membership is computed and binding (owner report 2026-10-03).

THE OWNER'S FAULT. `Akame ga KILL!` and `Akame ga KILL! ZERO` were correctly nested
under one franchise master; `Citrus` and `Citrus+` shipped as two top-level folders.
Two mechanisms had to fail for that:

  * `normalize_folder_name` deleted a trailing `+`, so `Citrus+` and `Citrus` collapsed
    to one key -- the sequel could not be represented in `config.COMIC_FRANCHISES` at
    all, nor found by the generator that writes rows from evidence;
  * nothing refused a new plan that files a table-known member outside its master.

This pins the fixes offline:
  Part 1 -- the '+' survives normalization, and the Citrus row resolves.
  Part 2 -- resolution prefers the folder that is ACTUALLY on the shelf, including a
            member folder named for the whole series (`Akame ga KILL! ZERO`), and stays
            ambiguous when two folders share a name.
  Part 3 -- `validate_plan` refuses a member filied outside its master (source-named or
            destination-named) and accepts the nested destination; fails open otherwise.
  Part 4 -- the digest block shows the live folder name, and the generator detects the
            weak prefix shape and proposes it only with AniList relation evidence.
  Part 5 -- the guard replayed over the real journal: prints what it would reject.

    python3 scripts/test_comic_franchises.py

Fixtures only; the live library is never touched. Exit 0 = all checks.
"""
from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import comicfacts                                                    # noqa: E402
import config                                                        # noqa: E402
import library                                                       # noqa: E402
import build_comic_franchises as bcf                                 # noqa: E402

failures: list[str] = []


def check(label, cond):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        failures.append(label)


def cbz(path, names):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for n in names:
            zf.writestr(n, b"page")
    return path


print("Part 1 -- the '+' is part of the title")
check("'Citrus+' normalizes to 'citrus plus'",
      library.normalize_folder_name("Citrus+") == "citrus plus")
check("'Citrus' does not collapse onto the sequel",
      library.normalize_folder_name("Citrus") != library.normalize_folder_name("Citrus+"))
check("a full-width plus is the same series",
      library.normalize_folder_name("Citrus\uff0b") == "citrus plus")
check("a mid-title plus is untouched",
      library.normalize_folder_name("Evangelion - 3.0+1.0") == "evangelion 3 0 1 0")
hit = library.comic_franchise("Citrus+", "manga")
check("Citrus+ resolves to the Citrus row and its own sub-folder",
      hit is not None and hit[0]["name"] == "Citrus" and hit[1] == "Citrus+")
hit = library.comic_franchise("Citrus", "manga")
check("Citrus resolves to the franchise's main member",
      hit is not None and hit[0]["name"] == "Citrus" and hit[1] == "Citrus")
hit = library.comic_franchise("Yashahime - Princess Half-Demon", "manga")
check("Yashahime resolves under the Inuyasha master (no shared prefix)",
      hit is not None and hit[0]["name"] == "Inuyasha"
      and hit[1] == "Yashahime - Princess Half-Demon")

print("Part 2 -- resolution sees the folder that is really on the shelf")
tmp = tempfile.TemporaryDirectory()
try:
    root = Path(tmp.name)
    (root / "Manga/Citrus").mkdir(parents=True)
    (root / "Manga/Akame ga KILL!/Akame ga KILL! ZERO").mkdir(parents=True)
    # The drifted shape is a member folder named for the whole series, not the short
    # suffix the table defaults to. Resolution must land in the real folder.
    (root / "Manga/Fairy Tail/Fairy Tail - 100 Years Quest").mkdir(parents=True)
    saved_comics = config.COMICS_ROOT
    config.COMICS_ROOT = root
    check("a drifted Akame ZERO folder resolves to itself",
          library.resolve_comic_folder("Akame ga KILL! ZERO", "manga") ==
          root / "Manga/Akame ga KILL!/Akame ga KILL! ZERO")
    check("a drifted Fairy Tail spin-off resolves to itself",
          library.resolve_comic_folder("Fairy Tail - 100 Years Quest", "manga") ==
          root / "Manga/Fairy Tail/Fairy Tail - 100 Years Quest")
    check("Citrus+ resolves under the master",
          library.resolve_comic_folder("Citrus+", "manga") ==
          root / "Manga/Citrus/Citrus+")
    check("the canonical member name still resolves",
          library.resolve_comic_folder("Ace's Story", "manga") ==
          root / "Manga/One Piece/Ace's Story")
    # Two folders with one normalized name prove nothing: gate (a) must stay shut.
    (root / "Manga/Shaman King").mkdir(parents=True)
    (root / "Manga/Shaman King/Flowers").mkdir()
    (root / "Manga/Shaman King/Extras/Flowers").mkdir(parents=True)
    check("two same-named member folders are ambiguous (None)",
          library.resolve_comic_folder("Shaman King Flowers", "manga") is None)
    block = library._franchise_block()
    check("the digest block shows the live Akame ZERO folder",
          "Akame ga KILL! ZERO" in block and ", ZERO," not in block)

    print("Part 3 -- the validator refuses a member outside its master")
    src = cbz(root / "Citrus+ v01.cbz", ["Citrus+/001.png"])
    comicfacts.VOLUME_MAP_PATH = root / "map.json"
    comicfacts.VOLUME_MAP_PATH.write_text(json.dumps({"version": 1, "series": {}}))

    def plan_for(source, dst):
        return {"media_type": "comic", "title": "Citrus+",
                "files": [{"src": str(source), "dst_rel": dst}]}

    refused = None
    try:
        library.validate_plan(
            plan_for(src, "Comics/Manga/Citrus+/Citrus+ v01.cbz"), str(root))
    except library.PlanError as exc:
        refused = str(exc)
    check("a top-level Citrus+ destination is refused",
          refused and "outside its master folder" in refused)
    accepted = library.validate_plan(
        plan_for(src, "Comics/Manga/Citrus/Citrus+/Citrus+ v01.cbz"), str(root))
    check("the nested destination is accepted", accepted.get("media_type") == "comic")

    bare = cbz(root / "Chapter 5.cbz", ["005.png"])
    refused = None
    try:
        library.validate_plan(
            plan_for(bare, "Comics/Manga/Citrus+/Citrus c0005.cbz"), str(root))
    except library.PlanError as exc:
        refused = str(exc)
    check("a bare-chapter source is judged by the destination folder",
          refused and "outside its master folder" in refused)
    ok = library.validate_plan(
        plan_for(bare, "Comics/Manga/Citrus/Citrus/Citrus c0005.cbz"), str(root))
    check("an unknown source series falls back to the destination folder",
          ok.get("media_type") == "comic")
    other = cbz(root / "A Silent Voice v01.cbz", ["A Silent Voice/001.png"])
    ok = library.validate_plan(
        plan_for(other, "Comics/Manga/A Silent Voice/A Silent Voice v01.cbz"), str(root))
    check("an ordinary top-level series is untouched",
          ok.get("media_type") == "comic")

    print("Part 4 -- the generator detects the weak shape, AniList confirms it")
    pairs = set(bcf._weak_prefix_pairs(
        ["Citrus", "Citrus+", "Berserk", "Berserk of Gluttony", "Akira"]))
    check("Citrus and Citrus+ are a weak-prefix candidate pair",
          ("Citrus", "Citrus+") in pairs)
    check("the unrelated one-word pair is only a candidate, not a group",
          ("Berserk", "Berserk of Gluttony") in pairs)
    saved_related = bcf._anilist_related
    bcf._anilist_related = lambda term: (
        [("SEQUEL", "Citrus+")] if bcf._norm(term) == "citrus" else [])
    check("AniList's SEQUEL relation confirms Citrus/Citrus+",
          bcf._relation_confirmed("Citrus", "Citrus+"))
    check("no relation means no group (Berserk stays two series)",
          not bcf._relation_confirmed("Berserk", "Berserk of Gluttony"))
    # A pair with NO shared prefix: only AniList relations tie Inuyasha to Yashahime.
    def _fake_related(term):
        key = bcf._norm(term)
        if key == "inuyasha":
            return [("SEQUEL", "Yashahime - Princess Half-Demon")]
        if "yashahime" in key:
            return [("PREQUEL", "Inuyasha")]
        return []

    bcf._anilist_related = _fake_related
    rel = bcf._relation_pairs(["Inuyasha", "Yashahime - Princess Half-Demon"], pace=0)
    check("relation-only pairs are found without any shared prefix",
          rel.get("Inuyasha") == ["Yashahime - Princess Half-Demon"])
    bcf._anilist_related = saved_related
    row = bcf._row("Citrus", ["Citrus", "Citrus+"], "manga")
    check("the generated row's member value keeps the full 'Citrus+' name",
          '"citrus plus": "Citrus+"' in row)

    print("Part 5 -- replay the guard over the real journal")
    last = {}
    try:
        for raw in (config.STATE_DIR / "journal.jsonl").read_text(
                encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if rec.get("info_hash"):
                last[rec["info_hash"]] = rec
    except OSError:
        last = {}
    replayed = 0
    hits = []
    for ih, rec in last.items():
        files = (rec.get("plan") or {}).get("files") or []
        if not files:
            continue
        replayed += 1
        try:
            library._reject_franchise_member_outside_master(files)
        except library.PlanError as exc:
            hits.append(f"{rec.get('name', ih)[:60]}: {exc}")
    print(f"  replayed {replayed} historical plan(s); the member guard would reject "
          f"{len(hits)}")
    for h in hits[:6]:
        print(f"    {h}")
finally:
    config.COMICS_ROOT = saved_comics
    tmp.cleanup()

print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("ALL COMIC FRANCHISE CHECKS PASSED")
