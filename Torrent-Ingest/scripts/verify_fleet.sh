#!/usr/bin/env bash
# ===========================================================================
#  verify_fleet.sh -- every read-only check the hand-off's §0.3 asks for, in
#  one command, before you trust a change.
#
#      bash scripts/verify_fleet.sh
#
#  Read-only by construction: it imports modules, replays history, and audits
#  configuration. It never writes to ~/Media, never calls apply_plan, and never
#  hammers a live source. Exit 0 means every check passed.
# ===========================================================================
set -uo pipefail

DEV="$HOME/Developer/Media-Orchestrator"
PY_INGEST="/opt/homebrew/Caskroom/miniconda/base/envs/torrent_ingest_env/bin/python3"
PY_BASE="/opt/homebrew/Caskroom/miniconda/base/bin/python3"
[ -x "$PY_INGEST" ] || PY_INGEST="$(command -v python3)"
[ -x "$PY_BASE" ]   || PY_BASE="$(command -v python3)"

fail=0
run() {  # run <label> <command...>
  local label="$1"; shift
  printf '%-46s' "$label"
  if out="$("$@" 2>&1)"; then
    echo "OK"
  else
    echo "FAILED"
    echo "$out" | tail -12 | sed 's/^/      /'
    fail=1
  fi
}

echo "=============== FLEET VERIFICATION ==============="
run "Torrent-Ingest modules import" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" -c "import config,library,identify,fastpath,ingest,journal,direct_ingest,direct_ingest_bridge,acceptance_gate,plan_coverage"
run "Torrent-Ingest plan API contract" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" contract.py
run "YouTube-Downloader preflight" \
    env -C "$DEV/YouTube-Downloader" "$PY_INGEST" preflight.py
run "Media-Syncer modules import" \
    env -C "$DEV/Media-Syncer" "$PY_BASE" -c "from scripts import config, utils"
run "AI is free-only" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/audit_free_only.py
# ffprobe is how the AI runtime's `Probe` tool reads a container, and how new .nfo files
# get their <streamdetails>. A homebrew x265 bump leaves ffmpeg linked against a
# libx265 that is no longer installed, and every invocation then dies in dyld -- silently,
# because nothing else fails when Probe returns nothing. Cheap to assert, so assert it.
run "ffprobe is usable" \
    ffprobe -version
run "placement guards vs. full history" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_placement_guards.py
# A plan that names PART of a release was permission to delete the rest: the Smurfs
# lost 365 files / 31 GB to a 40-file plan, Doctor Who (2005) lost 38 files / 60.9 GB
# to a wave's "not in plan (junk)" branch. The coverage contract parks the release
# unless every medium is filed or provably junk, the collision collapse parks instead
# of dropping, and a release whose own name states a year cannot be filed into another
# year's series. Part 5 replays every historical plan that still has its .torrent and
# PRINTS the would-park count, because a guard that rejects real history is worse than
# the bug it fixes (§4.114).
run "plan coverage: no partial plan may delete (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_plan_coverage.py
# `find_drop_files` reads only the watch root's TOP level, so a `.torrent` filed
# into queued//ingesting/ is never seen again: an iCloud "X 2.torrent" duplicate or
# a terminal record's leftover source had no path out, and two One Piece files sat
# there for a month (10.6). The sweep files terminal sources away, files live
# duplicates under finished/, adopts the survivor when the recorded copy is gone,
# and never deletes or touches a source it cannot parse.
run "orphaned sources get a way out of queued/ (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_orphan_sources.py
run "acceptance gate vs. full history" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_acceptance_gate.py
# The gate's `.torrent` half. §4.120 twice over: a gate on ONE of two drop paths goes dark
# the moment traffic moves to the other, and it did, in both directions. This asserts the
# second path still reaches the gate, that ingest's .torrent reader still agrees with the
# searcher's (two readers of one format drift), and that a refusal BEFORE the add never
# asks qBittorrent to remove a torrent it never had.
run "acceptance gate, .torrent path (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_torrent_gate_path.py
# A hostile `.torrent` -- traversal, absolute path, executable -- must be refused before
# qBittorrent is asked to add it. This check lived ONLY in the searcher, which judged every
# drop it made; the searcher was removed on 2026-09-10 and hand-dropping is the fleet's only
# admission path, so deleting it without porting this would have taken the check away from
# the one route that still admits anything (§4.22, with the traffic already shifted). Both
# ways: every hostile shape refused, AND every real `.torrent` on disk still accepted.
run "hostile .torrent metadata refused (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_torrent_metadata_safety.py
# A cache can serve a `.torrent` cut short. The searcher used to notice and write a
# sibling `.magnet`, and the searcher is gone, so a hash-named truncated drop must now
# recover itself: the filename IS the info hash, and qBittorrent can pull the real
# metadata from the swarm. Both ways, because a recovery that fires on every hash-named
# drop -- or on a drop still syncing from iCloud -- would trade a dead file for a dead
# magnet and call it a fix. The salvage is checked against a real torrent used as its
# own oracle, with its name and trackers cut away in the middle.
run "truncated .torrent recovers as a magnet (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_truncated_torrent_recovery.py
# A private trackerless `.torrent` has every peer-discovery channel switched off: DHT,
# PeX and LSD are forbidden by the private flag (qBittorrent prints "This torrent is
# private" for all three) and no tracker or web seed is there to replace them. Such a drop
# can never connect, so it sat until the 24h stall clock abandoned it and a re-drop
# repeated the day (SpongeBob S16, 2026-09-24 -- the failure was NOT its iCloud duplicate).
# Registration now refuses it before qBittorrent is asked to add it. Both ways: every
# private trackerless shape refused, every public DHT-only drop and every tracked private
# drop -- the shapes that must keep working -- accepted, and the real corpus replayed to
# prove a public or tracked torrent is never refused.
run "private trackerless .torrent refused at registration (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_undownloadable_torrent.py
# The duplicate-provider-id check is a check that reports NOTHING almost all the time, so
# it must be able to prove it CAN report something (§ diagnosis 7). Fixture-based: two
# series stamped with one key are found, a clean library is not.
run "duplicate series-key check (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_duplicate_series_keys.py
# The index lock is what stops YacReader (writing through FUSE) and a fleet tool (writing
# the same physical file on the SSD) from overlapping, which corrupts the database. If
# `is_held()` could never be true the supervisor would start the app straight onto a tool's
# edit, and the only symptom would be an index that goes bad now and then. Temp-file based,
# both directions, cross-process.
run "YacReader index lock (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_yacreader_lock.py
# A blocklist row that matches nothing is indistinguishable from no row at all, and both
# look exactly like a finished purge -- that is how 'Saiki? no' hid a stalled purge for
# weeks (§4.186). The audit that finds them is only trustworthy if its ORPHAN verdict can
# actually fire, and its corpora are live mutable state (§4.114), so the verdicts are
# asserted against FIXTURES here: all four reachable, including the real §4.186 string and
# a control proving the prefix matcher still matches.
run "blocklist orphan verdicts (fixtures)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/audit_blocklist_orphans.py --selftest
# The backup census has one branch that has NEVER fired: 6 of 7 backups are clean, so
# "no clean backup exists" has never been printed -- and that is the only state from which
# a corruption is unrecoverable. Fixture-based, because the live index cannot exercise it.
run "YacReader backup census (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_yacreader_backup_census.py
# A filed comic is invisible in YacReader until the APP runs a library update, and the
# only reliable trigger is `UPDATE_LIBRARIES_AT_STARTUP` in its own ini. On 2026-09-14
# both auto-update flags read `false` and every ElfQuest file -- on the mount, in the
# pool -- did not exist to the reader. The supervisor now enforces the flags and consumes
# a refresh marker `record_plan` drops; this asserts the ini surgery is exact (all other
# lines survive) and idempotent, and that the production callers patch under the lock.
run "YacReader scan-at-startup enforced (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_yacreader_scan_config.py
# The flag alone is not enough: a crash restore leaves YacReader UP WITH NO WINDOW, so
# `LibrariesUpdateCoordinator::init()` never runs and the startup update never fires —
# the app looks healthy while scanning nothing (measured 2026-09-14). The supervisor
# activates it, and backs off with an alert when the app is crash-looping instead of
# restarting forever. Driven with fake app control and a fake clock, both directions.
run "YacReader supervisor freshness (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_supervisor_yacreader.py
# The fleet bounces YacReader on its own schedule (every comic filing) and `open -g`
# stops focus-stealing but not the window APPEARING, so the owner reported it "keeps
# popping up and taking over the whole screen" (2026-09-19). Every fleet start/activate
# now ends in `yacreader_db.hide_app()`, which uses AppKit's NSRunningApplication.hide()
# through AppleScriptObjC FIRST -- no Accessibility grant needed, unlike the System
# Events fallback -- and the supervisor re-hides through a bounded window. Faked
# subprocesses: the route order, the fallback, and the fail-soft contract.
run "YacReader stays hidden after fleet starts (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_yacreader_hide.py
# The outside view: a crash row, a damaged index, a windowless app, drifted flags and
# shelf files the index lacks each get the severity that matches who can fix them, and
# `fleet_doctor` has reviewed remedies for the auto-fixable two. The detector is driven
# with fakes in both directions -- a checker that only ever said ALL CLEAR would pass on
# a good day and prove nothing (2026-09-14: ElfQuest invisible with no warning anywhere).
run "YacReader health check + remedies (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_fleet_health_yacreader.py
# `FolderModel::createModelData` dereferences the parent it looks up `ORDER BY
# parentId,name` with no null check, so a dangling parent / cycle / missing root / a
# parent that sorts after its child is a SIGSEGV inside the app -- the FolderModel::reload
# crash of 2026-09-13 that left the index stale for ten hours. Fixture-based in both
# directions: each crash shape is named, a healthy tree stays clean, the repair leaves a
# loadable tree, and the freshness report sees shelf files the index lacks.
run "YacReader index crash shapes + freshness" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_yacreader_index_shape.py
# The two AI prompts that choose WHAT TO ACQUIRE are the only remaining open-world paths,
# §4.146: a repair tool once REPORTED 250 repairs it never made, so the acceptance bar for
# the guide-first filler is "prove each claimed repair changed a file", never a count. This
# re-reads every sidecar off disk and asserts the returned number equals the files that
# really changed -- and that a half-covered episode (title, no summary) is left for the AI
# rather than written into a LOCKED sidecar nothing will revisit.
run "repair fills from the guide first (verified)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_repair_guide_first.py
# The iCloud control directory holds new.txt -- the ONLY admission path. Its census is the
# only thing that can ever explain a §4.13 vanishing, because the unified log retains ~9h.
# A loss must be reported AND growth must not be, or the log is noise nobody reads.
run "iCloud census drop detection (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/icloud_census.py --selftest
# Direct ingest is the fleet's non-torrent admission path, and since 2026-09-13 it takes
# video and dropped directories too. The dangerous half is the empty plan: over a single
# archive it is a verdict, but over video or a directory it used to mean deletion on a
# free model's word -- the "That 90s Show" class one layer out. Both directions: proven
# already-present is deleted, unproven is parked intact.
run "direct ingest: all media, safe empty plans" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_direct_ingest_media.py
# The iCloud Torrents/DirectIngest bridge crosses a sync layer and a volume: a dataless
# placeholder must be materialized and settled before the move, a same-named local file
# must never be clobbered, and a failed copy must leave the iCloud source intact.
run "direct ingest bridge: move, never clobber" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_direct_ingest_bridge.py
# The identify base prompt may be SCOPED by media kind but never shortened by deletion: it
# has zero verbatim repetition, so every character removed by editing is a rule removed, and
# a placement rule cannot be regression-tested without spending the daily budget the shrink
# exists to save. The load-bearing assertion is that the union of the kind variants is the
# WHOLE prompt -- that is what makes it a scoping and not a quiet deletion.
run "identify prompt scoping (nothing deleted)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_prompt_scoping.py
# identify and every auxiliary AI caller share three free accounts, and only two of them can
# run an identify prompt at all (groq's ceiling is 26,367 chars against a 58,513-char floor).
# `ai_budget_healthy()` read True with BOTH of identify's providers capped, so the searcher
# was free to drain them at the daily reset while downloads sat unfiled. The policy lives in
# one module and each repo wraps it; this asserts the two wrappers still agree, because a
# gate whose two halves disagree is the silent failure this fleet keeps paying for.
run "AI budget reservation (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_ai_budget_contract.py
# fleet_doctor acts on fleet_health findings unattended, and for an unrecognised finding a
# free model picks which repair runs. The containment is that it can only ever NAME one from
# a closed registry -- so this reads the source of every remedy and fails the build on a
# destructive operation, with a control proving the scanner can still catch one.
# The MEGA free-space cache goes stale for the whole of a purge because the reaper kills
# Media-Syncer on purpose, so for days at a time fleet_health reported a fault that was the
# fleet working correctly -- and a warning that is always on is one nobody reads. The pause
# is now subtracted, which means the check must still catch BOTH things that hides: a
# genuinely stuck sync loop, and a LEAKED pause (marker present, reaper gone) that leaves
# replication stopped forever and previously had no detector at all.
# A multi-episode file (`S01E01-E02`) carries ONE .nfo naming only its FIRST episode, so
# reading that bare number as the file's whole claim made every SECOND slot of every pair
# file report as a placement fault -- ~37 false items on The Powerpuff Girls alone, each
# telling the owner to re-file a correctly-placed file. §4.26 one level deeper: that lesson
# fixed a check reading the FILENAME instead of the file's own record; this fixes the same
# check reading that record without understanding what it is a record OF. Both ways: a
# legitimate pair file is clean at BOTH its slots, and a genuinely misfiled file (including
# one whose .nfo puts it in another season) is still caught.
# One Pace S13E05 held two files whose sidecars BOTH said "Quack Doctor", so the collision
# check could not say which was wrong. The newer cut had inherited the pre-seeded sidecar's
# title when it was filed into the slot. `_title_is_janky` cannot see this -- the title is
# perfectly good, just another episode's. The tie is broken by a THIRD witness (the ingest
# journal), not by preferring the filename, so §4.26 still stands. Both ways, and the
# second half matters more: a detector this eager would rewrite hundreds of correct titles
# if it mistook sanitised characters, truncation or case for a contradiction.
run "sidecar title contradiction (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_sidecar_title_contradiction.py

run "Season-0 specials stay locked (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_specials_locked.py

run "One Pace re-cut replaces, not drops (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_onepace_recut_replaces.py
run "multi-episode .nfo coverage (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_multi_episode_span.py
run "MEGA staleness vs. the reaper pause (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_mega_pause_check.py
# rclone.conf is where ~800 MEGA accounts live, and the reaper's 16-way probe heals dead
# sessions by rewriting it. Its old private copy was a lockless non-atomic read-modify-write;
# on 2026-09-13 two probe workers raced it and ZEROED the file, and every remote then read
# as dead. Asserts a strip removes one session and nothing else, that 22 concurrent strips
# cannot tear or zero it, and that the reaper goes through the locked primitive.
run "rclone.conf session strip never loses an account" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_rclone_conf_strip.py
run "self-healing remedies are non-destructive" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_remedies.py
# A chunked pack's `chunk_done` is a claim about the PAST, and a re-drop by hand usually
# means the content is GONE -- so carrying that claim turns the owner's re-acquisition into
# a silent no-op that reports COMPLETED (§4.31). Asserts progress is carried only where it
# is PROVABLE on the mount, and that a pack which proved nothing FAILS -- with controls,
# because a gate that fires on everything is not a gate.
# A chunked pack is STOPPED between waves while the finished wave is filed, so
# qBittorrent's `last_activity` -- the stall clock -- goes stale by design. The next wave
# then inherited that clock and was destroyed seconds after resuming: Monogatari (103
# files, 75 GB) was enabled at 09:11:55 and failed at 09:12:16 as "stalled 8h with no
# progress (no seeders/peers)" against a swarm of 450 seeders, with delete_files=True.
# And the 4h "no complete copy" deadline was selected by `availability < 1`, which is a
# LOCAL connected-peers fact that reads < 1 during every stall -- four slow-but-alive
# Bob's Burgers packs were killed after an overnight lull on 2026-09-23 and their partial
# payloads deleted with the torrent. The deadline now runs from the later of
# `last_activity` and the wave's own start, one 24h deadline applies, and an abandon KEEPS
# the partial bytes. Both ways, because a stall guard that cannot fire lets a dead torrent
# pin the download budget forever -- the exact deadlock it was written to break.
# Monogatari was run deliberately as the hardest naming case in the library, and the free
# identify chain failed it in a way nothing checked: one 26-episode arc filed across six
# season folders as absolute episodes 1-23, leaving Season 09 holding 18,19,20,21,23 and
# Season 10 holding only 22. Every file had a correct title and plot; the PLAN was
# incoherent. §4.4 -- the harness disposes. Both ways, and Part 3 is the load-bearing
# half: zero rejections over all 783 completed plans in the journal, because a placement
# guard that rejects real history stops the fleet filing anything.
# Which folders share a filename label, and whether their episode numbers form one
# continuous run across them, is arithmetic -- so the harness computes it and hands the
# model the finding as a fact instead of making it spot the conflict inside a 90,000-char
# prompt. That conflict is exactly what Monogatari failed on. Both ways: it fires on the
# real Monogatari layout naming the right folders, and stays silent on per-season folders,
# two-show packs, overlapping disc splits, and flat releases.
# The whole-library shows digest is ~32,000 chars of ~300 folders, sent on EVERY identify
# call -- so placing an anime pack shipped The Office's season breakdown to a provider on a
# daily budget. It is now scoped by name relevance. Both ways, and the second half is what
# protects correctness: it must FAIL OPEN (no hint, or a hint matching nothing, yields the
# byte-identical full digest), and every folder must still be named even when its detail is
# dropped, so nothing in the library can become invisible to the model.
run "library digest relevance scoping (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_digest_scoping.py
# The digest is the identify run's picture of the library, and that picture was reading
# the SSD alone -- where anything evicted to the pool is simply absent (HANDOFF §2.1).
# Measured 2026-09-24: The Simpsons held 40 episodes and Season 03 held 4, the SSD read
# said "Season 03 (2 eps)" and no other seasons, and the run -- told the counts were
# ground truth -- renumbered the next wave down by two onto occupied slots. The collision
# guard dropped those files and the coverage contract parked the whole 700 GB pack as
# FAILED; American Dad! parked the same hour (SSD: no seasons; library: 34 episodes).
# The summary now counts the Media-Syncer inventory's episodes too (the same complete
# view `_comics_coverage` reads), with the disk walk for not-yet-uploaded files and
# fixtures. Both ways: an evicted episode is counted and its local sidecar still decides
# locked/blank, a legacy cache entry cannot serve its subset, an unreadable inventory
# falls back to the walk, and one show's inventory never bleeds into another's counts.
run "show summary counts evicted episodes (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_show_summary_inventory.py
# The most authoritative-sounding line in the identify prompt used to be the wrong one:
# the retired searcher's stored file->item mapping, rendered as "reuse this mapping; do NOT
# re-derive the numbering". Monogatari's stored mapping keeps one absolute run across
# seasons 4,5,7,8,9 and strands episode 22 in season 10 -- the exact shape the harness
# rejects, and the exact layout that got filed. Nothing produces or re-checks these maps
# now. A stored plan is evidence; an incoherent one is withheld entirely.
# Identify runs were ending rc 0 with NO plan file -- the one failure that tells the
# harness nothing, because there is no wrong answer to reject and feed back. The runtime's
# own turn log explained it: six identical Greps in a row, then more, until all 40 turns
# were gone. A model does not track its turn count and cannot tell a result is one it
# already has; both are now said to it out loud. Both ways, and Part 2 matters most: a
# breaker this eager would corrupt ordinary varied work.
run "agent turn budget + tool loop breaker (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_agent_loop_guard.py
run "stored plan is evidence, not instruction (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_stored_plan_coherence.py
# The fleet already holds the provider's season shape -- epguide.season_shape() is cached
# on disk and used by the metadata repair -- and identify never saw it, so every run
# re-derived the season layout by web search and still got a boundary wrong (Nekomonogatari
# (Black) filed into the season the provider gives Tsubasa Tiger). Both the mistake and its
# answer were one lookup away. Best-effort by contract: an unknown title, or a guide that
# raises, must yield nothing and leave the run exactly as it was.
run "provider season shape in the prompt (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_provider_season_block.py
run "release structure conflict detector (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_release_structure.py
# The three HANDOFF §6 limits that turned out to be defects rather than facts of life.
# Each of these tools was either confidently WRONG (the arc sampler), actively misfiring
# (the supervisor, 17 Jellyfin restarts) or silently inflating (library.db, 25.9% duplicate
# rows). Their fixes are cheap to assert and expensive to regress, so they are gated.
run "a library scan is not a hang (supervisor)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_supervisor_scan_grace.py
run "arc mapping censuses, never samples" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_arc_mapping_census.py
run "library.db upsert + reconcile guards" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_library_db_reconcile.py
# A purge that leaves library.db claiming the content makes the acceptance gate refuse
# the title's own re-drop. The reaper supersedes the rows for the paths it VERIFIED gone;
# the fixture proves item-level matching (one episode, not its siblings), both series rows
# of a duplicated norm, loose and foldered films, comics by folder chain and by marker-less
# stem, and that a collection is dropped only when exactly one row could be meant.
run "a verified purge supersedes its library.db rows" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_purge_db_sync.py
# `record_plan` used to file EVERY comic as kind `manga`, so each library-seeded western
# series acquired a duplicate `manga` twin -- 25 live norm pairs by 2026-09-14, and the
# reason the identify model split the ElfQuest re-acquisition across `Comics/ElfQuest`
# and `Comics/Manga/ElfQuest`. The reaper now folds the pairs after every purge; this
# asserts the fold follows the POOL, never loses ownership in the merge, and REFUSES on
# ambiguity (no pool files, or files under both roots) -- a false supersede reads as
# "not owned" and invites a re-download of content already held.
run "comic kind splits fold + fail open (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_comic_db_reconcile.py
# The chapter/volume reconciler deletes files a volume already contains, so every rule has
# both directions: covered chapters go, uncovered/unknown/volume-absent/keep-listed ones
# stay. It also pins the two live mistakes this feature already made -- the leaf-folder
# name "Restoration" matching an unrelated manga (the identity is the folder chain) and the
# in-process AI call pacing a rate-limit window for minutes (it is a kill-bounded
# subprocess now).
run "manga chapters yield to volumes (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_manga_chapter_reconcile.py
# The manga tiers are COMPUTED from the archives (HANDOFF 10.5a-d): volume ceiling,
# volume->chapter sets, colour from entry names, and a mislabel that is refused and
# repairable. This is the guard that makes "One Piece v1176 should be c1176" a fact.
run "manga tiers are computed from the archives (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_manga_mislabels.py
# Citrus and Citrus+ shipped as two top-level folders while Akame ga KILL! + ZERO were
# correctly nested (owner report 2026-10-03). A trailing '+' is part of the title, not
# punctuation; the Citrus row resolves to its member folder; resolution prefers the
# folder actually on the shelf (Akame ga KILL! ZERO); and validate_plan now refuses a
# table-known member filed outside its master. The generator detects this weak prefix
# shape and proposes it only on AniList relation evidence.
run "franchise membership is computed and binding (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_comic_franchises.py
# A release that names every part of a story with the same `SxxEyy` (the SERIAL) was
# misfiled twice by two different models: once on the original ingest, then again on the
# re-fetch waves (28 files). The harness now COMPUTES the broadcast numbers from the
# release's folder ranges and makes them binding in validate_plan. This asserts the
# arithmetic against the handoff's independently confirmed slots, the real filename
# shapes from later seasons, and both directions of the guard -- an ordinary release
# produces no map, so the guard cannot touch normal plans.
run "serial-numbered releases compute their numbering" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_serial_release_numbering.py
# Release-order packs (The Smurfs: `S01E01 (The Smurfette)`, broadcast S01E31) compute
# their broadcast slots from their own titles, the harness hands a skeleton to large
# plans, and a truncated plan's missing slice is named before the run ends (HANDOFF 10.9).
# Part 5 pins that the map is computed against the provider JELLYFIN SHOWS (TMDB, the
# pinned id), not TVMaze -- the two disagree on real numbering and a TVMaze map placed 77
# Smurfs files in slots Jellyfin named differently.
run "release-order titles compute broadcast numbering" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_release_title_numbering.py
# A same-stem duplicate (`E07.mkv` + `E07.mp4`) shares ONE .nfo, so the sidecar can never
# separate the files -- and on 2026-09-20 the duplicate rule deleted the planner's copy at
# 38 Smurfs S01 slots while the older wrong-slot file survived. The journal's per-file
# source title is the third witness: a disagreement is a placement fault to re-file, and
# only a proven same-episode pair may be auto-deleted.
run "duplicates are deleted only on proven identity (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_duplicate_identity.py
# The same-slot collision scan read the SSD only, so an EVICTED episode (the Smurfs'
# 40 pool-only dvdrip S01 files) was invisible and the replacement pack applied beside
# it. The scan now reads the MOUNT too, and a colliding file whose journal-recorded
# content is a DIFFERENT episode parks the release instead of silently dropping the
# planned copy. Same episode and unknown identity keep the historical collapse.
# A chunked wave's filings had NO journal record (`plan` is null), so the identity
# witness was blind to them -- exactly the files most likely to collide with the next
# wave. The record's `applied` entries are indexed now, and when the journal is still
# silent the existing FILE's own name is the witness (on tag-cleaned titles, because
# two files of one release share their whole tag tail and raw similarity lands 0.838,
# a hair under the same-episode bar). A bare-numbered name still proves nothing.
run "same-slot collisions see the mount and check identity (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_existing_collision_identity.py
# A provider id must name the show the plan says it does (HANDOFF 10.3). TZ (2019) was
# filed with the TMDB id of *Too Cute* and the wrong TVDB id, and the wrong id authored
# the nfo and picked the cover. A contradicted id is stripped; a network error fails open.
run "provider ids are verified before they can pick art (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_provider_id_verify.py
# Series-level identity and title art self-heal (HANDOFF 10.3). The doctor never looked
# at tvshow.nfo identity fields or folder/landscape/season posters before this, which is
# why TZ kept the Too Cute cover through every refresh. Trigger is computed against TMDB
# (`premiered` vs `first_air_date`); a stale `<year>` with a matching premiere does not
# fire (four live shows carry that harmless shape).
run "series identity + title art heal (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_series_identity_heal.py
# The arc->season mapping the harness now COMPUTES, and the two guards that enforce it.
# This is the acceptance gate's own check: Monogatari failed three runs because nothing
# married the release's arcs to the provider's seasons, and the counts lined up perfectly
# while the arcs were wrong. Part 4 replays every historical plan through both guards --
# a guard that rejects real content is worse than the bug it fixes (§4.114).
run "arc -> season mapping + guards (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_arc_mapping.py
# Model ids rot -- five of the eight a careful reader would have written down on
# 2026-09-12 were already dead (HTTP 404/410). The chain now discovers a live replacement,
# PROBES it with a real tool call, and records it in an overlay. The safety property is the
# one this asserts: a retired model is told apart from a busy or rate-limited one, so a 503
# under load can never silently move the fleet off the ids a human chose.
run "retired model ids heal themselves (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_model_retirement.py
run "absolute run split across seasons (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_absolute_run_split.py
run "chunked wave stall clock (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_chunked_stall_clock.py
# A parked pack colliding with a duplicate whose library footprint is a PROVEN uniform
# episode shift is resolved automatically: the displaced footprint is superseded through
# the sanctioned purge path and the duplicate is retired REFUSED, payload kept. Every
# weaker shape ({shift 0, mixed shifts, cross-season, unconfirmed titles, a coverage gap,
# two candidates}) must leave the park standing, so the test pins both directions.
run "displaced duplicate packs resolve, or fail open (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_pack_conflict.py
run "chunked progress is proven, not remembered (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_chunk_progress_proof.py
# The English-only rule is enforced by two `parse` gates, and both leaked:
# a Japanese-market manga edition redeemed itself with its own Latin gloss, and a bare
# scene tag ("... FRENCH") was foreign only when suffixed `-dub`. The bare-tag rule has to
# be POSITIONAL because a language word is also a title word, so this carries the
# false-positive controls (*The Italian Job*) and replays both gates over the real journal
# to prove the new rules refuse nothing the old ones accepted.
run "English-only language gates (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_language_gate.py
# Metadata self-heal must SEE a pool-only show (the audit read the local SSD and Toriko
# was evicted), accept name-only guide rows, have a synopsis source, and charge the AI
# escalation budget only when the sidecars actually change (HANDOFF 10.4).
run "metadata heal sees the mount and verifies writes (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_metadata_heal.py
# The doctor's repair loop verifies its own postcondition and remembers what it
# proved. An episode image whose provider-best answer is already applied (or absent)
# is refused per image for a TTL instead of re-adopted every pass -- the provider
# re-uses one still across episodes it has none for, so "re-adopt" rewrote the same
# bytes forever. A provider LOOKUP failure is never remembered, a NEW provider still
# is adopted at once, and a stuck `[auto]` line is persisted, demoted to NEEDS REVIEW
# after STUCK_AFTER_PASSES, and forgotten when it stops being reported (the counters
# were advanced in the report, which ran after the state save, so they never survived).
run "the doctor's art repair and stuck reporting hold (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_media_doctor_repair.py
# This repository is public. It used to track the MEGA account pool (822 remotes with
# user/pass) and a live Jellyfin API key in nine plists; both moved to machine-local
# `.env`/an untracked conf on 2026-09-19. A `.gitignore` rule is not a guard against
# the next session pasting a key into a plist, so this scans exactly what a commit
# would contain for a tracked secret store or a credential-shaped value.
run "no tracked secrets in a public repo" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_no_tracked_secrets.py
# The owner report once carried a show folder, two poster md5s and a torrent hash in its
# source: incident evidence that cannot see the same fault elsewhere and rots. Comments
# and docstrings may still name incidents (the why-comments are the codebase's memory);
# executable string literals may not carry machine paths or digests. one-off/, archive/
# and tests are dated records/fixtures and exempt.
run "no incident hard-coding in shipped code" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_no_incident_hardcoding.py
# `reconcile` re-queues a completion it cannot find, so a blind witness re-downloads the
# library. It was blind twice on 2026-09-20: the SSD-only check said "gone" for files the
# mount still served, and a library-internal move (the One Piece franchise migration, 191
# files) left every applied path pointing at a vacated key -- 132 chapter completions were
# re-queued, re-fetched and re-filed on a loop, and the five covered ones were purged
# again by the chapter reconciler. Presence now counts the MOUNT and matches a moved file
# by its exact content identity (basename + byte size, both carried by the inventory), and
# a completion whose content library.db records as deliberately superseded is CLOSED
# instead of re-acquired. Both directions: a different-size same-name file is not a move,
# an owned or unknown item still re-queues, and the repair tool never closes a record that
# still holds a file. The replay over state/journal.jsonl prints the live counts.
run "a completion is re-queued only on every-witness loss (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_reconcile_presence.py
# Family Guy (S07E07) ships ONE episode twice under one release key (Uncensored + Bale
# Scene beside Uncensored + Commentary), the model names one, and the other parked a
# 95 GB pack. The harness now computes same-key + same-cleaned-title pairs as alternates
# of one episode, ranks the survivor and records the sibling as accounted-for; the merge
# attributes a re-typed `src` by parent path instead of basename alone (Friends lost 4 of
# 32 Featurettes to `Friends of Friends_new.mkv` existing in two season folders); and an
# incomplete skeleton answer is handed to the NEXT provider (with a --require-list)
# instead of parking the release -- the systemic seam Family Guy and Friends share.
run "skeleton merge attribution, alternates and feedback (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_skeleton_merge_feedback.py
# American Dad's wrong-slot `S04E06 - Independent Movie` (TMDB says S10E06) and Doctor
# Who's S00E04 collision: the repair path computes the true slot -- TMDB for a numbered
# episode, the library's own era-ordered Season-00 shelf for a special -- and re-files
# through refile_season machinery with an nfo rewrite that matches the destination. The
# doctor guard reports a sidecar slot that disagrees with the filename, and the live
# doctor worklist is replayed.
run "computed episode-slot repairs and the specials scheme (both ways)" \
    env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/test_slot_repair.py

# ---- advisory: is the acceptance gate still being REACHED? (§4.120) ----------
# Deliberately NOT part of the pass/fail above. This script answers "is the code sound?",
# and the gate going dark is a runtime fault -- `fleet_health` raises it as an ACTION.
# Printed here because this is the command a session actually runs first (§0.3), so it is
# where a human will see it.
echo
printf '%-46s' "acceptance gate liveness (advisory)"
if gate_out="$(env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/gate_status.py 2>&1)"; then
  echo "ALIVE"
else
  echo "DARK -- see below (does not block shipping)"
  echo "$gate_out" | sed 's/^/      /'
fi

# ---- advisory: the owner's five verified failures (10.10F) -------------------
# §2.6: a fix is real only where the owner can see it. Checks the actual artifacts
# (TZ art bytes and nfo, the One Piece shelf, the Smurfs plan) and prints PASS/FAIL.
# Never blocks shipping: the Smurfs pack is deliberately parked until the plan tool
# ships, and a reaper purge may still be draining when this runs.
echo
printf '%-46s' "owner report (advisory)"
if own_out="$(env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/verify_owner_report.py 2>&1)"; then
  echo "see below"
else
  echo "report failed to run (does not block shipping)"
fi
echo "$own_out" | sed 's/^/      /'

# ---- advisory: can identify RUN at all? -------------------------------------
# Not blocking, for the same reason as the gate advisory: an exhausted daily cap is a
# runtime condition, not a code fault. Printed because "downloads finish and sit UNFILED"
# is otherwise only visible as one repeated line in torrent_ingest.log, and because the
# OTHER thing this reports -- a provider whose tokens-per-minute ceiling is below the
# smallest prompt we can build -- never clears on its own and has no other symptom.
echo
printf '%-46s' "identify capacity (advisory)"
if cap_out="$(env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/identify_capacity.py 2>&1)"; then
  echo "OK"
else
  echo "NONE -- downloads will sit unfiled (does not block shipping)"
fi
echo "$cap_out" | sed 's/^/      /'

# ---- advisory: is YacReader's index intact? ---------------------------------
# Runtime state, not a code fault, so it does not block shipping -- but it is printed
# here because nothing else in the fleet looks. The corruption is partial and quiet
# (`folder` answers while `comic` does not), so row counts read healthy and only
# PRAGMA integrity_check catches it; two separate corruptions went unnoticed for four
# weeks because the only detector was the owner seeing black-X covers (§4.185).
echo
printf '%-46s' "YacReader index (advisory)"
yac_out="$(env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/yacreader_index_health.py 2>&1)"
case $? in
  0) echo "OK" ;;
  # Status 2 is deliberately NOT reported as damage: the index is intact, and saying
  # "DAMAGED" about a healthy library is how a real warning gets learned as noise. What is
  # missing is the thing that makes the NEXT corruption survivable.
  2) echo "INTACT, BUT NOT RECOVERABLE -- no clean backup exists (does not block shipping)" ;;
  *) echo "DAMAGED -- see below (does not block shipping)" ;;
esac
echo "$yac_out" | sed 's/^/      /'

# ---- advisory: may YacReader update its own library? ------------------------
# Runtime state again, not a code fault: the flags are enforced by the supervisor, and a
# drift here is what made every filed comic invisible on 2026-09-14. Printed because this
# is the command a session runs first.
echo
printf '%-46s' "YacReader scan-at-startup (advisory)"
if rescan_out="$(env -C "$DEV/Torrent-Ingest" "$PY_INGEST" scripts/yacreader_rescan.py 2>&1)"; then
  echo "OK"
else
  echo "DRIFT -- the reader cannot index new comics (does not block shipping)"
fi
echo "$rescan_out" | sed 's/^/      /'

echo
if [ $fail -eq 0 ]; then
  echo "ALL CHECKS PASSED."
else
  echo "SOMETHING FAILED -- do not ship." >&2
fi
exit $fail
