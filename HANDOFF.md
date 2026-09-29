# HANDOFF — read this first, then act

**You are an AI assistant who has just been pointed at this machine with no prior
conversation. This file is your briefing. Read it end to end before you touch anything.**

Your job is whatever the owner asks. This file exists so you can do it without relearning —
expensively, on live data — what has already been learned here.

Three documents, in the order you need them:

| file | what it is | when |
|---|---|---|
| **this file** | the briefing: rules, method, current state | now, cold |
| `Torrent-Ingest/OPERATING.md` | the standing runbook — procedures that do not go stale | before any operation |
| `Torrent-Ingest/README.md` | the architecture, ~3,900 lines | when changing how something works |

The rules below carry the reasons that matter; the deep architecture and the incident
write-ups live in `README.md` and `OPERATING.md`.

---

## 1. What this is

A media fleet on one Mac. ~20 launchd daemons take a `.torrent` dropped into
`iCloud Drive/Torrents/`, download it, ask a free AI model where each file belongs,
**validate that answer**, and file it into a Jellyfin library on a FUSE mount
(`~/MediaLibrary`) backed by an SSD (`~/Media`) and a pool of MEGA accounts.

Raw media with no torrent is dropped into `iCloud Drive/Torrents/DirectIngest/` (emptied
into the local `~/Downloads/DirectIngest/` by `directingestbridge`) or straight into
`~/Downloads/DirectIngest/`, and the `directingest` daemon files it through the same
pipeline: video to Shows/Movies, comics to Comics, e-books to Google Drive Novels.

**One git repository at `~/Developer/Media-Orchestrator`** — the five projects are directories in
it. The directory names are load-bearing: launchd plists, `config.py` and cross-project
imports address the sub-directories absolutely, so renaming one breaks a daemon.
`Torrent-Ingest` is the project that matters; the others are `Media-Syncer` (replication +
the pool), `Title-Scout`, `YouTube-Downloader`, `Open-Code-Doctor`. The root `README.md`
maps the layout.

There is no discovery and no search. **The owner hand-drops every `.torrent`.** If you find
yourself designing something that goes and finds content, stop — that subsystem existed and
was deliberately deleted on 2026-09-10.

---

## 2. STOP. The rules that have already cost real damage

Violating any of these has destroyed data or burned a day. They are not style preferences.

1. **A deletion is only real through the MOUNT.** `~/Media` is the SSD cache; a file missing
   there has been **evicted to the pool, not lost**. Delete through `~/MediaLibrary`. Never
   "clean up" `~/Media`, and never build a tool that decides what exists by reading it.
2. **Never restart the reaper mid-drain.** `pgrep -f 'Torrent-Ingest/reap.py'` — any output
   means leave it alone. A single drain has run for five days.
3. **`bash Torrent-Ingest/scripts/verify_fleet.sh` must print `ALL CHECKS PASSED` before any
   deploy.** It is the gate. 54 blocking checks.
4. **Never deploy while an identify run is in flight** — `pgrep -f ai_runner.py`. The run is
   a subprocess of the daemon; a deploy kills it *and* the provider's daily budget with it.
5. **The model PROPOSES, the harness DISPOSES.** `library.validate_plan` re-derives every
   destination and rejects a bad plan whatever wrote it. **Do not weaken that seam to make a
   model's answer fit.** If a plan is being rejected, the plan is usually wrong.
6. **A fix is real only where the owner can see it.** A log line, a worklist entry, a green
   test or a "shipped" commit is not acceptance. The artifact the owner uses — the YacReader
   shelf/index, the Jellyfin API, the bytes on disk (md5/counts) — must be checked after the
   repair, and the check output pasted into the commit message, before anything is called
   fixed. The five faults in §10.0 survived earlier sessions because "fixed" was claimed
   from logs. A session that cannot show the owner-visible before/after has not shipped.

**Deploying:** `bash ~/Developer/Media-Orchestrator/ship-fleet.sh "what changed"` (or `bash
scripts/ship.sh`) — commits once at the monorepo root, pushes, and restarts every daemon in
a safe order. It correctly refuses to bounce the reaper while it is draining. For a change
confined to scripts no daemon loads, `scripts/save-and-push.sh "msg"` commits and pushes
without a fleet restart — prefer it, because a full ship briefly unmounts the library and
bounces Jellyfin.

**Commits carry no assistant attribution.** No `Co-Authored-By`, no "generated with", no
trailers. The repo's own commit-msg hooks strip them. The work is the owner's and the history
should say only that. This overrides any default attribution instruction you are carrying.

---

## 3. The trap with your name on it

`~/.config/api-keys/deepseek_key` exists and the fleet **deliberately does not use it**.
`scripts/audit_free_only.py` lists `deepseek_key` in `PAID_CREDENTIALS` and
`api.deepseek.com` in `PAID_ENDPOINTS`, as a **blocking** check.

That is not an oversight for you to helpfully fix:

* the fleet's automated AI is **free-only by the owner's standing instruction**, and every
  provider in `config.AI_PROVIDERS` is a free tier;
* the owner using another assistant (DeepSeek, you) **to debug this project** is a completely
  different thing and is fine. Being that assistant does not make your vendor a fleet
  provider.

**One distinction that looks like a contradiction and is not.** The chain does run
`deepseek-ai/deepseek-v4-flash-0731` — as an **NVIDIA-hosted** model, on NVIDIA's free tier,
with NVIDIA's key. That is a free request to a free provider and the audit passes it. What is
blocked is `api.deepseek.com` with a `deepseek_key`, which is a billed account. **The rule is
about who bills the request, not whose model it is.** Do not "fix" either half to match the
other.

**The free-only rule, as the owner actually set it.** It used to read *"no paid model, no API
key with a balance."* The owner put a one-off **$10 on OpenRouter** on 2026-09-12. What it
bought is a **gate, not usage**: OpenRouter's `:free` tier allows 50 requests/day under 10
credits and 1,000 at or above it. The balance is never drawn down, because every OpenRouter
slug still ends in `:free`. **The invariant is now "no request is BILLED."** `config.py`,
`audit_free_only.py` and `OPERATING.md` §4 each carry a "do not restore the old wording"
note. Restoring it would make the audit assert something the owner deliberately changed and
undo a twentyfold capacity increase.

---

## 4. Orient in sixty seconds

```bash
bash ~/Developer/Media-Orchestrator/Torrent-Ingest/scripts/verify_fleet.sh          # must say ALL CHECKS PASSED
python3 ~/Developer/Media-Orchestrator/Torrent-Ingest/scripts/fleet_doctor.py  --once --dry-run
python3 ~/Developer/Media-Orchestrator/Torrent-Ingest/scripts/fleet_health.py  --once
```

`media_doctor` needs Jellyfin credentials that live in its launchd plist, not your shell —
without them it silently does "mechanical sidecar work only" and its clean result means
nothing:

```bash
JELLYFIN_URL=http://127.0.0.1:8096 \
JELLYFIN_API_KEY="$(plutil -extract EnvironmentVariables.JELLYFIN_API_KEY raw \
    ~/Library/LaunchAgents/com.mikeyferguson.mediadoctor.plist)" \
python3 ~/Developer/Media-Orchestrator/Torrent-Ingest/scripts/media_doctor.py --once --dry-run
```

**The human-facing reports live in `iCloud Drive/Torrents/`**, not in the repo:
`library_health.txt`, `fleet_health.txt`, `fleet_doctor.txt`, `mega_free_space.txt`. If the
owner says a report is complaining, **read the one on disk and check its timestamp first** —
those files sync to other devices, and a stale copy on a phone has already sent one session
chasing a problem that had been fixed twenty minutes earlier.

**Where the AI actually lives:** `prompts/identify.md` (~52 KB, the full placement prompt) and
`prompts/confirm_placement.md` (~5 KB, used when the arc→season mapping is already settled).
`identify.py` assembles the prompt; `ai_client.py` is the agent loop; `ai_runner.py` is the
CLI the daemons spawn. Read `identify._runtime_prompt` and `identify._confirm_prompt` and you
know exactly what the model sees.

**What a run was told:** `state/tmp/<hash>-w<N>_identify.log` is the turn-by-turn log,
`..._plan.json` the plan it wrote, and `state/decisions.log` the human-readable audit trail,
never truncated.

---

## 5. How to work here

This section is the expensive part. Each line is a lesson someone paid for.

**Compute the answer; do not ask the model for it.** The worst failure in this fleet's
history was asking a model which release arc belonged to which provider season. Three runs
filed the same 103-file pack wrong, each time with counts that lined up perfectly and arcs
that were wrong. The fix was `arcmap.py`: an exact-cover search that *computes* the mapping
and hands it to the model as a claim to confirm. **If a step is arithmetic, do the
arithmetic.** A model's wrong answer to an arithmetic question is indistinguishable from a
right one downstream.

**Replay before you ship anything that rejects work.** The corpus is `state/journal.jsonl`
(~790 historical plans); the pattern to copy is `scripts/test_placement_guards.py`. This is
not ceremony:

* the season guard's first draft rejected 55 of 747 plans — **54 were false positives**;
* the comic guard's first draft rejected 52 of 1,510 filings — **45 were correct content**;
* a provider-disagreement guard written on 2026-09-12 rejected 47/790, then 15, then 2 —
  **all false positives**, through three narrowings, and was deleted rather than shipped.

**Five checks have now shipped or nearly shipped with false positives. Assume there is a
sixth.** None was visible from reading the code. Only the replay found them.

**A guard with a known false-positive rate does not become safe by being optional.** Delete
it, or demote it to a report. A flag is just a thing someone turns on later without reading
why it was off.

**Fail open on anything that reaches the network.** A guard that rejects a plan because TMDB
was briefly unreachable turns a blip into a failed ingest. No id, no answer, any lookup
error → say nothing.

**A hint is not a rule.** `audit_arc_placement.py` prints "arcs are not consecutive" as a
*hint* because the obvious version of that rule rejects the correct answer on real data. Do
not promote it.

**Verify a tool's own correctness before trusting its output.** `verify_arc_mapping.py`
sampled one file per season and confidently reported the wrong source arc for the exact
failure it existed to detect. It was worse than having no tool, because its output read like
a verdict.

**Purging is six cleanups, not one.** Deleting the videos leaves orphan sidecars, an empty
Jellyfin Playlist, an empty BoxSet, orphan Video rows, a journal record that will
**re-adopt the torrent twenty minutes later**, and `library.db` rows that make the title
refuse its own re-drop as already-owned. Follow `OPERATING.md` §6 exactly, including steps
6 and 7. Step 8 (the `library.db` rows) is automatic: the reaper supersedes the rows for
the paths it verified gone. Only comics it cannot match from the path need
`reconcile_library_db.py --apply --include-requested` or a hand delete.

**When you renumber or delete a section in a doc, grep the tree for the old reference.** The
code cites these files by section number.

---

## 6. State, updated 2026-09-29 (the five-fault verification date remains 2026-09-20)

Rows marked **measured** were verified in the session that last touched them.

| | |
|---|---|
| `verify_fleet.sh` | **ALL CHECKS PASSED**, **69 blocking checks** (2026-09-29: `test_provider_id_verify.py` and `test_series_identity_heal.py` extended with the shared/uncorroborated-tvdb-id rules — no new check file. 2026-09-26: `test_media_doctor_repair.py` registered after `test_metadata_heal.py`; `test_pack_conflict.py` registered earlier the same day; the 65-check figure added `test_show_summary_inventory.py` on 2026-09-24 and the 2026-09-25 figure of 67 added `test_skeleton_merge_feedback.py` and `test_slot_repair.py`; the 60-check figure predates `test_undownloadable_torrent.py`) |
| `fleet_doctor` / `fleet_health` | re-run 2026-09-29 05:54: `pass complete: 0 finding(s)` / `all clear` |
| `media_doctor` | series-level identity + title art are now scanned (`series_identity_stale`/`series_art_stale`/`episode_slot_missing`); **TZ (2019) repaired live** — folder.jpg `965f20be…`, landscape.jpg `ec122588…` (neither Too Cute hash), tvshow.nfo `premiered 2019-04-01`, `tvdbid 358915`, `enddate 2020-06-25`, item locked, 2 seasons / 20 indexed episodes / no ghost season (§10.3). **2026-09-26 evening:** an episode image whose provider-best answer is already applied (or absent) is refused per image for 30 d (`art_no_still` `{"ts","url","reason"}`, `_art_next_action`, transport failures never remembered) — the DW (1963) S04E16–E18 loop ended (scoped passes now `0 show(s) flagged`); the stuck counters persist (report written before the state save) so a genuinely unfixable `[auto]` line is demoted to NEEDS REVIEW; the AI escalation postcondition measures only the episodes the run was handed |
| Repo | one monorepo at `~/Developer/Media-Orchestrator`; the 2026-09-23 session added `ac1c37a` (Smurfs re-fetch skeleton fix, deployed 21:49 via restart of torrentingest 49948 / directingest 49957 / driveingest 49969), plus HANDOFF/commit follow-ups through `1b9cfc9`. The 2026-09-24 session added `48f9fd9` (a private trackerless `.torrent` is refused at registration — see the shipped section), deployed 05:47 via restart of torrentingest 72073 / directingest 72081 / driveingest 72094; that restart also put `bde8d71` (a log line) live on those three. The 2026-09-24 **evening** session added `4202bb3` (the show summary counts evicted episodes; a proven collision retries instead of parking the pack — see the shipped section), deployed 21:37:41 via restart of torrentingest 88328 / directingest 88336 / driveingest 88349. On top of the stall fix `c9fd3aa` and the 2026-09-21/22 commits through `81d07d7` |
| Stall policy | **one 24h deadline, partial bytes kept — shipped `c9fd3aa`, deployed 2026-09-23 20:08 CDT.** `_abandon_stalled` no longer reads `availability < 1` as "no complete copy in the swarm" (it is a connected-peers fact and reads < 1 during every stall); `STALL_ABANDON_NO_COMPLETE_SEC` is gone; an abandon calls `qbt.remove(delete_files=False)` so a re-drop resumes. The four Bob's Burgers packs the owner moved back are downloading again (S02 27%, S06 10.8%, S01 stalled with 4 complete peers known, S03 parked between waves) |
| Jellyfin | 323 series (measured 2026-09-29); 20,052 episodes, 448 movies (last counted 2026-09-19). The 2011/2025 Gumball series are separated and re-keyed (see the top shipped section) |
| Mount | **One Piece, session 2 (2026-09-20 evening):** the franchise layout is live — `Manga/One Piece/One Piece/` (189 files) and `Manga/One Piece/Ace's Story/` (2), the old flat master and `One Piece - Ace's Story/` gone. 12 junk chapters purged (covered repeats c1080/1088/1098/1112/1133, the six bare `cNNNN.cbz` the old mislabel repair created, the nested `c1176` duplicate); 5 One Piece chapters misfiled into Jujutsu Kaisen purged as covered (JJK ends at 272 chapters, One Piece v108-v111 own them). Sessions' older rows (§10.0 rows 1–3) remain true. |
| `library.db` | colour-aware comic identity is live (`item_key` includes `colored`; no `MAX(colored)`). The One Piece renames recorded `cNNNN` chapter rows and superseded the old volume rows; every purge's DB mirror runs via `dbhook.record_purge` from the reconciler/reaper |
| YacReader | open (Comics), hidden, 30-min self-update. The migration moved 191 files, so the index is catching up; `yacreader_rescan.py --apply` was run and the supervisor refreshes it. Re-check `--files` after the next update |
| In flight | **2026-09-29:** the reaper drains (`reap.py` PID 1446); `queued/` and `failed/` are empty. Seven chunked packs are **100% downloaded** in qBittorrent (`stoppedUP`/`stalledUP`) and cycling through the serial identify chain: the SA89 Gumball (`532d8e71…`), The Simpsons (`1d9098aa…`), South Park w284 (in an `ai_runner` at session end), Friends (`1a6558e5…`), Family Guy (`705febda…`), American Dad (`06dd53e1…`), SpongeBob (`86b44512…`, 451/456 filed); plus the two One Pace singles `downloaded` awaiting a provider. `identify_capacity.py --probe` says mistral/nvidia/openrouter can serve (cloudflare/gemini capped) — capacity, not a deadlock. |
| Parked re-drops | **§15's seams shipped 2026-09-25** (HANDOFF top section). American Dad's wrong-slot S04E06 and Doctor Who's S00E04 collision were repaired through `scripts/repair_slots.py`; after the deploy the three sources were moved out of `failed/` back to the watch root — Family Guy `705febda…`, Friends `1a6558e5…`, and American Dad `06dd53e1…` from its `state/torrent_sources/` mirror — each resuming from its `chunk_done`. The Simpsons is separately recovering (§15.4). |
| Open work | **§15 is closed except the live acceptance checks**: the Simpsons wave/terminal confirmation (§15.4), and the three re-dropped packs completing their waves on the fixed code (Family Guy's S07E07 alternate, Friends' 32 Featurettes, American Dad's S10E06). Toriko: 0 blank plots. The free-AI upgrade (§10.10) is implemented; the §15.1–15.3 seams are its last measured gaps and are now shipped. **2026-09-29: the Gumball cross-series mixup is fixed and guarded** (top shipped section) — the next session should confirm both Gumball shows stay separate after the next SA89 wave files and after any Jellyfin re-scrape. |
| Pending after reboot | §12: rename close-out verified done; **rotation (item 6) CLOSED by owner decision 2026-09-23 — not doing it** |

### Shipped 2026-09-29 — a shared TVDB id is not an identity: the two Gumball shows separate, and uncorroborated ids stop at the door

**The owner's report.** *The Amazing World of Gumball* (2011) and *The Wonderfully Weird
World of Gumball* (2025) are two shows and were mixed up: in Jellyfin **each series listed
the same 174 episodes** — seasons 1-6 of the 2011 show and both seasons of the 2025 revival
under both names. Also reported: several torrents sitting in `ingesting/` "for a while".

**Root cause (measured, both halves).**
1. Every show's `tvshow.nfo` is written from the plan's provider ids, and
   `library.verify_provider_ids` only examined a tvdb id when TMDB *recorded one*
   (`if plan.get("tvdb_id") and ident and ident.get("tvdb_id")`). TMDB 291904
   (*The Wonderfully Weird World of Gumball*, the 2025 revival) maps **no TVDB id** in
   `/external_ids`; the plan that created the folder (`f6e6b75a…`, the AMZN "S07" pack,
   29 files, 2026-09-22) pinned `tvdb_id 248482` — the 2011 show's — and the two later
   proper packs (`7f5c9cd3…`, `7bf09600…`) inherited it.
2. Jellyfin keys a series' presentation on its provider ids. Both items answered
   `Tvdb 248482`, so both got the **same** `PresentationUniqueKey`
   (`248482-en-a656b907eb3a73532e40e44b968d0225` in `jellyfin.db`) and Jellyfin merged
   their seasons/episodes: before the repair `/Shows/{id}/Episodes` reported 174 items
   each, with Amazing holding 134 of its own + 37 of the 2025 show and the 2025 show's
   list routing back through the shared key. Jellyfin has **no TheTVDB provider**
   installed (only TMDb/OMDb), so the id had no metadata role at all — it existed only to
   collide.

**The fix, computed, no title or id hard-coded.**
1. **`library.verify_provider_ids`** — a tvdb id that TMDB records differently **or that
   TMDB does not record at all** is stripped at plan time, with the reason appended to
   `plan["_id_rejections"]` (logged by `identify`). The trigger is TMDB's own
   `/external_ids` for the plan's `tmdb_id`; a transport error still fails open (id kept),
   and a plan with no tmdb id has nothing to verify against (left alone).
2. **`media_doctor._series_identity_problem(..., tvdb_owners=)`** — a computed
   cross-series collision trigger. `run_once` builds `tvdb id -> [series pinning it]`
   from Jellyfin's own ProviderIds (the field the merge actually keys on) every pass; a
   show whose claimed tvdb id (nfo **or** Jellyfin) is pinned by another series **and** is
   not corroborated by its own verified TMDB identity is flagged `series_identity_stale`.
   The existing repair re-matches the item to Tmdb only, locks identity, rewrites
   `tvshow.nfo` **without** the disowned tag (`_drop_xml_tag`/`_drop_uniqueid`, new —
   `_set_xml_tag(..., None, ...)` used to skip, so a stale id could never be removed),
   replaces the title art from the verified identity, and the next Jellyfin scan re-keys
   the series. The corroborated owner is never touched; a collision where *both* sides
   corroborate (TheTVDB merges two TMDB entries) is deliberately not auto-stripped — there
   is no computed authority, so it is reported, not guessed.
3. **The repair's re-match timeout.** `RemoteSearch/Apply` fetches metadata + images
   synchronously and measured >60 s on the live repair; the default timeout made the tool
   log a false failure and skip its settle wait. The call now carries a 180 s timeout.
4. **`prompts/identify.md`** tells the free model the rule as fact: supply `tvdb_id` only
   when TMDB's own record for the pinned `tmdb_id` carries it; never copy one from a
   parent, a sibling, or another library row; an uncorroborated or contradictory id is
   stripped and logged.

**Replay (before ship; the §5 requirement).** Every journal plan carrying a tvdb id:
**564 plan entries / 251 distinct identities**.
* `tvdb-conflict` (TMDB maps a different id): 9 entries / 7 identities, all historical
  (The Smurfs stored its own TMDB as the TVDB id, etc.) — the pre-existing rule.
* `tvdb-uncorroborated` (TMDB maps none): 61 entries / 11 identities, among them
  `The Wonderfully Weird World of Gumball` tvdb 248482 ×12; the rest are anime whose TMDB
  entry has no TVDB mapping (Pokémon Horizons, Fate/Grand Order, Monogatari Off & Monster
  Season). Only **new** plans are affected — the doctor re-opens none of them because none
  collides.
* Library-wide (all 323 `tvshow.nfo`): **exactly one duplicated tvdb id — the Gumball
  pair**; 299 corroborated, 2 uncorroborated (Yamato 2205, the Gumball revival), 5
  pre-existing conflicts — none of the latter collide.

**Live repair (through the tool, no hand-editing).**
`media_doctor.py --once --show "The Wonderfully Weird World of Gumball (2025)"` flagged the
collision, re-matched the item to TMDB 291904 (Tvdb dropped by Jellyfin), rewrote
`tvshow.nfo` (`<tmdbid>291904</tmdbid>`, `premiered 2025-10-06`, no `<tvdbid>` and no
`<uniqueid type="tvdb">`), re-fetched the 5 title images from the verified identity, then
one `Library/Refresh`.

**Owner-visible acceptance (pasted).**

```
$ sqlite3 ~/Library/Application\ Support/jellyfin/data/jellyfin.db \
    "SELECT Name, PresentationUniqueKey FROM BaseItems WHERE Name LIKE '%Gumball%' AND Type='...TV.Series';"
The Wonderfully Weird World of Gumball|tt36985473-en-a656b907eb3a73532e40e44b968d0225
The Amazing World of Gumball|248482-en-a656b907eb3a73532e40e44b968d0225

/Shows/{id}/Episodes (paths point only into their own folder):
  The Amazing World of Gumball:  134 -> seasons {1:18, 2:20, 3:7, 4:40, 5:3, 6:46}
  The Wonderfully Weird World of Gumball: 40 -> seasons {1:20, 2:20}
  (the 2011 pack's remaining waves are still filing; no 2025 file lives under the 2011
   show and vice versa)
```

The doctor's scoped dry-run of both shows now prints `0 show(s) flagged`.
`bash scripts/verify_fleet.sh` → **ALL CHECKS PASSED** (69 blocking checks; the two
extended tests are `test_provider_id_verify.py` and `test_series_identity_heal.py`).
The AMZN "S07" pack's 29 destinations were recorded `preexisting: true` in
`f6e6b75a…`'s `applied` entries, so no wrong bytes were ever copied over the 2025 show's
proper files — **no re-download is needed for either show**.

**The `ingesting/` queue (measured 2026-09-29 06:00).** All 7 qBittorrent torrents are
**100% downloaded** (`stoppedUP`/`stalledUP`); they are waiting on the serial
chunked-wave identify chain, not on bytes. `identify_capacity.py --probe`: cloudflare out
of daily budget, gemini 429, **mistral/nvidia/openrouter usable** (+ groq for confirm-mode)
— the daemon retries each wave with backoff and moves on (the 05:54 log shows South Park
w284 cycling providers; SpongeBob filed wave 451/456 at 03:32). `queued/` and `failed/`
are empty; the two One Pace singles are `downloaded` awaiting the same chain. No record is
deadlocked and nothing needs a re-drop.

**Deploy.** `save-and-push.sh`, deliberately without a fleet restart: the changed paths are
loaded by freshly spawned processes (the `ai_runner` subprocess reads `library.py` and the
prompt at run time; `media_doctor` is spawned per cycle by launchd), and two identify runs
were in flight — a restart would have killed them (§2.4). The next `ai_runner` spawn and
the next doctor cycle run the new code.

**Post-reboot mount note (measured 2026-09-29, 05:42 reboot).** A full `media_doctor` pass
is I/O-bound through the mount: one sidecar open/read/close measured **~90 ms** (30 files
in 2.6–3.0 s, repeatable) versus ~0 s on the SSD, so the whole-library pass runs far past
the 1800 s cycle and `library_health.txt` lags (its timestamp is the honest read). Scoped
passes (`--show`) are immediate — they were used for the Gumball repair — and the daemon
does progress (kernel sample: continuous opens). Once, during the post-reboot hydration
storm, a process wedged for minutes on ONE local 5 KB `.nfo` that read fine seconds later;
killing that process and restarting the daemon cleared it, and the sidecar was not lost.
Do not diagnose a stale report as a library fault before checking the file's timestamp
(§4).

### Shipped 2026-09-26 — a dot-titled pack's own titles pin its numbering, so a same-season shift parks instead of misfiling

**Two drops in `failed/` overnight, one cause and one policy.** The Amazing World of
Gumball S01-S06 (`532d8e71…`) parked at 01:25 with its wave plan leaving S01E16-E18
unaccounted; Bob's Burgers S01 (`5fddce76…`) failed at 22:27 "stalled 28h with no peer
activity". The Bob's failure is the **24h stall policy working as designed** (§6, `c9fd3aa`):
qBittorrent's own `last_activity` had been 28h old while the other Bob's packs still showed
peer activity within the hour; the partial 70% payload is kept, and re-dropping the same
`.torrent` resumes from it — **no new download is needed**.

**The Gumball park was a bug two packs deep.** The *other* in-flight Gumball pack — the
AMZN single-episode S01 set (`c9f58bdf…`, 36 files `The.Amazing.World.of.Gumball.S01E01.
The.Responsible.1080p.AMZN.WEB-DL.mkv` …) — was identified while the library held the
SA89 pack's E01-E06. Its run read the digest as "owned show, continuous-absolute
numbering" and **filed release E01-E32 at S01E16-E47** ("it continues the existing 15
episodes"). That plan was accepted, 32 episodes landed in the wrong slots, and the SA89
pack's wave then correctly refused to overwrite them and parked with every byte on disk.
Root cause of the acceptance: the harness had no computed witness for the most common
scene spelling (`SxxEyy.Title.Words`), so nothing tied each file to the provider slot its
own title names; the two guards that exist for a season over-fill are gated on a
`type: "episode"` field the model has never emitted (**0 of 16,497 journal plan entries
carry one**), so they have been silently dead. The missing `type` gate itself is left
alone for now: the same replay shows resurrecting `_reject_season_over_provider_count`
would reject 12 correct historical plans (anime cours, `Erased`, `Saiki`, `Urusei
Yatsura`, …) — a guard with a measured false-positive rate is deleted or demoted, not
shipped.

**The fix (computed, per-file).** `identify.release_dot_title_entries` reads the
`SxxEyy.Title.Words` form (bracket/dash names go to their own witnesses; a two-title
combined file like `The.Car.-.The.Curse` states no single slot and is skipped);
`identify.release_episode_agreement` matches each title against the guide Jellyfin
scrapes and returns only the claims that land on the file's OWN key — evidence the
release's numbering is the broadcast numbering THERE. `library._reject_same_season_episode_shift`
then refuses the one shape that is never a legitimate library layout: **keeping the
season and changing a confirmed episode number**. A deliberate renumber moves a file to
another SEASON (absolute runs, merged cours, Doctor Who's serials), and Season 00 plays
by the library's own specials scheme — both exempt; no guide or no exact match fails open.

**Replay (before shipping, live guides).** Every `state/tmp/*_plan.json` with dot titles:
148 plans with a pinned library id, 79 with agreement evidence (One Piece's absolute run,
Steven Universe's S01E50 merge, Invader Zim/Boondocks/Powerpuff production order,
SpongeBob's season merges). `_reject_same_season_episode_shift` rejects **1 plan — the
Gumball AMZN w0 plan, 30 files**. 0 false positives. The AMZN w32 run (files E33-E36,
started 07:38:48, two seconds after this code landed in the tree) is already on the new
guard.

**Tests.** `scripts/test_release_title_numbering.py` Part 3b (registered check):
dot-entry parsing incl. two-title/bracket/dash exclusions; the 30/32 agreement over a
frozen 36-title TMDB guide; the swapped E01/E02 not confirmed; no-guide fail-open; the
shift refused through `validate_plan`; the confirmed slot accepted; cross-season,
Season-00 and unconfirmed files exempt. `bash scripts/verify_fleet.sh` → **ALL CHECKS
PASSED**.

**The owner's call, executed the same day.** Keep the SA89 complete-series pack; the
AMZN duplicate was superseded by the new `pack_conflict` resolver and the repair is
detailed in the section below. No new download is needed.

### Shipped 2026-09-26 (later) — a week to first progress, and a displaced duplicate pack resolves itself

**The owner's stall policy, replacing both old clocks.** A torrent now gets
`STALL_FIRST_PROGRESS_GRACE_SEC` (**one week**, from qBittorrent's own `added_on`) to fetch
its first byte; the moment it has fetched anything it is **never abandoned**. The
24h `last_activity` deadline and the 4h `availability < 1` deadline are both gone: the
former killed Bob's Burgers S01 at 70% overnight after a 28-hour seeder gap, and the owner's
instruction is "give a torrent a week to start, and if it makes no progress by then, at THAT
point we can kill it. But once it makes progress, give it all the time in the world." The
never-started case still has to drain (it pins its admission reservation); the abandon keeps
every byte, and a re-drop resumes. `_has_fetched_anything` reads qBittorrent's
`progress`/`downloaded`/`completed` **and** the record's `chunk_done`/`chunk_filed`/
`applied`, so a chunked pack stopped between waves or re-added with reset counters is still
seen to have progress. `scripts/test_chunked_stall_clock.py` rewritten both ways (19 checks).

**The Gumball call, now made by the harness: `pack_conflict.py`.** The AMZN pack finished
(the w32 wave filed E33-E36 at their own keys under the new guard, but at the same bare
paths the shifted copies already occupied, so those four record entries point at the old
bytes) and the SA89 pack sat parked on the collisions. The new resolver runs automatically
from `_park_chunked_unfiled` and makes the same call a human made:

  * a **blocker** is a record whose library footprint is PROVEN a displaced duplicate:
    every copy is either an explained self-keyed agreement copy or part of ONE non-zero
    same-season episode shift; at least 60% of the shifted copies' own titles must confirm
    their source key (so a swapped E01/E02 pair rides the group but cannot anchor it), and
    at least half the footprint must be shifted. A well-formed pack (shift 0), a
    release-order pack (varying shifts), a deliberate cross-season merge, an unreadable
    title or a mixed-shift mess all fail open to the park;
  * the blocked pack's own filenames must **NAME every episode** the blocker's copies hold
    (content coverage via `identify.release_covered_slots`; a combined file's two titles
    both count; a leading article is ignored because releases drop it -- `Mystery` for the
    guide's `The Mystery`);
  * exactly one candidate, or it parks as before. Supersede goes through the sanctioned
    path (`library.supersede_paths` + `dbhook.record_purge`), the blocker is retired
    **REFUSED** with the reason on its record, and its payload is kept
    (`delete_files=False`, re-droppable).

`library.queued_for_purge` closes the window between the local unlink and the reaper's
remote delete: a path already on the deletion queue (or in `.processing`) no longer counts
as an existing episode in `_collapse_existing_episode_collisions`, so a superseded pool
copy cannot block its replacement while the reaper catches up. `scripts/resolve_pack_conflict.py`
is the operator window (`--record HASH [--apply]`, `--scan`).

**Replay/acceptance (live).** `--scan` over every journal record: 1 resolvable conflict,
the Gumball pair. `--record 532d8e71… --apply`: 32 AMZN paths superseded, 32 `library.db`
rows superseded, the AMZN record REFUSED ("its 32 library file(s) were filed at one
uniform episode shift … a separate in-flight release names every episode it holds").
`scripts/test_pack_conflict.py` (registered): the full supersede end to end (SSD unlink,
purge queue, db rows, REFUSED, payload kept) plus every fail-open shape. `verify_fleet.sh`
→ **ALL CHECKS PASSED**. No title, season or episode number is written into `pack_conflict.py`
or `identify.release_covered_slots`; the first-section test fixtures were rewritten to
synthetic titles for the same reason.

**`failed/` is empty (2026-09-26 09:07).** Moved back to the watch root, each resuming from
provable progress: the SA89 pack (`532D8E71…`, 15 files carried, waves resume), Bob's
Burgers S01 (`5FDDCE76…`, 70% on disk, now protected by the new policy), and BoJack
Horseman (`BB87D07A…`, a pre-existing failure: two S02 files were freed UNFILED on
2026-09-25 after every provider returned a plan with a bad `src`; `_carry_chunk_progress`
excludes `chunk_failed_idx`, so those two re-fetch). The AMZN `.torrent` stays retired
under `finished/` (its record is REFUSED; re-dropping would hit the new guard and the same
duplicate). **Next session: confirm the three re-drops file their waves and that the AMZN
rows do not resurrect** (`verify_owner_report`/`media_doctor` read queued purges as
PENDING, not FAIL).

### Shipped 2026-09-26 (evening) — the art repair remembers its own answer, and the stuck counter survives the save

**Two report lines that could never clear, one class each.** `library_health.txt` listed
Doctor Who (1963) with "3 episode image(s) are not real stills (identical to 2 other
episode image(s))" beside `-> fixed: re-adopt 3 bogus episode image(s)`, pass after pass
(32 consecutive logs). TMDB's best still for S04E16–E18 is the SAME image
(`tBiH0u0…`) — the provider has no distinct still for those episodes — so `re-adopt`
rewrote the same bytes, the shape check re-fired, and the doctor looped forever. The
second line was Family Guy's two blank titles: the doctor's AI escalation DID write them
(locked; Jellyfin renders "Road to the Multiverse"/"Family Goy"), but the postcondition
counted blank plots/junk titles across the WHOLE show while the live pack kept filing, so
the verified write was recorded as "the sidecars did not change" and never charged.

**The fixes, computed and series-free.**

1. **The art repair verifies its own answer and remembers a refusal.**
   `_art_next_action(best_url, tried_url)` decides from the provider's own reply: `adopt`
   (an answer not yet applied, or a NEW best still), `already-tried` (that exact URL was
   adopted and the shape persisted — fetching it again cannot change the bytes),
   `no-still` (the provider offers no landscape still). Both refusal branches write a
   per-image refusal under `art_no_still` (`{"ts","url","reason"}`; the pre-upgrade
   bare-timestamp entries are still honoured) for `ART_NO_STILL_TTL_SEC` (30 d), and
   `_drop_refused_art` drops refused images while fresh, so one unfixable image cannot
   hide a fixable neighbour. `best_remote_still` no longer swallows transport errors (it
   returns None only for a genuine no-landscape-still): a lookup failure is logged and
   remembers NOTHING, so a TMDB blip cannot hide an image for 30 days.
   `art_still_tried`/`art_no_still` survive a disk-signature reset like `pids`.
2. **The stuck counters are persisted.** `_note_problem_pass` advances the per-problem
   counts and forgets lines that stop being reported; `run_once` now writes the report
   BEFORE `_save(STATE_FILE, state)` — it ran after, so the counters never survived and
   an unfixable `[auto]` line could never be demoted to NEEDS REVIEW (the DW art loop ran
   32 passes and was still `[auto]`). A healthy pass clears the show's counters.
3. **The escalation postcondition names the work.** `_metadata_repair_state(only_names=)`
   measures the exact episodes the run was handed, never the whole show; the measured
   race (BoJack `(0,0)->(2,2)` while the run did nothing) can no longer turn a no-op into
   a change or a live-ingest move into a failure.

No title, season, episode number, path or digest is written into the code; the rules are
`ART_DUP_MIN`, the provider's own ranking, the existing TTL, and the file names the
problem itself carries.

**Tests.** New `scripts/test_media_doctor_repair.py` (registered): the action decision
all three ways; fresh/expired/legacy refusal filtering; one image's refusal cannot hide
its neighbour; the full live loop (pass 1 adopts once and records the URL, pass 2 refuses
without re-adopting and the detection side drops it); a lookup failure records nothing;
a NEW provider URL is adopted at once and clears the stale refusal; dry-run records
nothing; and the stuck counter persisted/demoted/forgotten plus the healthy reset.
`test_metadata_heal.py` Part 4 (registered): an unrelated fix does NOT count as repairing
the target, and a target fix DOES count while the pack keeps filing.

**Live acceptance (owner-visible, pasted).** DW (1963), scoped passes on the live library:

```
[media_doctor] FLAGGED Doctor Who (1963): 3 episode image(s) are not real stills (identical to 2 other episode image(s))
[media_doctor]   -> Doctor Who (1963): re-adopt 3 bogus episode image(s) from the provider        # pass 1 records art_still_tried = the one TMDB URL for all three
[media_doctor]   -> Doctor Who (1963): 3 episode image(s) left as-is (the provider's own still is already applied and still shared; re-checked after 30 days)   # pass 2 refuses, art_no_still reason=already-tried
[media_doctor] pass done: 0 show(s) flagged, 0 auto-fixed, 0 pending human/AI review             # scoped re-checks: DW (1963) and Family Guy (1999)
```

The 12:18:03 report (written by the new code, PID 76619) has DW (1963) gone; Family Guy
kept only its 11 just-refused portrait stills (`-> fixed: 11 episode image(s) left as-is
(provider has no real still)`), whose scoped re-check prints `0 show(s) flagged`.
`bash scripts/verify_fleet.sh` → **ALL CHECKS PASSED** (69 blocking checks; the new check
is `test_media_doctor_repair.py`). A kickstart at 12:21 (PID 89183) started the next pass;
it is escalating Friends (1994), the live ingest it is mid-identify on — the doctor's
normal work, unrelated to this fix.

### Shipped 2026-09-25 — §15: the harness computes alternates, re-types are attributed, and a misfiled slot is repaired from its identity

**One systemic seam, three packs.** Friends (`1a6558e5…`) and Family Guy
(`705febda…`) both parked on the merge-unresolved seam: a model that re-types `src`
instead of copying the skeleton's path (Friends dropped the torrent root's closing `)`
on all 32 Featurettes) lost the four whose basenames repeat across season folders, and
one unresolved file parked a 182 GB pack. American Dad (`06dd53e1…`) parked when a
wave-0 plan (from the pre-`4202bb3` stale digest) filed `S10E06 - Independent Movie` at
`S04E06`; the digest fix removed the cause but not the wrong-slot file, and nothing
could compute its true slot.

**The fix, all computed.**

1. **Merge attribution by path, not basename alone.** `merge_skeleton_plan` matches the
   model's entry to a skeleton file by exact `src`, then a unique 3-component tail, then
   a unique parent+basename tail, then a unique basename. Each fallback claims only when
   exactly ONE skeleton file can match, so a genuine ambiguity still goes unresolved.
   The skeleton's on-disk `src` always wins, and the prompt now says so explicitly.
2. **Alternate cuts are computed, not modeled.** `plan_skeleton` groups files sharing a
   release `SxxEyy` whose names reduce to the SAME version-stripped title
   (`journal.alternate_title_core`) and slots them at one destination;
   `library._collapse_same_episode_alternates` keeps the ranked survivor
   (`DUPLICATE_DEPRIORITIZE_MARKERS`, then size) and records the sibling in
   `_deduped_dropped` (accounted-for, so the coverage contract never parks). The core
   comparison is exact on a trailing version parenthetical, so `II` vs `III` and
   `Part 1` vs `Part 2` never collapse. The model's authored title/plot/ids are carried
   onto whichever cut survives.
3. **An unresolved file is a fixable rejection.** `run_identify` hands
   `merge_skeleton_plan`'s `unresolved` list to the NEXT provider (persisted like any
   rejection, with those basenames as its `--require-list`); only a chain that never
   completes leaves the wave retrying. One incomplete free-model answer no longer parks
   a pack.
4. **Release-numbering AGREEMENT is a computed fact (the converse of 10.9).** For a
   pack whose titled files match the provider at their OWN `SxxEyy`
   (`release_identity_map`, dash titles tag-cleaned and admitted exact-only at the
   release key — the real American Dad mirror computes **346** claims against TMDB
   1433, including `S10E06`),
   `validate_plan(identity_map=)` refuses a remap to another numbered season, and the
   prompt states the agreement. Season 00 is exempt — that shelf has its own scheme.
5. **A computed repair path, `scripts/repair_slots.py`.** Numbered episode: its content
   title (a LOCKED nfo, else the filename — an unlocked nfo is Jellyfin's scrape of the
   slot and names the wrong episode) is matched against TMDB and re-filed through
   `refile_season` mapping machinery (remote move, mount unlink, `dbhook` purge/record,
   inventory/sync-state rewrite, then a locked destination nfo authored from TMDB).
   Specials: the library's OWN era-ordered Season-00 shelf is the authority
   (`library.specials_scheme`, persisted to `state/specials_schemes.json` and told to
   the AI as fact); a slot collision is resolved by air-date order — the file out of
   order moves to the shelf's next free slot, never to the provider's number — and a
   sidecar carrying a foreign provider number is queued for rewrite.
6. **Doctor guard.** `media_doctor._slot_disagreement` reports a filed episode whose
   `.nfo` `<season>/<episode>` disagrees with its destination filename
   (`episode_slot_mismatch`, `auto: false`); `find_show_folder` is year-aware (it
   matched Doctor Who (1963) for (2005)).

**Replay.** 53 titled historical plans: 6 computed identity maps, **0 remap
contradictions**; 0 historical same-episode alternate collapses (the same-slot guard
had always refused them); the new rejection changes no accepted plan. `media_doctor`'s
live DW shelf guard reports exactly the two known sidecars (E16 under S00E04, E149
under S00E04) and nothing else.

**Tests.** New `scripts/test_skeleton_merge_feedback.py` (alternates + II/III and
Part 1/2 controls, alternate merge and metadata carry-over, coverage accounting, tail
attribution, incomplete-plan feedback, identity guard + Season-00 exemption, replay)
and `scripts/test_slot_repair.py` (specials scheme from filenames not nfo numbers, AD
remap, DW collision → S00E23, Mysterio nfo fix, ambiguous collision refusal, doctor
guard, worklist replay). Registered in `verify_fleet.sh`: **67 blocking checks, ALL
CHECKS PASSED**.

**Live repairs applied through the tool (before ship).** American Dad's
`Season 04/S04E06 - Independent Movie` moved to
`Season 10/S10E06 - Independent Movie` (remote on `automega306`, records rewritten:
1 `chunk_filed` path, 1 `applied` entry; `library.db` 1 superseded + 1 recorded).
Doctor Who's `S00E04 The End Of Time Part 1` moved to `S00E23` (remote on
`automega282`), its new locked nfo carries `<episode>23</episode>`, and
`S00E04 The Return Of Doctor Mysterio`'s nfo was corrected from `<episode>149</episode>`
to `<episode>4</episode>`.

**Deploy + re-drop.** Shipped as `1dff91a` via `ship-fleet.sh` at a created
`ai_runner` lull (the three ingest daemons were SIGSTOPped so the in-flight run could
finish; the restart also forced Media-Syncer's remote rescan so the moved files
reappeared through the mount). New daemons: torrentingest 84268, directingest 84301,
driveingest 84381; mediafs 84172 / mediasync 84198. Then the three sources were moved
out of `failed/`: Family Guy (`705FEBDA…torrent`) and Friends (the named `.torrent`) to
the watch root, and American Dad from the `state/torrent_sources/06dd53e1…torrent`
mirror — `failed/` now holds only `.DS_Store`.

**Post-deploy acceptance (owner-visible).** Log line: `Re-queuing a failed torrent
dropped again` for all three at **2026-09-25 05:07:28** — Family Guy "resuming chunked
waves at 96 file(s) already filed", American Dad "…at 34", Friends wave 0 from zero.
Mount after the rescan:

```
Shows/American Dad! (2005)/Season 10/… - S10E06 - Independent Movie …-playWEB.mkv  (+ .nfo, locked S10E06)
Shows/Doctor Who (2005)/Season 00/… - S00E23 The End Of Time Part 1.mp4            (+ .nfo, locked S00E23)
Shows/Doctor Who (2005)/Season 00/… - S00E04 The Return Of Doctor Mysterio.mp4     (+ .nfo, locked S00E04)
```

Jellyfin API after `Library/Refresh` + a targeted `Library/Media/Updated` on Season 10:
`S10E6 | Independent Movie | …/Season 10/…-playWEB.mkv`; `S0E4 | The Return Of Doctor
Mysterio`; `S0E23 | The End of Time Part 1`. The ffmpeg thumbnail probe on the
pool-only S10E06 times out until the file hydrates, which is the normal cold-file
behaviour. The tool's sidecar writer now always targets the SSD (`repair_slots._place_path`):
writing through the mount into a pool-only directory has no writable lower and failed
with `PermissionError` (fixed after the deploy, `save-and-push.sh` — no daemon loads
the tool). **Still to confirm next session:** the three resumed packs' waves filing on
the fixed code (Family Guy's S07E07 alternate recorded in `chunk_dropped`, Friends
32/32 Featurettes, American Dad's S04E06 *The 42-Year-Old Virgin*), and the Simpsons
§15.4 wave/terminal check.

### Shipped 2026-09-24 (evening) — the show summary counts evicted episodes, and a proven collision retries instead of parking the pack

**Three packs parked today; two of them by one measured blindness.** The Simpsons (1989)
(`1d9098aa…`, 700 GB) parked at 10:21 CDT as `chunked: the wave plan left 2 file(s)
unaccounted`; American Dad! (2005) (`06dd53e1…`) parked at 06:16 the same way. The plans
were not the fault.

**Root cause.** `show_metadata_summary` walked the SSD (`~/Media`) alone — the cache, where
anything evicted to the pool is absent (HANDOFF §2.1). The mount held 40 Simpsons episodes
with Season 03 = 4; the digest the run was built from said "Season 03 (2 eps)" and knew no
other seasons (measured: the cached entry's directory signature still matched, so the subset
was served). The prompt tells the run the library's season counts are ground truth, so the
model renumbered the wave's S03E05-S03E24 down by two to continue from E02 — onto
S03E03/E04, which wave 22 had already filed. American Dad! the same hour: the SSD said no
seasons; the library held 34 episodes.

**The fix.** `_episode_videos_complete` enumerates a show's episodes as the union of the
disk walk and Media-Syncer's remote inventory — the same complete view `_comics_coverage`
already reads ("the pool holds every comic even when the local copy is evicted"). The walk
covers files too new to be uploaded and fixture directories; the inventory supplies every
evicted episode. Sidecars survive eviction, so the `.nfo` halves stay local and the mount is
only a fallback path. `_SUMMARY_CACHE_VERSION = "v2"` refuses the pre-fix subset readings.
Measured after: The Simpsons 0.08s for 40 episodes (the mount walk was 8.9s), One Piece
(1999) 1155 episodes in 0.32s, and the digest line now reads `Season 00 (2 eps), Season 01
(11 eps), Season 02 (22 eps), Season 03 (4 eps), Season 33 (1 eps)`.

**Second defect: the collision had no identity witness for chunked waves.**
`_collapse_existing_episode_collisions` saw the planned S03E03 collide with the existing
S03E03, `_existing_episode_mismatch` found no evidence (`journal.source_titles()` indexed
only `plan.files`, and a chunked record has `plan: null`), so the planned file was dropped
silently and the coverage contract parked the whole release terminally. Two additions:
`source_titles()` now indexes the record's `applied` entries (the staging source path
preserves the release filename), and when the journal is still silent the existing file's
own NAME is the witness — compared on tag-cleaned titles, because two files of one release
share their whole tag tail (raw similarity 0.838, a hair under the 0.85 same-episode bar).
A proven mismatch raises `CollisionPark` (a `PlanError` subclass) so the chain retries the
plan instead of the release failing, and the chunked per-file fallback returns the PARK
verdict — never freeing bytes on a collision (§10.2).

**Replay.** Every journal plan entry with a titled source at a known slot (2,052): 8 would
now raise, all eight the Smurfs incident shape (the old dvdrip's wrong-slot S01 files
against the replacement pack's correct ones), 0 false positives.

**Tests.** New `scripts/test_show_summary_inventory.py` (registered after the digest-scoping
check): an inventory-only episode is counted and its local sidecar still decides
locked/blank; the digest line carries it; a pre-fix cache entry cannot serve its subset; an
unreadable inventory falls back to the walk; fixture dirs are untouched; one show's inventory
never bleeds into another. `test_existing_collision_identity.py` extended with the name
witness (different episode parks; same episode under different tags still collapses;
bare-numbered name proves nothing), `_clean_episode_title`, the `applied` journal witness,
and the per-file PARK verdict.

**Acceptance (owner-visible).**
* `bash scripts/verify_fleet.sh` → **ALL CHECKS PASSED** (65 blocking checks).
* Live summaries: The Simpsons 40 eps / Season 03 = 4; American Dad! 34 eps / Seasons 01-04
  = 6/16/11/1.
* Pushed `4202bb3`, deployed **2026-09-24 21:37:41 CDT** via `Torrent-Ingest/scripts/ship.sh`
  (torrentingest 88328, directingest 88336, driveingest 88349). Two identify runs were in
  flight and were interrupted by the restart; their waves retry next cycle.
* The Simpsons `.torrent` was moved from `failed/` back to the watch root at ~21:38 and is
  visible to `find_drop_files`; registration was still pending at session end behind a long
  `advance()` identify sweep (the sweep re-registers within `REGISTER_REFRESH_SEC` once the
  current wave's chain returns). It resumes from proven progress; **the next session must
  confirm the wave files and the record's terminal state.**

**Still parked, distinct seams (no new download needed for either).**
* **American Dad! (2005)** (`06dd53e1…`, failed 06:16). Its wave-0 plan filed source
  `S10E06 - Independent Movie` at `S04E06`; the wave-34 plan (correctly) files `S04E06 - The
  42-Year-Old Virgin` at S04E06, which collides with the wrong-slot file. With the digest
  fixed a re-drop plans correctly, but the wrong-slot S04E06 still blocks; the repair is a
  reviewed re-file of that one file (`refile_season.py --mapping`), not a re-download.
* **Family Guy - Seasons 1 to 20** (`705febda…`, failed 21:27). Wave w96 holds TWO files
  named `S07E07 - Ocean's Three and a Half` (Uncensored + Bale Scene / Uncensored +
  Commentary Audio Track); the harness deliberately leaves same-release-key files unslotted
  and the model placed only one, so the coverage contract parked the pack. A different seam:
  the model CAN express "both files, one destination" (the intra-torrent duplicate collapse
  accounts it) but does not know to; the next fix is to compute the same-key duplicate
  relation in the harness (or say it in the prompt) and replay it. Bytes are on disk; no new
  torrent.

### Shipped 2026-09-24 — a private trackerless `.torrent` is refused at registration, not after a 24h stall

**What looked like a duplicate was a structurally dead drop.** The owner moved a failed
drop back to the watch root (`BE7A1BA6… 2.torrent`, SpongeBob S16) suspecting the iCloud
duplicate killed it. It did not: registration on 2026-09-22 tracked the " 2" copy and
filed the clean copy under `finished/` (§10.6 machinery, working as designed). The
`.torrent` itself is undownloadable — `info.private == 1`, no `announce`, no
`announce-list`, no `url-list`. A private torrent may not use DHT/PeX/LSD (a live probe of
qBittorrent printed "This torrent is private" on all three rows through
`/api/v2/torrents/trackers`), so with no tracker and no web seed it has zero ways to reach
a peer. It sat from 2026-09-22 08:19 to 2026-09-24 02:12 CDT and `_abandon_stalled`
retired it with "stalled 24h with no progress (no peer activity)" — the 24h policy working
exactly as designed on a drop no re-drop could ever heal.

**The fix.** `qbt.undownloadable_reason` computes the verdict from the bytes already in
hand (no network); `ingest._refuse_undownloadable` fails the drop through the normal
`_fail` path on both fresh-record paths of `register_new_torrents` (new hash and terminal
re-drop). The reason lands in the journal, the source is filed under `failed/`, the watch
top level stays clean. Public DHT-only drops (the dominant shape) and private drops WITH
trackers are untouched; a parse failure returns None and takes the normal path.

**Replay (285 real `.torrent`s: every journal-named file plus all state folders incl.
`failed/` and the local mirror): 3 refused, all private+trackerless by an independent
decoder** — the three byte-identical copies of the one known-broken drop (`BE7A… 2` in the
watch root, the `finished/` copy, the `state/torrent_sources/` mirror). 0 false positives.

**Acceptance (owner-visible).** Pushed `48f9fd9`; deployed 2026-09-24 05:47 CDT at an
`ai_runner` gap by `Torrent-Ingest/scripts/ship.sh` (torrentingest 72073, directingest
72081, driveingest 72094). The daemon picked the moved-back drop up on its first cycle:

```
[2026-09-24 05:47:02] Filed failed .torrent under failed/: SpongeBob SquarePants S16 1080p AMZN WEB-DL DDP2 0 H 264-BTN
[2026-09-24 05:47:02] FAILED SpongeBob SquarePants S16 1080p AMZN WEB-DL DDP2 0 H 264-BTN: private torrent with no tracker and no web seed: DHT, PeX and LSD are switched off by its private flag, so it can never find a peer; re-create the .torrent with its announce list -- re-dropping this file cannot help
```

The file now sits in `iCloud Drive/Torrents/failed/BE7A1BA6228AD4804E934908A493662D6FE4738D 2.torrent`
and the journal record carries the same reason. The release needs a `.torrent` that
actually carries trackers; neither copy of this one can download.

Tests: new `scripts/test_undownloadable_torrent.py` (35 checks: every private trackerless
shape refused; public-trackerless, tracked-private and web-seeded shapes accepted;
registration end to end; terminal re-drop fails fast again; real corpus replayed). It is
registered in `verify_fleet.sh` after the truncated-recovery check. Both that test and
`verify_fleet.sh` print ALL CHECKS PASSED.

### Shipped 2026-09-23 (late) — the 70-file Smurfs re-fetch: a reordered pack gets the computed skeleton, and a bracket-less title is a weak witness

**What was actually wrong.** The §6 `In flight` row said the drop was "waiting on
free-provider capacity". It was not: on 2026-09-20 20:58 every one of the 14 providers
produced a plan `validate_plan` refused and `direct_ingest` parked the whole drop in
`~/Downloads/DirectIngest/.failed/The Smurfs (1981)/` (70 files). The failure lines name
three real shapes:

* `file[60] "The Smurfs S07E02 (Jokey's Joke Book).mp4": ... computed S07E15, but the
  plan files it at ...S07E14` — the harness was right, the model copied the release
  order. Every provider made a version of this mistake because 70 files is **below the
  skeleton size floor** (`IDENTIFY_SKELETON_MIN_FILES` 150): the harness computed the
  title map but still asked the model to enumerate all 70 release-ordered files.
* `file[1] 'The Smurfs S01E06 (The Astrosmurf).mp4' ... computed S01E01, but the plan
  files it at ...S01E01` — the pre-session-2 guard reading the model's annotation
  fields, fixed 2026-09-20; kept as evidence the floor was the live cause.
* `file[39] Season-0 special has no episode_title` — the computed map puts the Springtime
  Special (release `S01E40`) in `S00E01`, where the validator requires
  `episode_title`+`plot` (specials are always locked); nothing supplied the title.

**The 70th file.** `The Smurfs S07E49 - Nobody Smurf.mp4` carries no brackets. Its
release number is another episode's slot — the shelf's `S07E49` is *Hefty's Rival* — and
TMDB (the provider Jellyfin scrapes) has *Nobody Smurf* at **S07E27**. The bracket-only
parser was deliberate (a loose dash fallback replayed as 54 false positives on
2026-09-20), so this file had no computed destination at all.

**The fix (three parts, all computed).**

1. **A reordered pack always gets the skeleton.** `_skeleton_needed` returns true for a
   large release OR any release with a computed title map. A pack whose own numbering is
   measured to be permuted is exactly where the model's copy of it is wrong; the
   enumeration must come from the harness at any size.
2. **A bracket-less title is a weak witness, gated on the strong one.**
   `release_dash_title_entries` reads `SxxEyy - Title` names; `_title_claims` consults
   them **only after the bracket witness has proven the pack reordered** (some bracket
   claim differs from its release key) and only through the exact/unique pass — never the
   ratio pass. First draft (weak witness on its own) reproduced the 2026-09-20 class
   exactly: **50 contradictions** over the journal (a multi-show pack whose every title
   is `Show (Year) - SxxEyy - Title.mkv`, legitimately filed across seasons). Gated:
   **0 contradictions**, and the shape the gate exists for — 69 bracket rows plus the one
   dash file — still computes `(7,49) -> (7,27)`.
3. **A computed Season-0 target gets its title, and the model is told it owes the plot.**
   `plan_skeleton` pre-fills `episode_title` from the release's own name for Season-0
   targets, and the prompt now lists `SEASON-0 SPECIALS THAT NEED A PLOT (n)` beside the
   files that need a decision. `plan_skeleton` also stops falling back to the release
   number for unmatched files in a reordered pack (the silent-collapse shape).
4. **A chunked wave's skeleton names real files only, and the model cannot clobber a
   verified src.** Found while this fix was being verified: the live American Dad wave's
   every provider died on `file[1] src does not exist:
   .../American Dad! (2005)/American Dad! (2005)/Season 01/...`. Two causes, both in the
   new seam. qBittorrent's own file list prefixes names with the torrent's root folder
   while `content_path` IS that folder, so `content_path / name` doubled it; and the
   skeleton covered all 390 files of the release when only the wave's files exist on disk
   (`validate_plan` requires every `src` to exist). `_resolve_release_abs` now resolves
   each name against the disk (stripping one duplicated root, handling a single-file
   torrent) and drops names not on disk — so the skeleton, the title map and the require
   manifest are exactly the wave. `merge_skeleton_plan` no longer lets the model's `src`
   overwrite the harness's verified path (it matched the entry by unique basename and
   replaced a real path with one that did not exist). Dry run of the live wave: the
   390-file list resolves to **32** on-disk files, the skeleton is 32/32 slotted, the
   merge fills 32, and `validate_plan` accepts it. **Live, after the restart: the wave
   filed `34/390` at 22:15 CDT (`skeleton merge: filled 1 episode destination(s)`,
   Jellyfin rescan triggered) — the first wave to file on the fixed path.** The skeleton
   trigger stays on the RELEASE list (`_skeleton_needed`), so every wave of a large pack
   gets a computed enumeration while the skeleton itself is built over the resolved
   on-disk subset; a wave below the floor gets one only when the title map proves its
   numbering permuted. **Deploy state:** `ac1c37a` is the running code (restart 21:49,
   torrentingest 49948 / directingest 49957 / driveingest 49969). `bde8d71` corrects the
   `wrote a N-file skeleton` log line, which read "390-file skeleton" beside a 32-entry
   file; it is committed and will land with the next daemon restart — it changes only
   that log line, nothing functional. A pgrep-gated watcher (no `ai_runner.py` for
   20 s, then `Torrent-Ingest/scripts/ship.sh`) was left waiting for a lull; if the
   daemons restarted after this section was written, that is why, and the PIDs above
   are simply one restart old.

**Acceptance (owner-visible).**

* `python3 scripts/test_release_title_numbering.py` → **all checks passed** — the new
  parts pin the exact dash claim, its two no-claim shapes (scene tag, multi-episode
  marker), the bracket witness's precedence, the skeleton trigger with and without a
  map, the needs-mapping marking, the Season-0 title pre-fill, the merge, root-resolved
  release paths (duplicated root, mirror-style name, missing file, single-file torrent)
  and the verified-src rule.
* `python3 scripts/test_release_title_numbering.py` replay (Part 9) →
  `replayed 53 titled-release plan(s): 0 contradiction(s), 0 dash-only claim(s)`.
* Offline dry run of the real drop through `plan_skeleton` → `merge_skeleton_plan` →
  `library.validate_plan`: `merged 70 filled 69 unresolved []`, the special at
  `Season 00/The Smurfs (1981) - S00E01.mp4` with `episode_title` and a plot, `Nobody
  Smurf` at `Season 07/... - S07E27.mp4`.
* `bash scripts/verify_fleet.sh` → **ALL CHECKS PASSED.**
* **Live re-drop (2026-09-23 22:01–22:06 CDT).** The parked directory was moved back
  into `~/Downloads/DirectIngest/`; the fixed daemon computed the map for all **70**
  titles, wrote the 70-file skeleton, and openrouter/nvidia-nemotron's plan was
  accepted at 22:05:58 — the first provider that served. The log:
  `filed The Smurfs (1981) -> ...` (70 destinations) then
  `removed source The Smurfs (1981) (70 file(s) verified in destination)`.
  Owner-visible result through the mount: **Season 01 = 39 mp4 (was 0)**, Season 00 = 6
  (the Springtime Special at S00E01), **405 mp4 in the show folder** (was 340), and the
  three shapes are at their computed slots — `S01E31` = *The Smurfette*, `S07E15` =
  *Jokey's Joke Book*, **`S07E27` = *Nobody Smurf***, with the shelf's `S07E49` left as
  *Hefty's Rival*. The drop directory is gone; Jellyfin rescan triggered.

### Shipped 2026-09-23 — a stall is not a swarm verdict, and the partial bytes stay

**The owner's report.** Four recent failures — `Bob.s.Burgers.S01E01-13` (45% at failure),
`S02E01-09` (22%), `S03E01-23` (1%, chunked), `S06E01-19` (0%) — were moved out of
`failed/` and re-queued, and would "likely fail again". Each journal line reads
`stalled Nh with no progress (no seeders/peers); abandoned to release the download budget`
(4h/6h/7h/8h).

**Root cause (measured, not inferred).** `_abandon_stalled` selected a 4-hour deadline
whenever qBittorrent reported `availability < 1.0`, on the premise in `config.py` that this
means "NO complete copy anywhere in the swarm". It does not: availability is computed from
the peers this client is *currently connected to* plus its own pieces, so during any stall
it collapses to our own completion fraction and reads < 1 even in a swarm full of seeders
(live: a stalled S04 at 5.69% progress reported availability 0.056 — exactly its own
fraction). Every stalled torrent therefore took the 4h path. The four packs were paused by
an ordinary overnight seeder gap (S01 had gone 1% → 21% → 45% over the day) and were killed
the moment the daemon next reached them — after an identify run that blocks the sweep for
hours. Worse, the abandon called `qbt.remove(delete_files=True)`: the partial payload was
deleted with the torrent, so every re-drop restarted from zero and stalled again — the loop
the owner saw. `_fail`'s own message promises "local download left for inspection"; every
other failure path leaves the bytes; this one did not.

**Measured cost (journal + `~/Library/Logs/TorrentIngest.log`).** 48 stall-abandons in the
journal; 35 of them are still in the current log with a recoverable progress line; 6 had
partial data deleted, **7.40 GB** in total — S01 3.56 GB (45%), Croods S06 2.26 GB (83%),
S02 1.24 GB (22%), FMA Brotherhood E58 0.32 GB (36%), Dropkick E06/E10 0.02 GB. Not one of
those hashes ever completed (they were never automatically retried). The old code also took
one **more** victim in the gap between this fix being pushed and the daemon restart that
afternoon — `Bob.s.Burgers.S14` at 12:30 CDT (`stalled 5h`), its partial bytes deleted; it
is chunked now and re-fetching. That gap is exactly the standing rule's hazard, and the
owner approved the restart with a run still in flight rather than wait hours for a chain
that never leaves the identify phase.

**The fix.** One deadline, selected by peer activity alone: `STALL_ABANDON_SEC` (24h) from
the later of qBittorrent's `last_activity` and a chunked wave's `wave_started_at`.
`STALL_ABANDON_NO_COMPLETE_SEC` is deleted (an unused guard is a thing someone turns back
on later). The abandon now removes the torrent with `delete_files=False` — a re-drop of the
same source resumes from the bytes on disk, and `artifactjanitor` reclaims a directory that
is never retried after its own 7-day grace. The rationale lives on `_abandon_stalled` and
`config.STALL_ABANDON_SEC`, and `scripts/test_chunked_stall_clock.py` pins both directions
plus byte preservation: a fresh wave survives a stale clock; a wave idle past 24h is still
abandoned; a non-chunked 8h stall at 45% with `availability 0.45` and no seeder known is
**kept** (the exact 2026-09-23 shape); every abandon passes `delete_files=False`.

**Why this is not the Tailscale bind.** qBittorrent binding to the `100.79.20.10` CGNAT
address and riding the rotating Mullvad exit node is the documented design (`tailscale_up()`
gates downloads on it; Media-Syncer rotates the exit for MEGA throttling). These are
DHT-only magnets with no trackers (`.torrent` `announce` absent, `url-list` empty), so
seeder gaps of hours are normal — which is exactly what the 24h window and the kept bytes
are for. No VPN or qBittorrent configuration was changed.

**Acceptance (owner-visible).**
* `python3 scripts/test_chunked_stall_clock.py` → `ALL CHECKS PASSED.` (17 checks: the three
  chunked-clock parts, the non-chunked regression, byte preservation both paths, controls).
* `bash scripts/verify_fleet.sh` → `ALL CHECKS PASSED.`
* Pushed as `c9fd3aa`, then deployed **2026-09-23 20:08 CDT** with
  `Torrent-Ingest/scripts/ship.sh` per the owner's explicit "restart now" (the identify
  chain had no gap; see the S14 note above). New daemon PIDs: `torrentingest` 19764,
  `directingest` 19772, `driveingest` 19785; 0 tracebacks in `TorrentIngest.log`; the
  reaper (PID 6539) was not in the script's label set and was left draining.
* The four were admitted 11:53 CDT and survived the old code to the deploy (the identify
  chains blocking the sweep is what let them; nothing new was abandoned). Live state
  measured after the restart: **S02 27.0% with `availability 2.27`, `num_complete=2`,
  2 seeds connected; S06 10.8% with `num_complete=2`; S01 1.2% `stalledDL` with
  `num_complete=4`** — a stalled torrent with four complete peers known, which is exactly
  the shape the old 4h rule called "no seeders/peers" and would have destroyed again;
  S03 parked between chunked waves (0/23 filed, next wave enables when it fits). No
  `stalled`/`abandon` line appears in the log after the restart. The S01/S02 bytes they had
  before the first failure were already gone (deleted by the old code), so they re-fetch
  from zero; anything fetched from now on survives a retry.

### Shipped 2026-09-20 — the five computed facts, the verified self-heal, the scalable plan

Six registered tests (check #55–#60): `test_provider_id_verify.py`,
`test_series_identity_heal.py`, `test_manga_mislabels.py`,
`test_release_title_numbering.py`, `test_metadata_heal.py`, and
`test_no_incident_hardcoding.py` (no machine paths or digests in executable string
literals -- comments and docstrings may still name incidents). The report
`scripts/verify_owner_report.py` prints the owner-visible acceptance per §10.0 and is
advisory in `verify_fleet.sh` (a parked Smurfs or a draining purge must not gate).

**10.3 — a provider id is verified before it can pick art.** `tmdbguide.show_identity`
fetches the provider record (`/tv/{id}` + `/external_ids`); `library.verify_provider_ids`
(now called by `identify` on the model's plan, before `validate_plan` — deliberately not
inside `validate_plan`, which tests and offline tools call without network) strips a tmdb
id that answers 404 or names a different year (>1), and a tvdb id that disagrees with
TMDB's own mapping. A network error fails open. Live: TZ (2019)'s `tmdb_id 80979`
(*Too Cute*) / `tvdb_id 325542` would have been stripped at filing.

**10.3 — media_doctor owns series identity and title art.** The trigger is computed:
`premiered` vs TMDB `first_air_date` (a stale `<year>` with a matching premiere is
harmless — four live shows carry that shape), now plus `enddate` vs TMDB's
`last_air_date`: Jellyfin's saver re-stamps `enddate` from its DB and the field is not
lockable, so without that trigger the TZ `enddate` silently reverted to Too Cute's
2013-03-06 beside the corrected 2019 premiere (repaired live, and the item's `EndDate`
is now set too). The repair re-matches the Jellyfin item
FIRST (`RemoteSearch/Apply`, order measured: a `replaceAllMetadata` refresh after the nfo
write made Jellyfin rewrite tvshow.nfo and the S02 nfos from its stale DB rows), syncs
season items, locks the item (`LockData`, valid `LockedFields` only), then writes nfos
LAST and replaces `folder.jpg`/`landscape.jpg`/`seasonNN-poster.jpg` with the verified
identity's art. `_write_nfo_title` now always emits/repairs `<season>`/`<episode>`, and
null Jellyfin indexes are set on the item DTO.

**10.5a–e — the manga tiers are computed from the archives.** New `comicfacts.py` reads
volume associations (`(vNNN)`), chapter markers (`cNNNN`/`dNNNN`, bare `1176-001`),
edition (`[Digital CC] [PZG]` vs `[VIZ Media] [1r0n]`) and the mislabel shape from the
entries themselves, cached in `state/comic_archive_facts.json`. `manga_volume_map`
persists `total_volumes`/`total_chapters`/`shelf_volumes`/`shelf_ceiling` and merges the
shelf's exact sets over MangaDex; `comicfacts.ceiling_for` gives the plan-time ceiling
(the larger of AniList's total and the shelf's). `validate_plan` refuses a `vNNNN` above
the ceiling whose archive is chapter pages, and refuses a grey copy superseding a
coloured file. `librarydb.item_key` is colour-aware for volume/chapter (two rows for two
editions; `mark_superseded`/`_supersede` with unknown colour match grey only — a grey or
unknown file may never kill the coloured row). `chapter_volume_reconcile` reads colour
from `comicfacts` and lets a surviving coloured volume cover chapters; a coloured chapter
is still only superseded by a coloured volume. The One Piece franchise row is in
`config.COMIC_FRANCHISES` (Ace's Story nests; a flat master that already holds the
master's own files stays flat until the owner decides on the migration), and
`resolve_comic_folder(colored=True)` no longer bypasses the table. `identify.md` and
`README.md` teach the current layout.

**10.9 follow-up (2026-09-20 afternoon).** The generic owner report lists the Smurfs collapse first, then three
COMPLETED legacy releases whose plans collapsed destinations: `Pokemon Horizons 115-123
English Dub` (1), `[Deadmau-RAWS] Shokugeki.no.Souma.OVA.2016-2018` (5), `[MTBB]
Monogatari Series Off & Monster Season S1` (1). These predate the merge collision guard
and may be benign same-episode duplicates or the Smurfs disagreement shape; each needs a
content-verify pass (`probe` the survivors against the guide) before being dismissed.
The Smurfs pack itself is the 32-collapse case already recorded in §10.0 row 5.

**10.9 — release-order packs compute their broadcast numbering, and plans scale.**
`identify.release_title_map` matches each `SxxEyy (Title)` filename against TVMaze and
returns the broadcast slots only when they actually differ; the block is stated in the
prompt as fact and `validate_plan(..., title_map=)` refuses a copied release number. Above
`IDENTIFY_SKELETON_MIN_FILES` (150) the harness writes a deterministic skeleton of every
release file (all computed slots) and the prompt names it; `ai_client` now checks plan
coverage against a `--require-list` manifest before the run ends and asks for the exact
missing slice twice. The coverage guard still parks as the last line of defense. Measured
on the Smurfs pack: 362/405 titles matched, 347 differ, `S01E01 (The Smurfette)` ->
`S01E31`, 409-file skeleton (49.7 KB).

**10.4 — metadata self-heal sees the mount and verifies its writes.**
`audit_metadata.shows_root()` is the mediafs mount (Toriko's 147 videos are evicted from
`~/Media`, which is why the audit found 0 episodes); `repair_metadata._guide_index`
accepts name-only TVMaze rows (Toriko: 146 names, 0 summaries); `_fill_synopses` fills
plots from TMDB episode overviews (49 available) without overwriting the provider title;
`media_doctor.escalate()` snapshots `(blank_plots, junk_titles)` before the AI run and
charges `escalate_n` only when the sidecars actually improve.

**10.10F — `scripts/verify_owner_report.py` is library-wide, not incident-coded.** The
first cut named the five artifacts (a folder, two poster hashes, the Smurfs info hash);
that is evidence, not a check, and it cannot see the same fault anywhere else. It now
computes the invariants across the whole library: no `tvshow.nfo` whose `enddate`
precedes its premiere; no `vNNNN` mislabel, no chapter covered by an owned volume, and
no volume in both editions across EVERY two-tier manga series (the reconciler's own
functions, so it cannot disagree with an apply); and the journal's completed records
whose destinations were collapsed (content-verification review) and terminal records
parking unaccounted files. Queued purges report PENDING; already-purged paths report PASS while the inventory catches up; only bytes still visible through the mount or on the SSD fail. Read-only, exit 0
always, advisory in `verify_fleet.sh`. Its first run surfaced **the Smurfs collapse plus three legacy ones**
the incident-coded version could not see — see §10.9's note.

### Shipped 2026-09-20 (session 2) — the map follows Jellyfin, duplicates need proof, collisions see the mount

Three tool fixes, each with a registered test (`verify_fleet.sh`: check #61
`test_release_title_numbering.py` extended, #62 `test_duplicate_identity.py`, #63
`test_existing_collision_identity.py`; the direct-ingest release list is Part 6 of
`test_direct_ingest_media.py`). The full suite prints `ALL CHECKS PASSED`.

**The title map is computed against the provider Jellyfin SCRAPES.** `tmdbguide.episode_names`
(fetched, cached 7 days) now feeds `identify.release_title_map` whenever the library
folder pins a `tmdbid`; `epguide` (TVMaze) is the fallback. THE MEASURED DISAGREEMENT:
The Smurfs' *Locomotive Smurfs* is TVMaze S07E41 and TMDB/Jellyfin S07E43; TVMaze's S09
is one slot short of TMDB's three-part opener; the four specials live in TMDB's S00.
The July/September map placed 77 files one slot away from the title the owner sees, and
Jellyfin's own `.nfo` titles (rewritten from the scrape) sat beside the wrong episodes.
The matcher also gained a character-ratio pass with a part-digit rule
(`Wild Side - pt1` -> `(1)`, pt2 -> `(2)`; a digitless query against several parts gets
NO claim) and a collision-safe skeleton fallback: an unmatched file whose release number
equals a matched file's computed slot is marked needs-mapping, never filed there.
Replay: 15 titled-release plans in the journal, 0 contradictions. And
`library._reject_title_numbering` now checks the DESTINATION slot, not the model's
optional `season`/`episode` fields: on the re-fetch drop the model wrote the correct
`S01E06 -> S01E01.mp4` while leaving `(1, 6)` in those fields, and the guard rejected
its own computed slot (live, 2026-09-20). The fields are an annotation; the destination
is what gets filed.

**A same-stem duplicate is deleted only when the journal PROVES it is one.**
`media_doctor._classify_slot_collision` asks `journal.source_titles()` (the source title
each destination was filed from) when the two files share one `.nfo`: different
identities are a `misfiled_episode` (reported, never deleted), unknown identity is
reported, and only a proven same-episode pair may be auto-deleted. This is the fix for
the loss this session found: the old rule kept the higher-ranked container, deleted the
planner's `.mp4` at 38 Smurfs S01 slots, and left the older wrong-slot `.mkv`.

**A same-slot collision now sees the mount and checks identity.** `_collapse_existing_episode_collisions`
scans `MEDIAFS_MOUNT` as well as `MEDIA_ROOT` (an evicted episode was invisible, so the
replacement pack applied beside the pool-only dvdrip files), and when the journal says
the existing file's content is a different episode it raises `PlanError` — the release
parks, it is never a silent drop or an overwrite. Same-episode and unknown-identity cases
keep the historical collapse, so no new parks for ordinary work.

**The repair, through the tools.** `refile_season.py --mapping` (now accepting
comma-separated `--record` hashes and rewriting the plan's `dst_rel` alongside
`applied`/`chunk_filed`): 147 moves, 0 failures, 290 stale sidecars deleted, 142 db rows
superseded. `journal.source_titles()` is shared by the doctor and `library`, so a moved
file's identity follows it. Direct drops of 4+ videos are handed to `run_identify` as a
RELEASE (`direct_ingest._release_files_for`), so the same pack cannot file one way
through the torrent and another through `DirectIngest/`.

### Shipped 2026-09-20 (session 2) — the manga shelf: bare markers, wrong-series chapters, franchise layout

Owner report: odd One Piece chapters in Jujutsu Kaisen; covered repeats and bare
`cNNNN.cbz` files in One Piece; a nested One Piece folder with more repeats; and
`One Piece` + `One Piece - Ace's Story` should share a master. No hard-coded titles
anywhere: every rule is a computed fact.

* **`repair_manga_mislabels.py` created the bare markers.** Its rename wrote
  `c{ch:04d}{ext}` alone, so the seven `vNNNN` mislabels became `c1078.cbz`..`c1176.cbz`
  with no series. It now replaces the volume marker in the existing stem and prefixes
  the series label when nothing else names it (`One Piece v1176.cbz` -> `One Piece
  c1176.cbz`). `test_manga_mislabels.py` Part 5 pins it.
* **`library.validate_plan` refuses a destination that names only the marker**
  (`_BARE_MARKER_STEM`) and **a chapter above a FINISHED series' chapter total**
  (`comicfacts.chapter_ceiling_for`, from the persisted AniList total + status; ONGOING
  series have no bound). This is the guard that would have refused `Jujutsu Kaisen
  c1093.cbz` at plan time. `manga_volume_map` now persists `anilist_status`.
* **The reconciler learned duplicates, fractional chapters and wrong-series misfiles.**
  Same-number chapter copies collapse to the canonical, shallowest one (`c1151.cbz` +
  `c1151.5.cbz` no longer merge -- fractional chapters are distinct); a chapter above
  its folder-series' FINISHED total that another series' owned volume covers is purged
  as a redundant copy of that volume, otherwise reported. The coverage index is built
  from the WHOLE shelf even for a `--series` run. `series_label_for_rel` collapses a
  doubled master leaf, so `One Piece/One Piece/` is one series, not "One Piece One
  Piece". `test_manga_chapter_reconcile.py` covers all four directions.
* **The franchise layout is live**: `migrate_comics.sh --apply` moved 191 files
  (verified 191/191) into `Manga/One Piece/One Piece/` and `Manga/One Piece/Ace's
  Story/`, rewrote inventory/sync_state, and the empty old master/member folders were
  removed. The table row is the generator's evidence-based one (`build_comic_franchises`
  resolves it from the library; no title was typed in for this fix).

### Shipped 2026-09-21 — reconcile sees every witness: the One Piece re-queue storm

**Damage (measured live 2026-09-20 19:55–20:15).** The franchise migration moved 191 files
without rewriting the journal records that named them, and `reconcile.py` checked presence
against the inventory keys and `~/Media` (the SSD cache) only — the MOUNT and a moved key
both read as "gone". Every moved chapter completion was re-queued, re-downloaded and
re-filed on a loop, and the five chapters a volume already covered (**c1080, c1094, c1101,
c1120, c1122**) were re-fetched only for `chapter_volume_reconcile` to purge them again.
38 records carried the re-queue error; the re-identify runs those spawned were competing
with the Smurfs re-fetch for the same free-provider chain (`state/tmp` logs at 20:00–20:12
are re-queues, not new drops; openrouter was out of daily budget).

**The fix (three computed facts, plus a repair tool).**
* `reconcile._is_present_local` checks the MOUNT as well as the SSD — the module always
  said "local mount"; the code checked only the cache (HANDOFF §2.1).
* A moved file matches by CONTENT IDENTITY: basename + byte size, both carried by the
  inventory (`[account, timestamp, size]`). One unique candidate proves presence;
  ambiguous or different-size candidates prove nothing and are never guessed.
* `dbhook.purged_evidence` reads library.db's own supersede ledger (the read-only mirror
  of `_supersede_path`'s path→row mapping) and a completion whose files are gone from
  EVERY witness and deliberately superseded is CLOSED (`reconcile_closed`, plus the older
  `reconcile_dead` switch the running daemon honors) instead of re-acquired.
* `scripts/repair_journal_paths.py` applies the same facts to records that drifted before
  the fix: rewrites `applied`/plan/`chunk_filed` to the unique moved key and closes the
  all-missing-and-superseded ones. It never touches a record that still holds a file.

**Acceptance (owner-visible; pasted from the live run).**
* `bash scripts/verify_fleet.sh` → **`ALL CHECKS PASSED.`** — the new check is
  `test_reconcile_presence.py`, which proves both directions and replays the live journal:
  `completed records audited: 330 / present: 325 / deliberately superseded: 5 / genuinely
  missing: 0 / the path-only check would have re-queued: 5`.
* `python3 scripts/repair_journal_paths.py --apply`:
  `APPLIED: 0 record(s) rewritten, 5 closed as superseded, 17 stale error(s) cleared,
  312 already exact, 13 unresolved`.
* The five closed records are gone from the mount (`find
  ~/MediaLibrary/Comics/Manga/One Piece -name '*cNNNN*'` → nothing) and the reaper purged
  their pool copies (`state/reap_purges.log`/`Media-Syncer/torrent_reap.log` lines for
  flat and nested c1094 at 07:09/10:46/12:39/19:57); their library.db rows are
  `superseded`. The journal's last line for each is `completed` with
  `reconcile_closed: superseded` and no new re-queue after 2026-09-21T01:10:52Z.
* The 13 `unresolved` records are pre-existing partial completions (legacy collapsed
  plans, pool-only files whose keys moved before the inventory rewrite); each still holds
  at least one file, which is reconcile's "present" verdict, so they were deliberately
  left untouched.

**Deployed 2026-09-21 05:25 CDT** (`ship-fleet.sh`; it correctly skipped the reaper,
which is still mid-drain): the ingest daemon restarted onto the new code, 0 tracebacks in
`TorrentIngest.log`/`DirectIngest.log`/`MediaSync.log`, the mount re-primed (312 show
folders) and Jellyfin answered `/Items/Counts` (313 series / 20,386 episodes / 452 movies
/ 90 box sets). The old re-queue loop stopped at the restart — the last old-code re-queue
was Chapter 1133 at 04:53 (`TorrentIngest.log`), its re-download sits `present` on the
mount so the new audit leaves it, and when `chapter_volume_reconcile` purges it again the
completion will be CLOSED, not re-queued. `reconcile_dead` + `reconcile_closed` keep the
closed five out of every audit. Two of the tails from the old queue ended `failed`
(c1098/c1112 — the plan-time guards refused them); that is terminal and needs no action.

### Shipped 2026-09-19 — the plan-coverage contract, the collision park, the orphan sweep, a hidden reader

Four changes, all in `Torrent-Ingest`, each with a registered guard and (where a guard
rejects work) a journal replay.

**10.1 — a partial plan can no longer delete the rest.** `plan_coverage.py` enumerates a
release's media (torrent metadata first, disk walk for a wave or direct drop) and
classifies everything the plan does not name: release `.nfo`/`.txt`/`.sfv`, samples,
screenshots, creditless OP/ED/NCOP extras, subtitles beside a planned video and sub-50 MiB
videos are junk; **anything else is unresolved and parks the whole release** —
`_fail`/`_park_chunked_unfiled` with the `unfiled` list, every byte left on disk, the
`.torrent` under `failed/`. The check runs at identify time AND again in
`_advance_cleanup` immediately before the irreversible step; the chunked path can no
longer `_free` a file the plan did not account for. `test_plan_coverage.py` is check #51.

**10.2 — collisions park, never free; release identity is checked; re-arm is tested.**
`_collapse_existing_episode_collisions` now routes to `plan._collision_parked` (unresolved
by the coverage contract) instead of `_deduped_dropped` (accounted-for), so a same-slot
collision parks the release rather than freeing the colliding download — the exact seam
that lost DW (2005)'s 38 files / 60.9 GB. `validate_plan(..., release_name=)` refuses a
release whose own name states a year filed into another year's series, plus a
single-folder plan whose `year` contradicts the folder. `refile_season.py` gained
`--rearm-only --record HASH --rearm i,j` (no mapping needed) and the re-drop path is
pinned by Part 4 of `test_plan_coverage.py`: a plain re-drop carries `chunk_dropped` as a
deliberate verdict, and `_rearm_indices` makes exactly the named indices re-fetchable.

**10.6 — queued/ and ingesting/ have a way out.** `ingest.sweep_orphan_sources` runs
every cycle: terminal records' leftover sources file under `finished/`/`failed/`, iCloud
`" 2"` duplicates file under `finished/`, a live record whose recorded source is gone
ADOPTS the survivor, an untracked hash returns to the watch root. Registration no longer
files duplicate sources into `queued/`; `test_orphan_sources.py` is check #52. In the
production cycle after this shipped, both One Piece 1177/1178 sources were filed out of
`queued/` and it is empty.

**YacReader stays hidden and refreshes itself (owner decision, 2026-09-19).** The reader
"keeps popping up and taking over the whole screen": every comic filing bounced it, and
`open -g` stops focus stealing but not the window appearing. Two changes:
`yacreader_db.hide_app()` runs after every fleet-initiated start/activation (and
supervisor restart), using AppKit's `NSRunningApplication.hide()` through
AppleScriptObjC FIRST (no Accessibility grant needed) with System Events only a
fallback — armed at start, fired when the library update is underway OR after the 30 s
settle. And the **restart-bounce for filed comics is GONE**: a restart lands YacReader on
its library CHOOSER (it never re-opens a library by itself; quit/relaunch, `open -a`,
CLI args and `open` document events all leave it there — measured), so every filing used
to leave it not scanning AND interrupt the owner. Instead the app's own periodic update
is the refresh mechanism, enforced on at 30 minutes
(`UPDATE_LIBRARIES_PERIODICALLY_INTERVAL` is an enum index: 0=30 min). The supervisor
still starts it when down, stops it while the mount is unhealthy, bounces it when the
scan flags drift, and alerts (without restarting) when it is parked on the chooser — the
one human click (open Comics) is named in the alert. Guards: `test_yacreader_hide.py`
(check #53), `test_supervisor_yacreader.py`, `test_yacreader_scan_config.py`.

**And a windowless reader it cannot activate is bounced once.** Every deploy restarts
mediafs moments before the supervisor starts YacReader, and an app that comes up during
the re-prime can end with no library opened at all — `activate` and `open` are both
no-ops on that process (measured 2026-09-19: 0 windows, five consecutive deploy-time
alerts). After the existing two activation attempts and the alert, the supervisor now
stops and starts it exactly ONCE; a fresh process after the mount has settled re-opens
its library, and a second failure stands as the alert with the crash policy owning it.
Bounded by `yac_bounced_after_alert`, re-armed only when an update is seen.

**Replay results (printed by `test_plan_coverage.py`, in the commit message).** 177
whole-torrent historical plans still have their `.torrent` mirror. The coverage contract
would have parked 5 — the Smurfs (365 files, the incident), **Yamato 2202 (26 REAL
episodes the plan never accounted for and the old cleanup deleted — a previously unknown
Smurfs-class loss)**, The Office's 273 featurettes, Ted Lasso's 4, Made in Abyss' 1. The
identity guard's first draft rejected 22/177 and **all 22 were accepted work** (Lupin III
parts in the franchise folder, bracketed CRC32s read as years, multi-show packs' top-level
year); the shipped rule rejects 0 of history after narrowing. This is what replay is for.


### Shipped 2026-09-15 — the two queued tasks

**1. Doctor Who (1963) renumber + missing-part re-fetch (`e099421feeda`).** The 26-season
XVID pack names its parts by the release's *serial* number, so `_reject_same_episode` and
the collapse saw every part of a story claiming one slot; 17 downloaded parts were dropped
unfiled and the rest shifted. The repair **computed** each file's true broadcast number
from the release's own serial structure (accumulated parts per season — the exact numbers
TheTVDB/Jellyfin use: `S01E07` The Escape, `S01E18` Rider from Shang Tu, `S01E31`
Strangers in Space), and `refile_season.py --mapping` moved 34 files to their slots
(Marinus E21-26, Aztecs E27-29, Sensorites E31-36, Reign E37-42 with its Intro/Outro to
`S00E07`/`S00E08`, Planet of Giants/Dalek Invasion/The Rescue cross-season into S02E01-11),
deleted their stale sidecars, rewrote `remote_inventory.json`/`sync_state.json`, corrected
`chunk_filed`/`applied` in the journal, re-armed the 17 lost indices (5-11, 13-14, 18-24,
39), and fixed `library.db`. Verified: no old paths remain, `media_doctor` shows zero
placement faults.

Two lessons now in README, both hit live: Media-Syncer's in-memory inventory is written
back every ~30s and **resurrected the old keys** after the tool's rewrite (pause it with
`state/reap_ms_paused` + bootout first, and re-verify both files after); and chained moves
(`S01E07→S01E31` while `S01E31→S02E11`) need a two-phase transform, not sequential
pop-then-set.

**2. Individual manga chapters + volume coverage reconciliation.** Chapters are first-class
drops (they already filed as `cNNNN.cbz` with `chapter` rows — 298 journal plan files; the
gate keys chapters independently, now asserted in the test). The new work is the
deterministic coverage half:

* `scripts/manga_volume_map.py` + `state/manga_volume_map.json` — AniList identity,
  MangaDex aggregate (unfiltered: scanlations carry no volume tags), per-volume chapter
  **sets**, folder-chain identity, AI fallback once per series with a confidence marker,
  75-day TTL, fail-open and a one-day retry park.
* `scripts/chapter_volume_reconcile.py` + daemon `com.mikeyferguson.chapterreconcile`
  (6 h) — enumerates the shelf, applies `library.supersede_paths` (the same path
  `apply_plan` Phase 4 now calls) and `dbhook.record_purge`, gated by
  volume-present + authoritative-map + chapter-in-set + keep rule + no colored author.
  Keeps and reports everything else, logs to `decisions.log`.
* Ingest hooks after every verified plan (`ingest._advance_verify`, the chunked wave's
  verify, direct ingest) use the **cache only**; a miss queues a refresh.
* `scripts/audit_volume_chapter_coverage.py` is the read-only census; it shares the
  reconciler's decision function so its `leftovers` cannot disagree with an apply.
* `state/manga_chapter_policy.json` keep rules; Runbook write-up in OPERATING §5d
  ("a chapter vanished — why").
* Live census after the map build: 4 two-tier series, **0 leftovers**, 46 volumes (14
  mapped), 31 chapters — and one caught false positive: `Rurouni Kenshin - Restoration
  c0001.cbz` would have been purged as Restoration ch. 1, but its pages are the "To Rule
  Flame" one-shot; the series is now `keep_chapters` and the chain-identity fix that
  exposed it is pinned by the test.

**Still open, deliberately (unchanged):** `_collapse_existing_episode_collisions` still has
no content check. If a future run sees a same-slot drop with a different `episode_title`,
fail the plan rather than dropping the file (the new guard removes the trigger seen so
far, not the hazard).

**3. The serial-release guard (follow-up to the Doctor Who re-fetch, same day).** The
re-fetch waves misfiled **28 more files** by copying the release's serial numbers again:
the plan put The Daleks (1) at `S01E02`, and a truncated 6MB copy was filed there because
`_collapse_existing_episode_collisions` scans `MEDIA_ROOT` and the correct An Unearthly
Child part 2 was **evicted to the pool** — invisible to the collision check. The fix is
the handoff's own rule, "compute the answer": `identify.serial_release_map` computes the
broadcast numbers from the release's folder `Parts N-M` ranges (accrued per season),
`serial_numbering_block` states them in the prompt, and `library.validate_plan(serial_map=…)`
now **refuses** a plan that contradicts them. Both sides fail open for ordinary releases.
`test_serial_release_numbering.py` (registered, check #50) pins the arithmetic against the
independently confirmed slots and both guard directions. The 28 misfiles were cleaned:
18 real parts moved to their computed slots (chain-ordered, `.bak-remap-1` state backups),
10 misfiled copies superseded (partial 6MB copies, redundant copies of owned mkvs, 4
intros/outros occupying episode slots), and 8 indices re-armed for re-fetch
(5, 18, 39, 146-150). A path-level damage scan now reports **0 wrong placements** against
the computed map.

**Still open from that class (general, not serial-specific):** the episode-collision
collapse is blind to episodes that are **pool-only**; a future same-slot drop with a
different `episode_title` can still be filed alongside an evicted episode. The next fix is
to make the collision check consult the mount (or an owned inventory) and to fail the plan
on a title mismatch instead of dropping.

**Recovery note (2026-09-15 21:36-21:37, self-inflicted).** The serial-numbering commit
was authored while the daemon was already executing the working tree; an intermediate edit
referenced a helper by the wrong name for ~40 minutes, and every identify call in that
window died with `NameError: _serial_numbering_block`. Three packs lost files to the
per-file "giving up, freeing bytes UNFILED" path: Doctor Who (1963) 32 indices, Smallville
25, the new-Who pack 18 (it FAILED and its `.torrent` went to `failed/`). All three are
recoverable from their torrents and were repaired the same hour: the failed lists and
attempt counters were cleared so the active waves re-fetch and re-file, and the new-Who
`.torrent` was moved from `failed/` back to the watch root (it re-adopted at "160/178
already filed" and is fetching the rest). The lesson is the deploy rule's mirror image:
**the working tree is live the instant it is saved, so an edit is a deploy** — after any
change to `identify.py`/`library.py` under a running daemon, `verify_fleet.sh` before the
daemon next reaches that code path, not before the commit.

---

## 7. What is known-broken, accepted, and NOT on anyone's to-do list

This is the section that matters most, and the one a previous handoff got wrong by letting it
calcify. **Seven limits sat here for weeks reading like physics. Five were defects with a fix
one import away**, and were closed on 2026-09-12. What remains is genuinely hard:

* **Groq's day is 200,000 tokens** — about thirty agent turns. Not our code; it is the
  provider's free tier. Mitigated by the confirm-mode prompt and a 12-turn cap. One run that
  investigates instead of writing can still spend the whole day's budget, and one did.
* **A season boundary off by one arc resolves perfectly and is invisible.** Every episode
  gets a real title and a real plot; they are simply another arc's. Nothing in the file, the
  sidecar, or Jellyfin can see it. Only a census back to the source arc can —
  `scripts/audit_arc_placement.py`. **This is the failure mode to fear in this system.**
* **68 Season-0 sidecars are undecidable.** `scripts/audit_unlocked_specials.py` finds the
  unlocked population (71 of 456) and splits it: 3 are adjudicable against a journal plan
  (2 currently disagree — Vinland Saga), and 68 have no record anywhere of what they *should*
  say. Nothing outside the sidecar knows. The tool has **no `--apply`** and re-locks nothing,
  because re-locking a wrong title freezes the error permanently. Fix a real one by re-filing
  that special `owned`.
* **TVMaze and TMDB disagree about some seasons, and Jellyfin scrapes TMDB.**
  `scripts/audit_provider_disagreement.py` reports every divergence (35 named seasons across
  88 shows; 13 more have no pinned `<tmdbid>`). **It needs a human** — see §5 for why the
  automated version was deleted. The fix for a real one: file that season `owned`, so the
  fleet's own metadata is locked over the scrape.
* **`library_supervisor` cannot distinguish every pathological Jellyfin state.** It can now
  tell a *scan* from a hang (`/ScheduledTasks`, progress-gated, bounded by
  `SUPERVISOR_SCAN_GRACE_SEC`), which is what had it restarting Jellyfin 17 times. Other
  wedged states will still read as hangs.

**Before you inherit any limit on this list, re-test its premise.** One entry here used to say
a fix "needs an API key the fleet does not have" — the key was in `~/.config/api-keys/` the
whole time, beside a module that already spoke that API.

---

## 8. If you change something

1. Read `OPERATING.md` §3 (the damage rules) and §7 (deploying).
2. Make the change. Match the surrounding code — these files carry dense comments explaining
   *why*, and that is the house style, not decoration.
3. Add a test to `scripts/` and **register it in `scripts/verify_fleet.sh`** — tests are
   listed explicitly, never auto-discovered. An unregistered test does not exist.
4. If it rejects work, replay it over `state/journal.jsonl` first (§5).
5. `bash scripts/verify_fleet.sh` → `ALL CHECKS PASSED`.
6. Check `pgrep -f ai_runner.py` is empty, then ship.
7. After a full `ship-fleet.sh`: the mount unmounts and re-primes, and the supervisor stops
   and restarts Jellyfin. **This is normal and takes ~2 minutes.** Wait for
   `~/MediaLibrary/Shows` to repopulate and for `/Items/Counts` to answer before concluding
   anything is broken. Jellyfin may briefly lose series posters; `media_doctor` pushes the
   on-disk `folder.jpg` back on its next pass.

---

## 9. Useful tools you would not guess exist

All read-only unless noted.

```bash
scripts/audit_arc_placement.py      --show "<Show>" [--hash <ih>]  # the arc census: pass/fail
scripts/verify_arc_mapping.py       --show "<Show>"               # arcs beside provider titles
scripts/audit_provider_disagreement.py [--counts]                 # TVMaze vs TMDB, per season
scripts/audit_unlocked_specials.py  [--show "<Show>"]             # unlocked Season-0 sidecars
scripts/reconcile_library_db.py     [--apply] [--include-requested] # library.db; --apply WRITES
scripts/audit_volume_chapter_coverage.py [--series X] [--refresh]  # manga chapter/volume census
scripts/chapter_volume_reconcile.py  [--series X] [--apply]        # reconciler (dry by default)
scripts/manga_volume_map.py          --series X [--refresh] [--all] # cached volume->chapter map
scripts/refile_season.py             --mapping <json> [--record <ih>] [--rearm i,j] [--apply]
                                                                   # reviewed per-file episode refile
scripts/yacreader_rescan.py         [--files] [--apply]            # reader scan flags + unindexed shelf
scripts/repair_manga_mislabels.py    --series X [--all] [--apply]   # vNNNN whose contents are chapters
scripts/verify_owner_report.py      (read-only)                    # the owner's five failures, PASS/FAIL per line
comicfacts.py                       (module)                       # archive-content facts: colour, volume, chapters, ceiling
scripts/yacreader_index_repair.py   [--apply]                      # crash rows in the reader index; --apply WRITES
scripts/identify_capacity.py        --probe                       # which providers can serve
scripts/audit_free_only.py                                        # the billing invariant
scripts/repair_journal_paths.py     [--apply] [--record <ih>]     # records left on moved/purged paths; --apply WRITES
```

`identify_capacity.py --probe` re-probes every model id live. **Do this when model ids rot,
and they will** — five of the eight a careful reader would have written down on 2026-09-12
were already dead (HTTP 410 "end of life", 404 "no longer available"). `ai_models.py` heals
retired ids into `state/ai_model_overrides.json` at runtime; anything in that file is an id
`config.py` still names wrongly, so promote it when you see it.

---

## 10. Open work — the owner's five verified failures, then the free-AI upgrade

**Owner's standing instruction, and it outranks every repair instinct you are carrying:**
do not repair these faults by editing files. Each is a missing computed fact, a
missing guard, or a self-heal path that gave up. Upgrade the tool — prompt, harness,
validator, daemon — so that it detects the fault, repairs the live library, and cannot
repeat it; then run that tool. A hand-edited `.nfo`, a hand-typed `mv`, or a manual
`--apply` with no new test behind it means this session failed its brief **even if the
symptom disappears.** The owner is explicitly measuring this session by whether the same
faults can come back.

"Upgrade the tool" includes the **prompt**: `prompts/identify.md` is part of the tool.
Every computed fact the harness now has must be stated in the prompt as fact (the
`arcmap.py` pattern), and the prompt must stop asserting rules the code has already
changed (10.5d is the live example). The prompt is where the free model learns that a
franchise exists, that a series has a volume ceiling, and that a provider id is verified —
none of that may be left to the model's judgment.

The method is not negotiable: **compute the answer, do not ask the model for it** (§5);
replay every new rejection over `state/journal.jsonl` before it ships (§5, §8.4); register
every new test in `scripts/verify_fleet.sh` (§8.3); fail open on network errors; do not
weaken `validate_plan` to make a model's answer fit (§2.5). Match the house style — the
dense *why* comments are the codebase's memory, not decoration.

**Right now** (state table §6, measured 2026-09-20 05:30): no identify run is in flight,
the reaper is draining (PID 1311), and the Smurfs replacement pack has already failed once
on a truncated plan (§10.9). Do not re-drop it, and do not deploy, until the tool work
below ships.

Suggested order: **§10.0 is the contract** — the owner's five faults, each tied to the
subsection that fixes it. Do 10.3 (TZ) and 10.5a–10.5e (One Piece) first, because both are
live-repair-and-guard pairs; then 10.9 (the Smurfs, which is the proving ground for the
plan-assembly upgrade); then 10.10 (the systemic free-AI upgrade those pieces are all
instances of); then 10.4 (Toriko) and the 10.5e migration. Run every repaired tool over
the live library, verify against the owner-visible artifacts (§2.6), then re-run the
checks and paste the output.

### 10.0 The owner's five verified failures — the work order, with the evidence

The owner reported these on 2026-09-20. Each was re-verified this session at the path
named; each has survived at least one prior session that treated its guard as the fix.
They are ordered as the owner listed them. **Do not mark any of these done in a later
edit of this file without pasting the acceptance command and its output beside it.**

| # | fault (owner's words) | verified state 2026-09-20 | fix |
|---|---|---|---|
| 1 | repetitive One Piece chapters already covered by volumes are still in YacReader | `~/MediaLibrary/Comics/Manga/One Piece/` = **322 files**: 100 two-digit volumes (v00–v99), 111 three-digit (v001–v111), 7 four-digit mislabels, **104 chapters**. `ChapterReconcile.log` last run 2026-09-20 04:35: "One Piece: 104 chapter(s) kept, nothing to purge". The map (`state/manga_volume_map.json`) covers only v1, v2, v10, v11 (partial sets) | 10.5b, 10.5c |
| 2 | a non-colored volume does not get replaced by a colored one | the shelf holds both overlapping runs and **mixed colour inside each** (measured by archive contents: v01/v02/v50/v51/v91/v98/v99/v101/v102 are COLORED; v89/v90 are grey fan scans; the 1r0n v001–v111 are grey). `library.db` has **one** `colored=1` volume row (number 100). `chapter_volume_reconcile.plan_decisions` *has* a "colored supersedes grey" rule (#5) but `_kind` detects colour **from the filename only** (`dbhook._COLOR`), and these filenames carry none — so the rule can never fire | 10.5d |
| 3 | `One Piece v1176` should be `c1176` — there are not that many volumes | all seven are live on the shelf and in `library.db` as `volume` rows: `v1078`, `v1151`, `v1152`, `v1161`, `v1162`, `v1171`, `v1176`. Sampled `v1078.cbz` contents are `d1078` chapter pages (15 pages). No volume ceiling is persisted anywhere, so nothing can reject them | 10.5a, 10.5c |
| 4 | TZ (2019) still shows *Too Cute* as its show cover | `folder.jpg` md5 = `05520557851cb23ea38e121ed2713514` — byte-identical to the TMDB 80979 (*Too Cute*) poster. `tvshow.nfo` still carries `<originaltitle>萌宠成长记（精编版）</originaltitle>`, `<tvdbid>325542</tvdbid>`, `<premiered>2013-01-30</premiered>` (its `<tmdbid>` is now the correct 83135). `Season 01/season.nfo` year 2013; S02 episode `.nfo`s have **no `<season>`/`<episode>`**; the doctor worklist has no TZ entry at all | 10.3 |
| 5 | the Smurfs torrent is failing and it should not be | failed 2026-09-19 22:36 CDT: `plan accounts for 24 file(s) but leaves 385 release file(s) unfiled`. The 24-file plan is S01E01–10, S02E01–10 and the 4 Xtras; the identify log shows a 36-turn investigation, then two tiny writes. The 24 are also **wrong numbering** (release order ≠ broadcast order; *The Smurfette* is broadcast S01E31). The full 54.8 GB pack is retained (51 GiB by `du`); the `.torrent` is in `failed/` | 10.9 |

**Status 2026-09-23 (verified live).** Rows 1–4 are **done** and `verify_owner_report.py`
prints them PASS (`sidecar identity`, `no vNNNN mislabels`, `chapters covered by volumes`,
`one edition per volume`; `large releases` PENDING on the three parked chapter drops).
**Row 5 is DONE live.** The tooling shipped, the pack filed (409 -> 377 applied, 32
accounted duplicates), the content-verified refile ran 2026-09-20 (147 moves, 142 DB
rows superseded), and the 70 files it left missing were re-dropped through the fixed
reordered-pack skeleton on 2026-09-23: `70 file(s) verified in destination`, Season 01
39/39, 405 mp4 on the mount, `Nobody Smurf` at S07E27 and the Springtime Special at
S00E01 (see the shipped section for the full output). The evidence as measured:
  * TZ folder.jpg `965f20be…`, landscape.jpg `ec122588…`, premiered 2019-04-01,
    tvdbid 358915, enddate 2020-06-25; Jellyfin 2 seasons, 20 indexed episodes.
  * One Piece: 7/7 mislabels renamed; shelf 322 -> 197 (127 superseded), 0 `vNNNN`,
    112 volume numbers one edition each, 0 covered chapters.
  * Toriko: 69 blanks -> 0 (68 guide titles + 69 TMDB synopses), 8 junk titles -> 0.
  * Smurfs map: 362/405 titles matched, 347 differ, release `S01E01 (The Smurfette)` ->
    broadcast `S01E31`; 409-file skeleton written.

**The acceptance rule for all five (this is §2.6 in practice).** A repair is done when the
owner's artifact is checked and the output is pasted into the commit message:

* comics — the shelf listing and the YacReader-visible result (`scripts/yacreader_rescan.py
  --files`, `scripts/audit_volume_chapter_coverage.py`): zero `vNNNN`, zero chapters
  covered by an owned volume, exactly one copy per volume, colour correct;
* TZ — `md5 -q` of `folder.jpg`/`landscape.jpg` is neither Too Cute hash, `grep` of
  `tvshow.nfo`/`season.nfo` shows the correct title/year/ids, S02 episodes have
  `<season>`/`<episode>`, and `GET /Shows` via the Jellyfin API shows the corrected art and
  no ghost season;
* Smurfs — all 405 episode slots are broadcast-correct with the content on the mount:
  the main pack's record is `completed` and the 70 re-fetch files filed through the
  2026-09-23 skeleton path (`filed ... 70 file(s) verified in destination`, Season 01
  39/39, 405 mp4 on the shelf; see the shipped section).

### 10.1 P0 — a partial plan must never authorize deleting unfiled bytes (The Smurfs) — **SHIPPED 2026-09-19**

**Damage (verified).** `27dba0357753fe1c22b0bbad10e7c44d9fcf028f` ("The Smurfs Complete
Seasons 1-9 dvdrip") was a **405-file, 34,870,471,108-byte** release (S1=40 … S9=38). The
run that completed on 2026-09-15 returned a **40-file plan, every file in Season 1**; there
is no coverage requirement anywhere, so the 40 were applied and `_advance_cleanup` →
`_delete_local_content` (`ingest.py:2727`, `ingest.py:2870`) deleted the whole download
root. **365 files / ~31.16 GB are gone.** The record has no `chunk_*` keys because the
release was not chunked, so nothing recorded the loss at all. The surviving 40 S01 files on
the mount are also **positionally numbered and wrong** — `S01E01` is "The Smurfette", which
is aired S01E31, and `S01E40` is the "Springtime Special" that belongs in S00. This is the
Doctor Who (1963) renumber class again, unfixed. Evidence: the verified source torrent at
`state/torrent_sources/27dba…torrent` (sha1 matches, 405 files); the bad plan at
`state/tmp/27dba…_plan.json`; an Aug 12 plan in `state/journal.jsonl.bak-pack:108` that
mapped the same 405 files by title (this session measured that plan's provider ordering
as TVDB-ish, NOT the TMDB order Jellyfin actually renders — do not use it as the
authority; `tmdbguide.episode_names` is, see the session-2 section in §6).

**Build.** A plan-coverage contract computed by the harness, never asked of the model:

* enumerate the release's media files (torrent metadata for a torrent, disk walk for a
  direct drop);
* subtract `plan["files"]`; classify the remainder **deterministically** — release
  `.nfo`/SFV, samples, and non-media extensions are junk; anything else is *unresolved*;
* if any unresolved file remains, the plan **fails** and the whole release is parked
  (`.parked/`, journal `unfiled` list, status not completed). `_delete_local_content` runs
  only when unresolved == 0.

This is the same fail-open instinct as §2.5 and it is why the chunked "not in plan
(junk/duplicate); dropping" branch (`ingest.py:2492`) is **banned until 10.2 rewrites it**.
The model may never be the one who decides that a downloaded file is junk.

**Proof.** Fixture release of N media files with a 3-file plan → wave fails, bytes remain,
record not terminal. Then replay the classifier over the ~790 historical plans and print
the would-park count — some packs legitimately carry art/samples, so tune on
extension/size/sample patterns rather than on "file count differs", and show the replay
result in the commit message.

**Repair.** Done only after the guard ships: re-drop the owner's parked replacement pack
(`~/Downloads/The Smurfs (Complete cartoon series in MP4 format.).torrent`, `6c413306…`,
409 files) into the watch root and let it file. The original dvdrip mirror
(`state/torrent_sources/27dba…torrent`, 405 files) is the fallback. The new plan must be
allowed to replace the 40 wrong-slot S01 files — "skip a preexisting destination" must
yield to plan evidence for a wrong-slot file (the renumber precedent in §6). Do not
hand-renumber the 40.

### 10.2 P0 — a collision must never silently delete a downloaded file (Doctor Who (2005)) — **SHIPPED 2026-09-19**

**Damage (verified).** `74c608c7ba56dd4b3f2c04ab3999f045d767013f` ("Doctor Who Seasons 1 to
13 Mp4 1080p", 178 files, 281,369,570,819 bytes) was identified by the only surviving
provider as **Doctor Who (1963)**. Every early-2005 file collided with an existing (1963)
slot; `_collapse_existing_episode_collisions` (`library.py:1527`, `:1573`) removed those
entries from `plan["files"]`; `_advance_chunked` then treated every index absent from the
plan as junk and `_free()`d it (`ingest.py:2492`, `ingest.py:2396`). **38 files =
60,896,879,353 bytes (60.9 GB) deleted unfiled**, exactly indices
`[1–31, 41, 55–59, 92]`: S01E02–E13, S02E00–E13 (the whole 2005 Season 1 and 2), S03E00–E04,
S04E00, the four S04 specials, S01E01 "Rose", and S07E05 "The Angels Take Manhattan".
`chunk_failed` is empty — no genuine giving-up path fired; the record ended "completed".
Evidence: `state/tmp/74c…-w0_plan.json` (the 1963 destinations), the original in
`decisions.log:411672+`, and `~/Library/Logs/TorrentIngest.log.1:24359+`. Both the
`finished/` copy and `state/torrent_sources/74c…torrent` survive (sha1 matches).

**Build (three parts, in order):**

1. **A computed release-identity guard.** The harness knows the release title/year and the
   destination series title/year. Refuse a plan that files a 2005 release into a 1963
   series (normalize titles; compare years with the same tolerance the provider-season
   block uses). State it in the prompt as a fact, the way `serial_numbering_block` does,
   and enforce it in `validate_plan`. **Replay first** — reboots and yearless releases are
   the false-positive class.
2. **A collision must PARK, never free.** The absent-from-plan branch in `_advance_chunked`
   and `_collapse_existing_episode_collisions` must both move the file to a parked area (or
   leave it and record it `unfiled`) and the record must end non-terminal or
   completed-with-unfiled. The §6 open item ("the collapse is blind to pool-only episodes
   and has no content check") is the same seam: consult the mount/inventory, compare
   `episode_title`/size, and **fail the plan on disagreement** instead of dropping the
   file. This is also why 10.1's coverage contract and this part share a test suite.
3. **A tested re-arm path.** `refile_season.py:_rearm_indices` (`scripts/refile_season.py:235`)
   already clears indices from `chunk_done`/`chunk_dropped`/`chunk_failed_idx`. Add a test
   that a re-drop of the finished `.torrent` after re-arming refetches exactly the re-armed
   set — today `_carry_chunk_progress` (`ingest.py:224`) carries `chunk_dropped` as a
   deliberate verdict, so a plain re-drop is a **no-op that immediately reports completed**.

**Repair.** Re-arm `1–31, 41, 55–59, 92` for `74c608c7…`, re-drop the mirrored `.torrent`,
verify 178/178 with no drops — all through the fixed tool, not by editing the journal by
hand. 60.9 GB re-fetches; **no new download source is needed.**

### 10.3 P1 — provider IDs are verified before they can pick art (The Twilight Zone (2019)) — **SHIPPED 2026-09-20, repaired live**; **2026-09-29: the tvdb-id check gained the no-TMDB-mapping rule and the doctor its cross-series collision repair — see the top shipped section**

**Damage (verified).** Both TZ-2019 plans (`state/tmp/25d44d51…_plan.json`,
`state/tmp/a3a6e4ca…_plan.json`) carry `tmdb_id: 80979`, `tvdb_id: 325542`; the run log says
it "confirmed" them (`decisions.log:431988`). **TMDB 80979 is "萌宠成长记（精编版）", the
Chinese edit of *Too Cute*** (2013, Henry Strozier); TVDB 325542 is an unrelated Italian
1995 series; the correct TMDB for the Jordan Peele series is **83135**. `media_doctor` saw
the identity flip twice (`MediaDoctor.log:70655`, `:70667`) and re-ran `replaceAllImages`;
the second refresh fixed Jellyfin's remote art, but the **on-disk `folder.jpg` and
`landscape.jpg` are still Too Cute** (byte-identical to TMDB 80979 art; md5
`05520557851cb23ea38e121ed2713514` / `27082e830c7d93b7bd1defefbe9f884e`) and the local-media
provider outranks remote. The `_scan_episode_art` check (`scripts/media_doctor.py:781`)
only ever looks at episode stills, so series art reads healthy. `tvshow.nfo` and
`Season 01/season.nfo` carry Too Cute's `premiered 2013`, `originaltitle 萌宠成长记（精编版）`,
`tvdbid 325542`; Jellyfin's S1 shows "Season 1, 2013" and a **ghost "Season Unknown" row**.
The S02 episode `.nfo`s carry real titles but **no `<season>`/`<episode>`**, so Jellyfin
shows IndexNumber `null` and the series name for every S02 episode.

**Re-verified 2026-09-20 (owner's report — this is still live).** The shelf `folder.jpg`
still md5s `05520557851cb23ea38e121ed2713514` (the Too Cute poster). `landscape.jpg` now
md5s `ace480b21fbaae4b9679aa8a158c7050` — not the Too Cute file recorded above, but it has
**not** been verified against TZ identity either; the tooling must decide that, not a human
eye. `tvshow.nfo` now carries the correct `<tmdbid>83135</tmdbid>` but still
`<tvdbid>325542</tvdbid>`, `<originaltitle>萌宠成长记（精编版）</originaltitle>` and
`<premiered>2013-01-30</premiered>`; `Season 01/season.nfo` is still year 2013; every S02
episode `.nfo` still lacks `<season>`/`<episode>`; and `state/doctor_worklist.json`
(2026-09-20 05:06) contains **no TZ entry at all**, which is why this cannot self-heal:
`_scan_episode_art` only looks at episode stills. Repairing the files by hand would leave
the self-heal blind and the fault would return on the next Jellyfin re-scrape. The fix is
the doctor's title-level check, then run it.

**Build.**

* **ID verification is computed at plan time.** For every provider id the model supplies,
  fetch the provider record and compare normalized title + year against the plan's own
  title/year; a mismatch strips the id (and is logged into the rejection feedback), a
  network error fails open but **does not let that id pick art**. The fleet already speaks
  TMDB/TVDB for art, so this is an import, not a new integration.
* **media_doctor owns series-level art.** Extend the art check to title-level
  `folder.jpg`/`landscape.jpg`/`seasonNN-poster.jpg`, verify them against the *verified*
  identity, rewrite the files on disk, then refresh Jellyfin (`replaceAllImages`) so the
  local files win. A no-op on network failure, reported not silently passed.
* **The sidecar writer must always emit `<season>`/`<episode>`.** Find the writer that
  omitted them for S02 and fix it at the source; repair the ten files by re-running the
  (fixed) writer — not by hand.
* **Repair the contaminated nfos and the ghost season through the tool**, then confirm
  `/Shows/{id}/Seasons` has exactly two seasons with correct years and all 20 episodes have
  indexes.

**Proof.** Fixture with a swapped `folder.jpg` and a tvshow.nfo whose ids name a different
show → the doctor detects and replaces, and an offline run does nothing. Replay the ID
verification over the journal and count (do not enforce) how many historical plans carry
ids a live lookup contradicts.

**Acceptance (owner-visible, §2.6).** After the fixed doctor runs: `md5 -q` of
`folder.jpg`/`landscape.jpg` differs from both Too Cute hashes recorded above;
`grep -E 'originaltitle|tvdbid|premiered'` on `tvshow.nfo` and `Season 01/season.nfo` shows
the TZ (2019)/83135 identity and no 2013 date; every `Season 02/*.nfo` carries
`<season>2</season>` and a real `<episode>`; and `GET /Shows?searchTerm=Twilight` answers
two seasons with the corrected image. Paste that output in the commit message.

### 10.4 P1 — metadata self-heal must see pool-only episodes and survive a flaky provider (Toriko) — **SHIPPED 2026-09-20**

**Damage (verified).** All 147 videos are correct; 8 episode `.nfo`s carry the literal
release-group title `[Judas] x265 10b` (S01E133, E134, E135, E136, E138, E139, E142, E143)
and **69 have an empty `<plot>`**. Jellyfin mirrors both. `fixed_episodes_summary.json` in
the show folder ("Fixed 40 episodes…") is a lie — it records intent, not verified writes.

**Why it never self-heals (three independent reasons, all verified):**

1. `scripts/audit_metadata.py:178` walks `library.episode_is_blank` over
   `config.SHOWS_ROOT` (`~/Media`) — where Toriko's videos are **evicted** — so it finds 0
   episodes and `state/metadata_worklist.json` is empty. The mount/`remote_inventory` is
   the only complete view.
2. `repair_metadata._guide_index` requires **name and summary** from TVMaze
   (`scripts/repair_metadata.py:260-274`); TVMaze's Toriko has 146 names and **0
   summaries**, so it supplies nothing. The 69 synopses have no configured source at all.
3. `media_doctor.escalate()` (`scripts/media_doctor.py:2041`) counts any non-empty final
   message as success; two timed-out/empty AI runs burned `escalate_n`, and
   `MAX_ESCALATIONS_PER_SIG = 2` (`:95`, `:2574`) has now retired Toriko permanently.
   Free-provider caps (`state/ai_budget_capped/openrouter` 09-18, `cloudflare` 09-12) are
   also blocking the AI filler — a transient, not a reason to retire.

**Build.**

* Make the metadata audit enumerate from the **mount or the sidecars**, not the local
  video tree, so a pool-only show is visible.
* Accept **name-only** guide rows and add a synopsis source for rows the guide lacks
  (TMDB/TVDB overview, or the free-AI chain with the confidence/`ai_volumes`-style caching
  and fail-open semantics `manga_volume_map.py` already models). Add a source-priority note
  so a model summary never overwrites a provider one.
* `escalate()` must verify postconditions before it charges the budget: the intended
  `.nfo` fields must actually change (on disk and/or via API) or the run is retried without
  incrementing `escalate_n`; corrupted/AI-said-nothing runs must not count.
* When providers are capped, **park and retry**, never retire — the caps are the normal
  daily state (§4).
* Then repair: run the upgraded nightly path (`audit_metadata.py` → `repair_metadata.py`)
  plus the bounded AI filler for the 8 titles and 69 synopses. No hand-editing.

**Proof.** Synthetic pool-only show with a blank plot and a release-group title → audit
finds it, repair fills it, escalation counts only on a verified write. No re-download.

### 10.5 P0 — the manga tiers are computed, and the DB can hold both colors (One Piece) — **SHIPPED 2026-09-20**

**Verified state 2026-09-20 (re-measured; the 09-19 description flattened this).** The
shelf `Comics/Manga/One Piece/` holds **322 files**, exactly: **100 two-digit volumes
`v00–v99`**, **111 three-digit volumes `v001–v111`**, the **7 four-digit mislabels**
(`v1078`, `v1151`, `v1152`, `v1161`, `v1162`, `v1171`, `v1176`), and **104 `cNNNN`
chapters**. The two volume runs overlap on v01–v99 (~99 duplicate numbers), and the
overlap is **mixed colour**, which the old description flattened:

* 2-digit run, measured by archive contents: `v01`, `v02`, `v50`, `v51`, `v91`, `v98`,
  `v99` are COLORED (PZG / "Digital Colored Comics" / Colored Council / Gido); `v89`, `v90`
  are grey fan scans ("Davy Jones Edition"); `v00` is unclassified (one image).
* 3-digit run: the 1r0n volumes are grey VIZ (`v001→c0001` … `v111→c1123`), but `v101`
  and `v102` are the colored PZG files, and the archives' own chapter markers show the
  volume→chapter boundaries (`v099→c0995`, `v100→c1005`, `v110→c1121`).
* `library.db` series 1261 has **119 volume rows and only one `colored=1`** (number 100);
  series 1262 `One Piece Colored` still exists and still owns v100, v106 and c0424 as
  `owned` even though those files were unlinked through the mount on 2026-09-04 and
  2026-09-13 (`~/Library/Logs/MediaFS.err:100659-100661`, `:111149-111151`) and reaped
  (`state/reap_purges.log:28256-28258`).

The 1r0n pack applied all 154 files (111 volumes + 43 chapters) — **no re-download is
needed for coverage** — but 37 of its chapter copies were skipped in favour of older/larger
scans and v101–v105 were skipped in favour of the colored files.

**The single most useful fact for all of 10.5:** the owned archives themselves carry the
chapter numbering (`One Piece - c0001 (v001) - …`, `One Piece - Digital Colored Comics -
c0471 (v049)` …), and their first entries name the edition. A volume→chapter map for One
Piece is therefore **computable offline from the shelf**, and colour is detectable from the
archive contents — neither needs a model or MangaDex. Build the map that way first
(10.5b), providers second, the AI only for what remains.

**10.5a — a computed volume ceiling decides volume vs chapter.** Today the only rule is
prose in `prompts/identify.md:124-130` ("large ⇒ volume"), and the model filed `c1077`
correctly then treated every later bare number as "the next volume" because the library
digest (`library.py:504-515`) showed `v1078` as a volume. `manga_volume_map.anilist_search`
already reads AniList's total `volumes`/`chapters` (`scripts/manga_volume_map.py:220`) but
`refresh()` never persists them. Persist the ceiling in
`state/manga_volume_map.json`, state it in the prompt as fact ("this series has N
volumes"), and enforce it: a bare number above the ceiling is a **chapter**; a `v` marker
above the ceiling is a mislabel the validator refuses. Replay over the journal to prove
the guard does not reject legitimate high-numbered volumes for series whose AniList count
is stale (fail open when the ceiling is unknown).

**Measured 2026-09-20:** all seven mislabels are still on the shelf and in `library.db` as
`volume` rows, and `state/manga_volume_map.json` persists no total for any series. The
ceiling is also derivable offline from the shelf: the highest real volume archive's chapter
marker (`v111→c1123`) plus the number of single chapters after it. Use AniList's total when
fresh, the shelf when it is not, and fail open when neither exists — never invent one. This
is the guard that makes the owner's "there are not that many One Piece volumes" a computed
fact instead of a judgment call.

**10.5b — the volume→chapter map must actually cover the volumes.** The One Piece map entry
is `source=mangadex, confidence=0.0` with only `v1, v2, v10, v11` mapped — 4 of 119 owned
volumes. MangaDex's One Piece aggregate carries almost no integer volume tags, so
`plan_decisions` keeps every chapter ("volume map unknown") by design and the census says
`covered=0, uncovered=103`. A working map for 1–111 is the gate for any chapter purge. Add
a second provider/source (AniList volume/chapter ranges, an official listing, or the
existing one-shot AI completion path) with per-volume chapter **sets**, the same
confidence/TTL/fail-open contract, and make the refresh able to reach 111. Do not hand-edit
the JSON — the tool must fetch it.

**Measured 2026-09-20:** the map still holds only v1, v2, v10, v11 (partial sets;
`source=mangadex`, `confidence=0.0`), so `ChapterReconcile` keeps all 104 chapters every
6 hours ("nothing to purge"). The build order is now fixed: **(1) compute from the owned
archives' embedded chapter markers** — offline, deterministic, covers all 111 volumes, and
it is the §5 "compute the answer" step; (2) fill gaps from AniList volume/chapter ranges or
an official listing; (3) only what remains may use the existing AI fallback with its
confidence marker. Record in the entry which volumes were shelf-derived so a better scan
landing later can invalidate exactly those, and keep the rule that a colored chapter may
only be superseded by a colored volume (10.5d) — the reconcile gate already carries a
colored-author keep, so do not drop it.

**10.5c — DONE live 2026-09-20 (session 2, see the session-2 shipped section).** Covered
chapters purged, the repair now keeps the series in renamed files, bare markers refused
at plan time and collapsed as duplicates by the reconciler, and the mismatch class is
guarded by the finished-series chapter ceiling. The text below is the original brief.

**10.5c — chapters yield to volumes, and mislabels are repaired by tool.** Once 10.5a/b
land: the seven `vNNNN` files are renamed to `cNNNN` **by a repair tool that uses the same
computed ceiling** and goes through `library.supersede_paths`-style machinery (mount
unlink + reaper queue + `dbhook.record_purge`) rather than `mv`; then
`chapter_volume_reconcile.py` (6-hourly daemon and `--apply`) can supersede
`c1077–c1133`-style chapters covered by `v107–v111`. Add a fixture where a chapter above
the volume ceiling sits alongside a mapped volume and assert the reconcile now purges it.

**Measured 2026-09-20:** the reconcile runs every 6 hours and says "One Piece: 104
chapter(s) kept, nothing to purge"; YacReader still lists them. The acceptance is the shelf
census (`audit_volume_chapter_coverage.py`) **and** the reader's own index via
`yacreader_rescan.py` — not the daemon's log line.

**10.5d — colour is a file property, the colored copy replaces the grey one, and a grey
file may never eat a colored one.** This is the fault the owner actually suspects, and the
2026-09-20 re-verification found the concrete mechanism. `librarybrain/librarydb.py` keys
media on `(mtype, None, number)` — colour and path excluded — and `upsert_media` does
`colored = MAX(colored, ?)` (`:468`). So the colored `v01` and the grey `v001` collapse to
one row, a stale `colored=1` can never clear, and the two tiers cannot coexist in the DB
(measured: 119 volume rows, exactly one `colored=1`). Worse, the prompt still teaches the
**old** layout: `prompts/identify.md:113-122` says a colored volume lives in
`<Series> Colored/`, while the owner rule of 2026-09-05 (`library.py:102-123`, "a folder is
named for the SERIES, never for the edition") forbids that and makes colour a file
property; `README.md:1268-1280` teaches the old layout too.

The reconcile daemon already contains the right rule — `plan_decisions` rule 5, "a colored
volume supersedes a same-numbered grey volume" — but **it can never fire on One Piece**
because `chapter_volume_reconcile._kind` reads colour from the **filename**
(`dbhook._COLOR = colored|full[- ]?color|colour`), and these filenames carry no colour
marker. The shelf's own archives do: the first entries name the edition
(`One Piece v002 (Colored) (Digital) (PZG)`, `One Piece - Digital Colored Comics - c0471
(v049)`, `One Piece - c0001 (v001) - … [VIZ Media] [Digital] [1r0n]`). That is the fix:
detect colour where the fact lives.

**Owner direction, 2026-09-20 (it overrides the older "colored is a separate series" design
in every direction):** when a colored and a non-colored copy of the same volume coexist,
**the colored copy is the one kept and the non-colored file is superseded**; a grey file may
never supersede a colored one; and no future plan may re-file a colored volume into a
`<Series> Colored/` sibling (the 2026-09-05 folder rule stands).

Fix all six parts: (i) give media a colour-aware identity (or a path/file key) and stop
`MAX(colored)`; (ii) make the supersede path refuse any plan that would delete a colored
file in favour of a grey same-numbered one, trace which tool queued the 09-13 unlinks
(candidate: the purge batch that wrote
`library.db.bak-purge-batch2-20260913-110358`; MediaFS logs at `:111149`), and pin that tool
with a guard and a regression test; (iii) detect colour from the **archive contents**
(first entries / ComicInfo) and use it in `_kind`, reconcile rule 5 and the DB — a
filename-based detector is the bug; (iv) rewrite the prompt and README so the free AI is
told the current rule, not the deleted one; (v) reconcile the stale `One Piece Colored` DB
rows so the title can be re-acquired cleanly; (vi) run the fixed reconcile and verify the
shelf keeps exactly the colored copy per volume (owner-visible acceptance in §10.0). If the
owner wants the reaped colored `v100/v106` back, that is a re-acquire (10.7), not a
fabrication.

**10.5e — DONE live 2026-09-20 (session 2).** `migrate_comics.sh --apply` moved 191
files into `Manga/One Piece/One Piece/` and `Manga/One Piece/Ace's Story/` (verified
191/191); the table row was already the generator's evidence-based one, and
`resolve_comic_folder` resolves both the master and the member's canonical name into the
nested layout. The text below is the original brief.

**10.5e — franchise grouping is computed: One Piece + Ace's Story in one folder (owner
request, 2026-09-19).** `config.COMIC_FRANCHISES` has **no One Piece row**;
`library.comic_franchise` therefore let `One Piece - Ace's Story` sit as its own top-level
series. `scripts/build_comic_franchises.py --no-net` already prints the exact row to add:

```json
{
    "name": "One Piece",
    "kind": "manga",
    "members": {
        "one piece": "One Piece",
        "one piece aces story": "Ace's Story"
    }
}
```

Add it (prefer making the generator's output the source of truth so the table cannot drift;
curated rows exist for western franchises where the prefix heuristic cannot work — this is
not one of those). The table is injected into the identify prompt's digest already, so once
the row exists the free AI will know the franchise without being asked; state it as a
**computed fact** in the prompt text too, so a plan that files `Ace's Story` outside the
master folder is rejected rather than debated. Follow the established convention exactly
(`Dragon Ball/` holds `Dragon Ball/`, `Super/`, `…`), which means the end state is
`Comics/Manga/One Piece/One Piece/…` and `Comics/Manga/One Piece/Ace's Story/…`. If the
owner meant the main run flat in `Comics/Manga/One Piece/` with only `Ace's Story/`
nested, that is a franchise-code change and the migration must not start until it is
decided; record the decision in the migration notes. Note `resolve_comic_folder(colored=True)`
bypasses franchises (`library.py:123`) — fix that too, or future colored One Piece volumes
land in a sibling folder and recreate the split. Decide whether
`Wanted! Eiichiro Oda Before One Piece` belongs to the same master (it is Oda one-shots,
not the main continuity) and record why either way.

**Proof for 10.5e.** A test pins the One Piece franchise row (the
`test_duplicate_series_keys.py` / `build_comic_franchises` class). Migration follows
`OPERATING.md`'s pause-and-rewrite contract (`scripts/migrate_comic_franchises.py`, inventory
rewritten, mediafs paused), then `comic_shelf_audit.py` is clean.

### 10.6 P1 — a duplicate drop must not sit in `queued/` forever (One Piece 1177/1178) — **SHIPPED 2026-09-19**

**Root cause (verified).** The two files in `queued/` are **untracked duplicate copies** of
hashes already completed and owned: `find_drop_files()` only scans the watch root's top
level (`ingest.py:145-165`), never the `queued/` contents; at registration a second iCloud
copy ("` 2`") was parked as a pseudo-record with no journal entry (`ingest.py:516-518`);
admission iterates journal records (`:683-733`) and terminal filing moves only
`record["torrent_path"]` (`:2934-2941`), so the extras were never moved or cleaned.
`reconcile.py` only re-queues completed records whose content is *absent*, and `janitor.py`
never touches `queued/`. qBittorrent currently holds zero torrents; the content is on the
mount and in `remote_inventory.json` (c1177, c1178), so the files are safe to remove.

**Build.** (a) At registration, a second copy of a drop is deduped or tracked on the
record; (b) an **orphan sweep** in the cycle walks `queued/` and `ingesting/`, hashes each
`.torrent`/`.magnet`, looks up the journal, and files terminal duplicates under
`finished/` (or deletes them with a logged reason); (c) a regression test for the iCloud
`" 2"` duplicate path — there is none today. **Owner action now:** delete or move the two
`queued/` files; do **not** drop them at the top level, which deliberately re-queues a
fresh download.

### 10.7 Owner actions — the re-download list (verified, so do not guess)

| torrent | verdict |
|---|---|
| **The Smurfs Complete Seasons 1-9** | **No new download; the bytes are already here and parked.** The replacement MP4 pack (`6c413306…`, 409 files, 54.8 GB) was dropped 2026-09-19 and **failed on the plan-coverage guard the same night** (24-file plan of 409, §10.9) — nothing was deleted, the full 54.8 GB pack (51 GiB by `du`) sits in `~/Downloads/.torrent-ingest/` and the `.torrent` is in `Torrents/failed/`. Fix the plan-assembly tool (10.9), then move the `.torrent` from `failed/` to the watch root's top level. The original dvdrip mirror (`Torrent-Ingest/state/torrent_sources/27dba…torrent`, 405 files) stays as the fallback. The existing 40 wrong-slot S01 files must be superseded by the correct plan. |
| **Doctor Who Seasons 1 to 13** (2005, `74c608c7…`) | **Done — no re-acquire.** Re-armed and re-dropped 2026-09-19; the journal's final line is `completed` with `chunk_done=178`, `chunk_dropped=0`, `chunk_failed=0` (verified 2026-09-20). The 38 re-fetched files are on the mount. |
| **One Piece (Digital) (1r0n)** (`12873efd…`) | **No re-download for coverage** — all 154 files applied. The work is rename/reconcile, not fetch. |
| **One Piece Colored v100/v106/c0424** | **Gone from the pool** (reaped 2026-09-04/09-13, see 10.5d). Only if the owner wants the colored run restored does anything need re-acquiring; the fleet has no source. |
| **One Piece 1177/1178 in `queued/`** | **No download** — already filed and owned (10.6); safe to delete or move to `finished/`. |
| **TZ (2019), Toriko, everything else** | **No download.** Metadata/art/DB repairs only. |

### 10.7b Parked re-drops — where they are and where they go (recorded 2026-09-19)

The owner parked both re-downloads **by hand, outside the pipeline**, so neither could be
ingested before its prerequisite guard shipped. Both have since been dropped: DW is
`completed` (10.7), and the Smurfs failed on plan assembly and now sits in
`Torrents/failed/`. `~/Downloads/` is no longer a parking spot — the only re-drop left is
the Smurfs, **after 10.9 ships** (below). `~/Downloads/` is not watched by the pipeline
(`DirectIngest/` is the only watched folder under Downloads).

| release | parked at | move where, when |
|---|---|---|
| Doctor Who (2005), "Doctor Who Seasons 1 to 13 Mp4 1080p" (info hash `74c608c7…`, 178 files, 281.4 GB) | **DONE.** Re-armed 2026-09-19 (`refile_season.py --record 74c608c7ba56dd4b3f2c04ab3999f045d767013f --rearm 1-31,41,55-59,92`), re-dropped to the watch root's **top level**, final journal line `completed` with `chunk_done=178`, `chunk_dropped=0` (verified 2026-09-20). The pipeline mirror stays at `Torrent-Ingest/state/torrent_sources/74c608c7….torrent`. | Nothing left to move. |
| The Smurfs Complete Seasons 1-9 — **the owner's replacement pack**, not the original dvdrip (info hash `6c413306e7053dbb8f1dabf7dcc845f509ec3027`, 409 files, 54,806,198,752 bytes = 54.8 GB) | **`Torrents/failed/The Smurfs (Complete cartoon series in MP4 format.).torrent`** since the 2026-09-19 22:36 failure; content retained at `~/Downloads/.torrent-ingest/The Smurfs (Complete cartoon series in MP4 format.)/` (51 GiB on disk by `du`; 54,806,198,752 bytes by the torrent). Contents: 409 `.mp4` — S1=40, S2=35, S3=51, S4=48, S5=41, S6=63, S7=65, S8=24, S9=38 (405 episodes, counts identical to the original release) plus 4 Xtras: `The Smurfs - The Lost Village (movie).mp4`, `… A Christmas Carol (special).mp4`, `… The Legend of Smurfy Hallow (special).mp4`, `… The Smurfs and the Magic Flute (movie).mp4`. The original dvdrip mirror (`27dba035…`, 405 `.mkv`, 34.9 GB, `Torrent-Ingest/state/torrent_sources/27dba035….torrent`) stays as the fallback. | **After 10.9 ships:** move the `.torrent` from `failed/` to the **top level** of `iCloud Drive/Torrents/` (never `queued/`). It must produce a 409/409 plan (405 episodes + 4 Xtras) and supersede the 40 wrong-slot S01 files. |

One check before the Smurfs re-drop, because a truncated `.torrent` is a silent no-op:
verify the info hash parses to the expected value (bencode + sha1, the `_ensure_source`
convention) and count the `files` list — **409 for this pack** (not 405; the old mirror is
the 405-file one). After it files, confirm the record's last journal line is terminal and
the content is on the mount. One naming caveat, unchanged and now load-bearing: this pack's
filenames carry the release's own `SxxExx` numbering (e.g. `S01E01 (The Smurfette)`), which
is the same scheme as the deleted dvdrip, so the compute-the-numbering rules of §5 and
§10.9 apply before any file is trusted over the provider.

### 10.8 Acceptance for this batch

**Progress 2026-09-19:** 10.1, 10.2 and 10.6 shipped with registered tests #51/#52; the
replay surfaced **Yamato 2202** (26 real episodes a partial plan never accounted for,
nobody had noticed). DW (2005) was re-armed and re-dropped; the outcome is in the journal.

**Progress 2026-09-20:** the tooling for all five is shipped with 60 registered checks.
§10.0's status block says exactly which live artifacts are verified and which await the
reaper drain / the Smurfs re-drop; do not claim more than it says.

1. Every new guard has a registered test in `scripts/verify_fleet.sh` and a journal replay
   result in its commit message; `ALL CHECKS PASSED`.
2. The five owner-visible faults in §10.0 are fixed **and each acceptance command from
   §10.0 is run and pasted, with its output, into the commit message.** Specifically:
   * Smurfs' 409-file pack fully planned (405 episodes + 4 Xtras) and filed, superseding
     the 40 wrong-slot S01 files, record terminal, content on the mount;
   * TZ (2019) has two seasons, correct art on disk (**md5 differs from both Too Cute
     hashes**), a clean `tvshow.nfo`/`season.nfo`, no ghost season, S02 episodes numbered;
   * One Piece has zero `vNNNN`, zero chapters covered by an owned volume, exactly one
     copy per volume with the coloured copy winning, a colour-correct DB, and the One
     Piece franchise folder holding both series; the YacReader shelf reflects it;
   * Toriko has 0 release-group titles and 0 blank plots (or a bounded, logged queue for
     the rest); `queued/` is empty.
3. `verify_fleet.sh`, `fleet_health`, `fleet_doctor`, `library_health.txt` and
   `media_doctor` (with the Jellyfin credentials from §4) all read clean or name only
   known-accepted items from §7.
4. The next session that reads this file can tell from `state/decisions.log` and the test
   names exactly which tool prevented which fault — that trace is the deliverable. A fix
   with no such trace and no owner-visible before/after is not done (§2.6).

### 10.9 P0 — the Smurfs: a plan the harness computes, and a plan the model can finish writing — **SHIPPED 2026-09-20; the reordered-pack floor fixed 2026-09-23; re-drop pending**

**Failure (verified 2026-09-20).** `6c413306…` (409 files, 54.8 GB) was dropped
2026-09-19 09:11 and failed the same night. `state/tmp/6c413306…_plan.json` is 8,388 bytes
and contains **24 files**: S01E01–E10, S02E01–E10 and the four Xtras. The journal's
terminal line (2026-09-19 22:36 CDT; `updated_at 2026-09-20T03:36:02Z`) is `failed` with
`plan accounts for 24 file(s) but leaves 385 release file(s) unfiled … the release is
parked intact` — §10.1's guard did its job. The identify log ends: turn 36 "finished
WITHOUT writing … asking for it (1/2)", turns 38/39 two tiny `Write`s, turn 40
"done (stop)". All bytes are still on disk; the `.torrent` is in `Torrents/failed/`.

**Two independent defects; fix both.**

1. **Nothing computes the mapping this pack needs.** The filenames carry SxxExx in the
   *release's* order, which is not broadcast order — the dvdrip mirror's own history proves
   it: release S01E01 "The Smurfette" is broadcast S01E31 (§10.1). The 24-file plan maps
   release S01E01 → broadcast S01E01, so even a complete plan written this way would file
   S01 wrong. This is the `serial_release_map`/`arcmap` class: the harness must compute
   release→broadcast episode numbers from the provider's episode list (match the release
   file's own episode title against the guide), state the block in the prompt as fact, and
   have `validate_plan` refuse a plan that contradicts it. Fail open when the guide is
   unavailable.
2. **A single `Write` cannot hold 409 entries.** The model spent 36 turns investigating,
   then wrote a truncated prefix; the harness accepted it because the JSON was valid, and
   only the POST-run coverage check saw the missing 385. Plan assembly must scale:
   * the prompt must say the plan may be written in parts (append via `Edit`, or a
     `files_part` schema), and `identify`/`ai_client` must merge parts, detect a
     truncated/partial plan **before the run ends**, and ask for the missing slice with the
     explicit unfiled list instead of finishing;
   * above a size floor (~150 files) the harness must hand the model a deterministic
     skeleton — every release file enumerated with its computed destination (or an
     explicit `needs_mapping` marker) — so it fills titles/ids/gaps instead of re-typing the
     listing;
   * a cut plan is a **retryable state, not a terminal `failed`**: bounded retries with the
     missing slice, then park with the reason. The coverage guard stays the last line of
     defense (§10.1), it is not the first.

**Repair.** After the tool ships: move the `.torrent` from `failed/` to the watch root's
top level. The full 409-file plan (405 episodes + 4 Xtras) files the pack; the 40
wrong-slot S01 files from the old dvdrip are superseded by plan evidence (the renumber
precedent in §6/§10.2); end state: every episode slot occupied with broadcast-correct
numbers, the 4 Xtras placed (S00/Movies), the record terminal.

**Proof.** Fixture release whose filenames are deliberately not in broadcast order → the
computed mapping block is in the prompt, the plan is rejected when it contradicts the block
and accepted when it follows it. Fixture release of >300 files → the skeleton is complete,
the model writes it in parts, coverage is 100% before the run ends. Replay both over
`state/journal.jsonl`; register the tests in `verify_fleet.sh`.

### 10.10 P0 — the free-AI upgrade: computed facts, completable plans, verified self-heal — **IMPLEMENTED 2026-09-20**

The five failures in §10.0 are one shape repeated: **the harness let the model do
arithmetic/enumeration it cannot do, and the self-heal that should have caught the result
was blind.** This section is the systemic fix the owner asked for and the umbrella over
10.3, 10.5 and 10.9. Do not treat the pieces as separate nice-to-haves; each is the
difference between a fault returning and not.

**A. Compute before the model runs, and state it as fact.** Every block follows the
`arcmap`/`serial_numbering_block` pattern — computed by the harness, injected into
`_runtime_prompt`, enforced by `validate_plan`, fail-open:

* manga volume ceiling (AniList total, shelf fallback) and the `vNNNN`-above-ceiling
  rejection (10.5a);
* volume→chapter sets from the owned archives, providers second, AI last (10.5b);
* colour per file from archive contents, and colored-supersedes-grey in both the reconcile
  and the plan validator (10.5d);
* franchise membership from `config.COMIC_FRANCHISES` (10.5e);
* release→broadcast numbering for season packs (10.9);
* provider ids verified (TMDB/TVDB title+year fetch) before any id may pick art; a mismatch
  strips the id and logs the rejection; a network error fails open but the unverified id
  cannot pick art (10.3).

**B. Plans the model can actually finish.** Enumerate the release in the harness (it does
this for coverage already); require the plan to cover it; support part-written plans with
merge + truncation detection; above a size floor hand the model a deterministic skeleton;
reject with the explicit missing list; bound retries; park — never terminal-fail — when
only assembly failed.

**C. The prompt tells the truth.** `prompts/identify.md` must lose the stale rules (the
`<Series> Colored/` layout at :113-122 is the live example) and gain every computed block.
Anything a human has had to say twice belongs in the prompt or the validator.

**D. Validation with evidence.** Every new rejection is replayed over
`state/journal.jsonl`, with the false-positive count printed in the commit message (§5);
tests are registered in `scripts/verify_fleet.sh`. No guard ships on a hunch.

**E. Self-heal that verifies itself.** `media_doctor` must see series/title art and nfo
identity (10.3); `audit_metadata` must enumerate pool-only media from the mount/inventory,
accept name-only guide rows and have a synopsis fallback (10.4); `escalate()` must verify
the postcondition (fields actually changed) before charging its budget; provider caps
park, never retire; `chapter_volume_reconcile` purges covered chapters once the map exists
and logs before/after; every repair writes a `decisions.log` entry with its evidence.

**F. The verification gate for "fixed".** Add `scripts/verify_owner_report.py` (name it
what it is): it computes the five §10.0 checks — Too Cute md5s absent, no `vNNNN` on the
shelf, zero covered One Piece chapters, colour-correct volume pairs, Smurfs plan coverage
once a plan exists — and prints PASS/FAIL per line. Read-only; registered in
`verify_fleet.sh` as a report check (it must not gate a deploy on the parked Smurfs). Run
it at the end of the session and paste the output into the commit message. Then add the
regression tests that fail if any of the five returns.

**G. Definition of done for this batch.** §10.8 item 2. The measure is not that the symptom
is gone today; it is that the tool that computes the fact, the guard that enforces it and
the self-heal that repairs it all exist and are tested, and that the owner-visible artifact
is clean.

---

## 11. The repository is PUBLIC — the 2026-09-19 secrets extraction

The GitHub repo was made public on 2026-09-19 and lives at
**https://github.com/Pirate-Hunter-Zoro/Media-Orchestrator** — renamed from `Media-Fleet`
the same day, and the local directory renamed to match
(`~/Developer/Media-Orchestrator`). It used to be private and tracked two credential
stores; both are now machine-local, untracked, and gone from history:

| what | where it lives now | tracked template |
|---|---|---|
| MEGA account pool (`user`/`pass`, ~822 remotes) | `Media-Syncer/rclone.conf` (root `.gitignore`) | `Media-Syncer/rclone.conf.example` |
| machine paths + `JELLYFIN_API_KEY` + provisioner email | `.env` (root `.gitignore`) | `.env.example` |

**What changed, and what a future session must not undo:**

* `fleet_env.py` (repo root) loads `.env` with `os.environ` taking precedence. The four
  configs (`Torrent-Ingest/config.py`, `Media-Syncer/scripts/config.py`,
  `Title-Scout/config.py`, `YouTube-Downloader/ytconfig.py`) read machine-specific roots
  through it with generic `Path.home()` defaults; the absolute paths that used to be
  hardcoded in code are gone (audit tools derive from `config`, plists keep the username
  in their absolute paths and that is accepted).
* The nine Torrent-Ingest plists that need a Jellyfin key carry `__JELLYFIN_API_KEY__`;
  `Torrent-Ingest/startup.sh` substitutes the real value at install time from `.env`,
  `~/.config/api-keys/jellyfin_key`, or the environment. The installed agents in
  `~/Library/LaunchAgents` still hold the real key; re-running startup.sh reproduces them.
* `Media-Syncer/scripts/mega_accounts.py` **no longer commits or pushes** the pool conf —
  the provisioner appends to the untracked file and publishes it atomically to
  `~/.config/rclone/rclone.conf`. Do not restore the commit path.
* Guard: `scripts/test_no_tracked_secrets.py` (verify check #54) scans `git ls-files`
  for secret stores and credential-shaped values; `.githooks/pre-commit` unstages
  `rclone.conf`/`.env` and rewrites a live Jellyfin key to the placeholder.
* History was rewritten with `git filter-branch` (rclone.conf removed from every commit;
  the Jellyfin key string replaced with the placeholder). A force-push alone was NOT
  enough and was measured not to be: after the rewrite the old commits were still
  fetchable by SHA (`raw=200`/`api=200`), and this file itself named old commits. So the
  GitHub repository was deleted, a clean one created, and the project renamed to
  `Media-Orchestrator` (GitHub would not release the old name immediately). The old git
  objects are gone: `git fetch origin <old-sha>` answers `upload-pack: not our ref` and the
  old SHAs 404 under the new name. One residue is outside the owner's control: the OLD-name
  raw URL (`raw.githubusercontent.com/.../Media-Fleet/<old-sha>/...`) kept answering 200
  from Fastly's cache (`x-cache: HIT`, `max-age=300`) while the same SHA under the NEW name
  404s — a CDN entry only GitHub Support or time can clear (it was observed to expire after
  the TTL once the old name was vacated). The pre-rewrite bundle
  (`~/Developer/Media-Fleet-backups/Media-Fleet-prepublic-20260919-151123.bundle`) was
  deleted after this migration was verified — it contained the old credentials by
  definition, hence the rotation note below.
* **The local directory was renamed the same day** (`~/Developer/Media-Fleet` →
  `~/Developer/Media-Orchestrator`), and every absolute-path reference in the tracked tree
  was rewritten: launchd plists, `config.py`/`ytconfig.py` fallbacks, audit tools,
  `ship-fleet.sh`, `verify_fleet.sh`, the `.env.example` template and the docs. The
  `.env` itself was repointed too. Because the move happened while an identify run and the
  reaper drain were live (§2.2/§2.4 forbid bouncing either), a **temporary compatibility
  symlink `~/Developer/Media-Fleet -> Media-Orchestrator`** was left in place so the
  in-flight processes' already-loaded absolute paths keep resolving; the installed launch
  agents were refreshed from the renamed tree (with the Jellyfin key substituted) but
  deliberately NOT reloaded, so nothing running was disturbed. **Remove the symlink only
  after a reboot has every daemon running from the new path** (`ship-fleet.sh` cannot do
  it: `kickstart -k` restarts the LOADED definition, which still names the old path). The
  owner scheduled that reboot; **§12 is the closing checklist** — until it runs, the
  symlink is load-bearing. `~/Developer/.megaignore` was repointed to
  `Media-Orchestrator/...` AND excludes the old name, so MEGA neither syncs the alias nor
  re-engages the old churn paths.
* **Rotation is recommended** even though the tree is clean and the remote object store
  is gone. The old commits were publicly fetchable by SHA for the window the rewritten
  repo sat public (~15 min) and until Fastly's 5-minute cache expired after the deletion —
  a real, measured exposure window, not a theoretical one. Changing the MEGA account
  password(s) and regenerating the Jellyfin API key is the only complete mitigation;
  nothing in the fleet breaks if the key is rotated and `.env` + the installed plists are
  updated with it. **OWNER DECISION 2026-09-23: the rotation will NOT be done.** It is
  closed, not pending — do not raise it again or treat the exposure window as actionable.

This work was shipped without a fleet restart (`scripts/save-and-push.sh`): `.env` carries
the exact values the code previously hardcoded, so the running daemons see no change.
`verify_fleet.sh` printed `ALL CHECKS PASSED` (54 blocking checks) after the extraction.

---

## 12. After the reboot (2026-09-19) — close-out verified 2026-09-20

The owner rebooted on 2026-09-19 so launchd would re-read the refreshed agents. The
rename itself is DONE and pushed (`aa70cc5`); `ship-fleet.sh` could not put it into effect
because `kickstart -k` restarts the LOADED job definition, which still names the old path
(measured: `program = .../Developer/Media-Fleet/...`). The reboot also bounced the in-flight
Smurfs identify and the reaper drain — the owner knowingly waived §2.2/§2.4 for this one
boot, so this checklist is about proving recovery and finishing the rename, **not deploying
again**.

1. **Every daemon must be on the new path before anything is removed — DONE**, verified
   2026-09-20: `pgrep -fl 'Developer/Media-Fleet'` is empty and no live process resolves
   the old path.
2. **Remove the compatibility symlink — DONE.** `~/Developer/Media-Fleet` no longer exists
   (verified 2026-09-20). Leave the `-p:Media-Fleet` line in `~/Developer/.megaignore`; it
   is now insurance.
3. **Prove the fleet healthy:** `bash Torrent-Ingest/scripts/verify_fleet.sh` must print
   `ALL CHECKS PASSED`; `fleet_doctor --once --dry-run` and `fleet_health --once` clean or
   naming only §7/§10 known items. After boot the mount and Jellyfin take ~2 minutes to
   re-prime — wait for `~/MediaLibrary/Shows` to repopulate and `/Items/Counts` to answer
   before calling anything broken (§8.7).
4. **Reaper recovery** (the reboot interrupted the drain): confirm it restarted
   (`pgrep -f 'Torrent-Ingest/reap.py'`) and that Media-Syncer was not left paused. A
   lingering `state/reap_ms_paused` marker is self-healing — the next reaper cycle
   resumes Media-Syncer and clears it; check `media_sync.log` for the resume rather than
   removing the marker by hand. The drain restarts its probe from the beginning; that is
   the reboot's cost, not damage.
5. **Smurfs/identify recovery — it ran, and it failed for a new reason.** `6c413306…`
   produced a 24-file plan for a 409-file release and the coverage guard parked it intact
   at 2026-09-19 22:36. That is the §10.9 defect, not a transient: **do not re-drop the
   `.torrent` until the plan-assembly tool ships**, and do not hand-file any of the 54.8 GB pack (51 GiB by `du`)
   still in `~/Downloads/.torrent-ingest/`.
6. **Rotation (from §11) — CLOSED BY OWNER DECISION 2026-09-23: not doing it.** The Jellyfin
   API key and the shared MEGA password stay as they are; the exposure window recorded in
   §11 is accepted. Do not re-raise this, and do not re-run `startup.sh` for the key.

---

## 13. The FUSE layer: mediafs rides fuse-t, macFUSE removed (2026-09-21)

Symptom: after the 05:56 reboot the mount never came back. `com.mikeyferguson.mediafs`
crash-looped (13 × `mount_macfuse: the file system is not available (2)`), the
"macFUSE is too old" dialog kept popping, and `library_supervisor` held Jellyfin down
(`mount not ready -> stopping Jellyfin`) — the guard working as designed, not the fault.

Diagnosis: `fusepy` resolves its dylib by NAME (`ctypes.util.find_library('fuse')`),
which matched macFUSE's `/usr/local/lib/libfuse.dylib` → `libfuse.2.dylib` (macFUSE
5.0.6, hand-installed 2025-10-01, **no Homebrew receipt**), so the mount exec'd macFUSE's
`mount_macfuse` even though the fleet's FUSE layer is fuse-t. The nightly BrewUpgrade pass
could never help: macFUSE was not a cask it could see, and fuse-t 1.2.7 is already current
(`brew outdated` listed only jellyfin). The daemon was doing exactly what it was built to
do — hold the casks it cannot safely swap at 04:00 and leave them for a person; this
incident is the hand-upgrade path firing.

Fix: `run_mediafs.sh` pins `FUSE_LIBRARY_PATH=/usr/local/lib/libfuse-t.dylib` (fusepy's
supported override, `fuse.py:84`), read before `find_library`, and exits 1 with a clear
error if the library is missing instead of silently falling back. The LaunchAgent plist
comment and the mediafs README/docstring now say fuse-t.

macFUSE removal (by hand, admin): `macFUSE.framework`, both launch daemons + privileged
helpers (booted out first), the prefpane, `/usr/local/lib/libfuse.2.dylib`, the
`libfuse.dylib` symlink, `libfuse.la`, `/usr/local/include/fuse.h`, `pkgconfig/fuse.pc`.
**macFUSE's own uninstaller was deliberately NOT run**: it rm's
`/usr/local/lib/libfuse3.4.dylib` and `libfuse3.dylib`, which on this box are FUSE-T's
(timestamps + FUSE-T's own uninstaller agree; its wiki says the package never installs
`libfuse.dylib`). One remnant survives: `/Library/Filesystems/macfuse.fs` — SIP/`sunlnk`
on `/Library/Filesystems` refuses root `rm -rf` (`Operation not permitted` on every
entry). It is inert (no userspace lib and no launch job can invoke it); removing it needs
a SIP-disabled boot and was not judged worth it.

Verified after the change: a fresh `kickstart -k` of mediafs mounts
`fuse-t:/MediaLibrary (nfs)`, a read through the mount returns real bytes, and
`library_supervisor` restarted Jellyfin on its own; `/Items/Counts` = MovieCount 452,
SeriesCount 313, EpisodeCount 20386, BoxSetCount 90 (identical before and after). The
mount drop during the restart tripped the supervisor's "mount not ready" stop — expected,
and it recovered in ~70 s. Shipped with `Media-Syncer/scripts/ship.sh`.

---

## 14. WebDAV vs the mount for Jellyfin: considered, rejected (2026-09-21)

Owner asked whether WebDAV is faster than the FUSE mount and could improve streaming. Answer:
no, and the premise inverts the layering. Jellyfin cannot speak WebDAV (no remote-storage
support at all); the documented workaround is to mount the WebDAV endpoint locally and point
Jellyfin at that mount — i.e., WebDAV is a *backend protocol for a mount*, an extra hop, never
a replacement for the mount. The fleet already does the recommended thing, with a filesystem
that carries the inventory.

What an rclone mount / `rclone serve webdav` would forfeit here:

* **Inventory-local scans.** mediafs serves all 25,597 paths full-size from
  `remote_inventory.json`; `stat`/scans never touch MEGA. An rclone mount makes Jellyfin's
  first scan list directories over the API — precisely the load the account pool + VPN
  rotation exist to spread, but outside that machinery.
* **The tier engine.** Cold reads hydrate in 4 MB segments with 2 parallel workers
  (`STREAM_SEGMENT_BYTES` / `STREAM_WORKERS`, config.py:788), resume interrupted fills, serve
  un-hydrated ranges on demand, promote into the local cache, and prefetch the next episode
  (`PREFETCH_AHEAD`). rclone's VFS is a weaker, unmanaged re-implementation.
* **The deletion contract.** Deletes through the mount are tombstoned and reaped to every
  pool copy; an rclone mount knows nothing of `mediafs_deletions.jsonl` or the reaper.
* **Credential/rotation management.** Hydration rides the same rotating MEGA account pool as
  the syncer; a WebDAV endpoint needs its own access path and VPN story, unmanaged.

`.strm` files pointing at an HTTP/WebDAV endpoint were considered in the same pass: they would
bypass the local cache entirely (every play refetches from MEGA), sidestep the tier engine,
and add an unmanaged server — rejected on the same grounds.

Measurement that settles it: a 160 MB local file read through `~/MediaLibrary` runs at
**314 MB/s** (page-cached direct read: 17 GB/s). Playback bitrates sit orders of magnitude
below that, and a cold file is bound by MEGA's per-connection throughput, not the local
protocol — there is no streaming headroom for WebDAV to win back. Reconsider only if the pool
is replaced by a LAN-hosted store with no API metering; then a plain NFS/SMB/rclone mount
could be simpler. Shipped docs-only with `Media-Syncer/scripts/save-and-push.sh` (no daemon
runs this text, so no bounce).

---

## 15. Diagnosis queue — 2026-09-25 — **SHIPPED; kept as the evidence of record**

> **Status 2026-09-25.** Every seam below is implemented, tested and shipped — see the
> top shipped section. The items remain as the written diagnosis and the named
> acceptance targets: 15.1/15.2/15.3/15.5 shipped (the fixes are in the harness, not
> hand-moves), 15.4 is the live Simpsons acceptance, 15.6/15.7 are unchanged.

**OWNER'S STANDING INSTRUCTION FOR EVERY ITEM BELOW. The named answers are acceptance
targets, NOT repair instructions. The deliverable is NOT this assistant hand-moving a
file, hand-editing an `.nfo`, or typing a `--apply` with no new test behind it. Every
item here is a missing computed fact, a harness seam, or a prompt gap: the free AI
system must be upgraded so the fleet itself makes these calls and processes these
torrents in the future. A session that hand-fixes the symptoms and leaves the tool
blind has failed its brief, even if the mount looks right today. Same method as §10
and §5: compute the answer in the harness, state it in the prompt as fact, enforce it
in `validate_plan`/the repair tools, register a test in `scripts/verify_fleet.sh`
(explicit list, never auto-discovered), replay every new rejection over
`state/journal.jsonl` with the false-positive count in the commit message, fail open
on network errors, and check the owner-visible artifact (mount/Jellyfin/bytes) before
calling anything fixed.**

Context: the 2026-09-24 evening fix `4202bb3` (shipped, live) closed the
evicted-episodes-invisible digest and made a proven collision retry instead of fail.
Four packs are affected by the seams below. The Simpsons re-drop is already
recovering; the other three need the tool work.

### 15.1 American Dad! (2005) — `06dd53e1…` — one wrong-slot library file

* **State.** Record `failed` 2026-09-24 11:16Z. `chunk_done` 34, `chunk_filed` 34,
  wave `chunk_active` 34-65, `chunk_unfiled` = `Season 04/… - S04E06 - The 42-Year-Old
  Virgin …`. All bytes on disk under `~/Downloads/.torrent-ingest/American Dad! (2005)/`.
* **What the library holds wrong.** `Shows/American Dad! (2005)/Season 04/American
  Dad! (2005) - S04E06 - Independent Movie [WEBDL-1080p][EAC3 5.1][h265]-playWEB.mkv`
  (plus its `.nfo`/thumb). It was filed by this torrent's **wave-0 plan**, which mapped
  source `S10E06 - Independent Movie` to destination `S04E06` (stale-digest
  remapping).
* **THE COMPUTED ANSWER (TMDB 1433, the provider Jellyfin scrapes; verified this
  session):**
  * **"Independent Movie" is S10E06**, so the existing file belongs at
    `Shows/American Dad! (2005)/Season 10/American Dad! (2005) - S10E06 - Independent
    Movie [WEBDL-1080p][EAC3 5.1][h265]-playWEB.mkv`.
  * **"The 42-Year-Old Virgin" is S04E06**, so the wave-34 plan was right to target
    S04E06; the wrong-slot file is what blocked it (collision, then the old silent
    drop, then the park).
* **What to build (not do by hand).** The release's own `SxxExx` matches TMDB here;
  the 4202bb3 digest fix removes the reason the model remapped. The remaining work is
  (a) a repair path that computes a library file's true slot from TMDB + the release's
  numbering and re-files it through `refile_season.py`-class machinery (mount unlink,
  reaper queue, `dbhook.record_purge`, inventory rewrite), and (b) an identify-time
  guard that the release-vs-TMDB numbering is stated as fact so a season remap cannot
  be invented again. The source `.torrent` is **not** in `failed/` anymore; the mirror
  `Torrent-Ingest/state/torrent_sources/06dd53e1…torrent` survives and is the re-drop
  source.
* **Acceptance.** After the repair, `S04E06` holds *The 42-Year-Old Virgin* and
  `S10E06` holds *Independent Movie* through the mount, both `.nfo`s agree, and the
  re-dropped pack resumes from `chunk_done=34` and files the wave. Paste the mount
  listing + Jellyfin API output in the commit.

### 15.2 Family Guy — Seasons 1 to 20 — `705febda…` — same-release-key duplicate left unresolved

* **State.** Record `failed` 2026-09-25 02:27Z. `chunk_done` 96, `chunk_filed` 95,
  `chunk_dropped` [0], wave `chunk_active` 96-103, `chunk_unfiled` = `Season 07/… -
  S07E07 - Ocean's Three and a Half (Uncensored + Bale Scene).mkv`.
* **The wave holds TWO encodes of one episode** (both on disk, measured):
  * `Family Guy - S07E07 - Ocean's Three and a Half (Uncensored + Bale Scene).mkv`
    — 220,012,939 bytes.
  * `Family Guy - S07E07 - Ocean's Three and a Half (Uncensored + Commentary Audio
    Track).mkv` — 252,752,246 bytes.
  `plan_skeleton` leaves BOTH unslotted (two files, one release key), the model wrote a
  one-file plan naming the Commentary copy, and the merge had no way to record the
  other as the same episode → 1 unresolved → coverage park.
* **THE COMPUTED ANSWER (TMDB 1434): "Ocean's Three and a Half" is S07E07.** One
  destination only:
  `Shows/Family Guy (1999)/Season 07/Family Guy (1999) - S07E07 - Ocean's Three and a
  Half.mkv`. The second file is a deliberate same-episode alternate, not an
  unresolved release file. The harness's existing duplicate rule
  (`DUPLICATE_DEPRIORITIZE_MARKERS` + larger-size tiebreak) keeps the **Commentary**
  copy (252.7 MB); if the owner wants the **Bale Scene** cut instead, the rule must say
  so explicitly — either way the harness decides, records kept/dropped, and never the
  model by omission.
* **What to build.** Teach the harness that two files sharing a release `SxxEyy` and a
  cleaned episode title are alternates: slot the survivor (ranked) and mark the
  sibling `_deduped_dropped` (accounted), OR feed the merge's `unresolved` list back to
  the provider chain as a fixable rejection (see 15.3 — same seam). The prompt must
  state the rule: naming one file of a same-key pair means the other is the same
  episode's alternate, and both files' episode_title must agree. Replay: count how
  many historical plans carry same-key+same-cleaned-title pairs (start with the Family
  Guy wave and the Smurfs incident) and prove no legitimate distinct-episode pair is
  collapsed (the title clean is the guard: II vs III must NOT collapse).
* **Acceptance.** One S07E07 file through the mount, `.nfo` title = *Ocean's Three and
  a Half*, the dropped alternate recorded in `chunk_dropped`/`decisions.log`, and the
  re-dropped pack (mirror `state/torrent_sources/705febda…torrent`) completes the
  wave.

### 15.3 Friends (1994) — `1a6558e5…` — the merge lost 4 entries to a mis-transcribed src

* **State.** Record `failed` 2026-09-25 01:54Z. Wave 0 = **all 32 Featurettes** (Bonus
  Disc + Featurettes/Season 1-5/10), `chunk_active` 0-31, total pack 181.97 GB. Every
  byte is on disk under
  `~/Downloads/.torrent-ingest/Friends (1994) Season 1-10 S01-S10 (1080p BluRay x265
  HEVC 10bit AAC 5.1 Silence)/Featurettes/`.
* **What the model wrote.** A complete 32-entry plan placing every featurette as a
  locked Season-00 special, `Friends (1994) - S00E01…S00E32.mkv`, with titles/plots.
  **But all 32 `src` paths dropped the closing `)` of the torrent root** — plan
  `…AAC 5.1 Silence/Featurettes/…` vs disk `…AAC 5.1 Silence)/Featurettes/…` (32/32
  entries; the model re-typed instead of copying the skeleton's `src`).
  `merge_skeleton_plan` matched 28 by unique basename; the 4 whose basenames repeat
  across season folders could not be attributed (its basename fallback requires a
  unique name). The merge returned `filled 28, unresolved 4`; `run_identify` ignores
  that list (and disables the `--require-list` nudge whenever a skeleton exists), so
  the 28-file plan was accepted and the coverage contract parked the release.
* **THE COMPUTED ANSWER — nothing here is content-ambiguous; the harness lost the
  mapping.** The four unresolved files are distinct featurettes; the plan's slots and
  titles are correct:
  * `Featurettes/Season 10/Friends of Friends_new.mkv` → S00E11
    ("Friends of Friends (Season 10)").
  * `Featurettes/Season 2/Friends of Friends_new.mkv` → S00E15
    ("Friends of Friends (Season 2)").
  * `Featurettes/Season 3/“What’s Up with Your Friends”_new.mkv` → S00E25
    ("What’s Up with Your Friends (Season 3)").
  * `Featurettes/Season 4/“What’s Up with Your Friends”_new.mkv` → S00E28
    ("What’s Up with Your Friends (Season 4)").
  (Numbering is the plan's own; any unique S00 slots with the same titles are equally
  acceptable — the requirement is all 32 filed as locked specials, titles/plots
  intact.)
* **What to build — THIS IS THE SYSTEMIC SEAM (Family Guy and Friends are the same
  bug):**
  1. `run_identify` must treat `merge_skeleton_plan`'s `unresolved` as a **fixable
     rejection fed to the next provider** (the missing filenames in the failure
     context / `--require-list`), and only park after the chain is exhausted. Today a
     single unresolved file takes down a 700 GB pack terminally on the first
     incomplete answer.
  2. Attribute a model entry whose basename is ambiguous by **parent folder +
     basename** (`Featurettes/Season 2/Friends of Friends_new.mkv` is unique) and/or
     heal a mis-transcribed `src` root against the disk before matching
     (`_heal_missing_src` exists in `validate_plan` but never sees merge-dropped
     entries). The skeleton's `src` is the harness's enumeration and must never be
     re-typed by the model — say that in the prompt and enforce it.
  3. Replay the new matching/feedback rules over `state/journal.jsonl` (the Smurfs
     151-file re-fetch, this Friends plan, and the Family Guy wave are the fixtures)
     and register the test.
* **Acceptance.** Re-drop the mirror `state/torrent_sources/1a6558e5…torrent`; wave 0
  files 32/32 Featurettes as S00 specials with the four titles above, `chunk_filed`
  records all 32, and the record advances to the next wave.

### 15.4 The Simpsons (1989) — `1d9098aa…` — recovering; the live acceptance for `4202bb3`

* Re-dropped from `failed/` 2026-09-24 23:48 ("resuming chunked waves at 43 file(s)
  already filed"), admitted 2026-09-25 03:11:33 with the corrected digest, and
  **parked waiting for disk space** (43 MB admittable, smallest next file 592 MB).
  It resumes automatically. **Next session must confirm** the S03E05-S04E12 wave filed
  at S03E05/E06 (not the old collision slots) and the record reaches terminal
  `completed` — this is the owner-visible proof that the evicted-episode summary fix
  works on the real pack. If it parks again, the failure line and
  `state/tmp/1d9098aa…-w*` artifacts are the evidence.

### 15.5 Doctor Who (2005) — the S00E04 specials conflict (doctor worklist, `auto: false`, sev 3)

* **State.** `Season 00` holds two files at `S00E04`:
  * `Doctor Who (2005) - S00E04 The End Of Time Part 1.mp4` — its locked `.nfo` says
    `<season>0</season><episode>16</episode>`.
  * `Doctor Who (2005) - S00E04 The Return Of Doctor Mysterio.mp4` — its locked `.nfo`
    says `<episode>149</episode>`.
  (`state/doctor_worklist.json`: "S00E04 has two files … rename DIFFERENT episodes …
  re-file it, do not delete it.")
* **THE COMPUTED ANSWER.** TMDB 57243 (pinned; Jellyfin scrapes it) numbers the
  specials `S00E016 = The End of Time (1)`, `S00E066 = The Snowmen`,
  `S00E149 = The Return of Doctor Mysterio`. But the LIBRARY owns its own sequential,
  locked specials scheme `S00E01…S00E22` — era-ordered: E01 Day of the Doctor, E02
  Time of the Doctor, **E03 Husbands of River Song, E04 Return of Doctor Mysterio,
  E05 Twice Upon a Time**, … E14 A Christmas Carol, E15 Doctor/The Widow/Wardrobe,
  E16 The Snowmen, E17 Christmas Invasion … E22 The Waters of Mars. So:
  * **The Return Of Doctor Mysterio at S00E04 is CORRECT for the library's scheme**
    (its `.nfo` `<episode>149</episode>` is the TMDB number and is the thing to
    repair).
  * **The End Of Time Part 1 belongs at S00E23** — the next free slot after The
    Waters of Mars (E22); Part 2, if acquired, is E24. It currently sits at S00E04
    with a TMDB `<episode>16</episode>`.
  * The conflict is real (one slot, two files) but the doctor's "two different
    episodes" read comes from TMDB-numbered nfos colliding with the library's own
    scheme.
* **What to build.** A repair path that (a) computes a special's slot from its content
  identity + the library's own locked scheme (not from the nfo's foreign number),
  (b) re-files through the tool with an nfo rewrite that matches the destination slot,
  and (c) adds a guard in `validate_plan`/`media_doctor` that a filed special's `.nfo`
  `<season>/<episode>` agrees with its destination (so a TMDB number can never again
  be written beside a library-scheme filename). The specials scheme itself must be
  computed/persisted (it is currently implicit in the locked nfos) so the free AI is
  told it as fact. Register the test; replay the doctor worklist.

### 15.6 Other open items already known (no action unless touched)

* `verify_owner_report.py` (read-only) at the time of this diagnosis: **4 PASS, 0 FAIL,
  1 PENDING, 0 REVIEW** — the PENDING is the three One Piece chapter tails
  (`Chapter 1093/1098/1112.zip`), which are owned/superseded by design; leave them.
* Toriko blank plots and TZ (2019) identity/art are repaired live (§10.0); the Smurfs
  row 5 is closed. The free-AI upgrade is implemented; the seams in 15.1–15.5 are its
  remaining gaps.
* Provider capacity at diagnosis: `cloudflare` capped for the day, `gemini` 429ing,
  `groq` confirm-mode only, `mistral`/`nvidia`/`openrouter` usable. A saturated chain
  can delay registration for hours (measured this session) — the merge-unresolved
  feedback (15.3) must not depend on a fresh provider being available, which is another
  reason the harness should compute same-key duplicates itself.

### 15.7 Definition of done for the next session

1. Every seam above fixed in the tool — prompt, harness, validator, doctor — with a
   registered test in `scripts/verify_fleet.sh`; `ALL CHECKS PASSED`.
2. A journal replay for every new rejection/matching rule, false-positive count in the
   commit message.
3. The named files land at the named destinations through the fixed tools (no hand
   `mv`, no hand-edited `.nfo`), and the owner-visible artifact (mount listing, Jellyfin
   API, `chunk_filed`) is pasted into the commit.
4. The free AI is demonstrably able to make the calls: the Family Guy same-key
   duplicate, the Friends 32-featurette plan, and the American Dad TMDB numbering must
   all be decided by the harness/prompt, not by the assistant.
