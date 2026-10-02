#!/usr/bin/env python3
"""Brew-Upgrade: keeps this machine's packages current without anyone remembering to.

Homebrew is the bulk of it, and npm's global packages ride along at the end -- they are
the only other package manager on this box that upgrades without a password. Claude Code
and Codex follow, through their own native updaters: both live in ~/.local/bin, outside
brew and npm, and the board's headless turns run them with nobody at a prompt to see an
update notice. What is
deliberately NOT here: the conda base (the fleet's python lives in it; see
PROTECTED_CASKS), system gems, and `softwareupdate`, all of which need root that a
LaunchAgent cannot get and should not have.

`brew upgrade` is the maintenance nobody does until something is already broken -- a
year-old openssl, an rclone that predates the remote's API change, a node the global
npm packages no longer match. This runs it once a day, unattended, and logs what moved.

Design, and why:

  * SCHEDULED HOURLY, GATED TO ONCE A DAY. The plist wakes this every hour and the
    stamp file decides whether there is anything to do. A plain daily calendar job on a
    laptop misses the whole day whenever the machine is asleep, off, or on battery at
    04:00, and there is no retry until tomorrow. Hourly + a gate gives the same one
    upgrade per day plus a catch-up whenever the machine is actually awake.

  * PREFERRED WINDOW, NOT A HARD ONE. Normal runs happen in the quiet hours; if the box
    was unavailable through the whole window, the run stops being fussy after
    OVERDUE_HOURS and takes the next chance it gets.

  * NOT ON A LOW BATTERY. A large upgrade on a laptop at 8% is how you get a half-linked
    Cellar. On battery below MIN_BATTERY_PCT it skips and tries again next hour.

  * FORMULAE ALWAYS, CASKS EXCEPT THE LOAD-BEARING ONES. Upgrading a formula swaps a
    binary that running daemons already hold open, which is safe -- they pick it up on
    their next restart. Casks are applications: PROTECTED_CASKS names the three the
    fleet is actually built on (the conda base its daemons' python lives in, the FUSE
    layer the media mount rides on, the media server itself). Those get *reported* as
    outdated and upgraded by hand in a window someone is watching, never at 04:00 by a
    daemon. Casks are also non-greedy: an app that self-updates is left to do so.

  * RUN UNDER /usr/bin/python3, NOT THE BREW ONE. This process is upgrading Homebrew's
    own python; interpreting it with the interpreter being replaced underneath is a
    needless way to lose a run. The system python is never a brew upgrade's target, and
    this module is stdlib-only so it is happy at 3.9.

Two flags, for hands-on use:  --dry-run  says what it would upgrade and touches
nothing;  --force  ignores the once-a-day/quiet-hours/battery gate and upgrades now.
Unflagged is what launchd runs.

A cask upgrade that needs an admin password cannot get one from launchd -- it fails,
gets logged, and is left for a human. That is deliberate: nothing here escalates.

Companion to doctor.py in this repo, and the reason the pair live together: a brew
upgrade of node relinks the prefix out from under the globally-installed opencode-ai,
which is exactly the breakage the doctor repairs on its next minute.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

HOME = Path.home()
REPO = Path(__file__).resolve().parent
LOG = HOME / "Library" / "Logs" / "BrewUpgrade.log"
LOG_MAX_BYTES = 2_000_000
STATE_DIR = REPO / "state"
STATE_FILE = STATE_DIR / "brew_upgrade.json"
LOCK_FILE = STATE_DIR / "brew_upgrade.lock"

BREW = Path("/opt/homebrew/bin/brew")

DRY_RUN = False   # --dry-run: report the plan, run nothing that changes the machine
FORCE = False     # --force:   run now regardless of the gate

# Gate: at most one attempt per MIN_INTERVAL_HOURS. 20, not 24, so a run that happens at
# 04:10 one day is not pushed to 04:10 + drift out of the window on the next.
MIN_INTERVAL_HOURS = 20
QUIET_HOURS = (3, 4, 5, 6, 7)     # preferred local hours to upgrade in
OVERDUE_HOURS = 48                # after this long without a success, any hour will do
MIN_BATTERY_PCT = 40              # on battery below this, wait for power
LOCK_STALE_HOURS = 6              # a lock older than this belonged to a dead run

# Casks the fleet runs on. Upgrading these is a supervised act, not a nightly one:
# each takes something down that other daemons assume is up.
PROTECTED_CASKS = {
    "miniconda": "hosts the conda env every Torrent-Ingest daemon's python lives in",
    "fuse-t":    "the FUSE layer the mediafs mount rides on",
    "jellyfin":  "the media server librarysupervisor starts and stops",
}

BREW_ENV = {
    "HOMEBREW_NO_AUTO_UPDATE": "1",     # this script decides when to `brew update`
    "HOMEBREW_NO_ENV_HINTS": "1",
    "HOMEBREW_NO_ANALYTICS": "1",
    "NO_COLOR": "1",
}


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def trim_log() -> None:
    """Keep the log to its last half when it passes LOG_MAX_BYTES. An unattended daily
    job that runs for years should not be the thing that fills the disk."""
    try:
        if LOG.stat().st_size <= LOG_MAX_BYTES:
            return
        lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines(True)
        LOG.write_text("".join(lines[len(lines) // 2:]), encoding="utf-8")
    except OSError:
        pass


def run(cmd: list[str], timeout: int, mutating: bool = False) -> tuple[int, str]:
    """Run a command with the brew environment; return (exit code, combined output).
    Never raises: a timeout or a missing binary is a bad exit code like any other.
    Anything marked `mutating` is only described, not run, under --dry-run."""
    if mutating and DRY_RUN:
        log(f"  [dry-run] would run: {' '.join(cmd)}")
        return 0, ""
    env = dict(os.environ)
    env.update(BREW_ENV)
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except OSError as exc:
        return 127, str(exc)


def log_output(out: str, source: str, tail: int = 15) -> None:
    """Log the tail of a command's output, tagged with the command it came from.

    Untagged, the blocks are unreadable: this job runs four or five brew commands plus
    npm, each contributes a tail, and a reader looking at 03:00's log has no way to tell
    a `cleanup` warning from an `upgrade` one. The tag is the whole difference between a
    log you can diagnose from and a log you can only scroll."""
    lines = [ln for ln in out.splitlines() if ln.strip()]
    for ln in lines[-tail:]:
        log(f"    [{source}] {ln}")


# --------------------------------------------------------------------------- state

def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        tmp.replace(STATE_FILE)
    except OSError as exc:
        log(f"  ! could not write {STATE_FILE.name}: {exc}")


def parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# ----------------------------------------------------------------------- the gates

def hours_since(when: datetime | None) -> float:
    return float("inf") if when is None else (datetime.now() - when).total_seconds() / 3600.0


def battery_ok() -> tuple[bool, str]:
    """False only when running on battery below MIN_BATTERY_PCT. Anything unreadable
    counts as fine -- a desktop with no battery must not be gated out."""
    code, out = run(["/usr/bin/pmset", "-g", "batt"], timeout=15)
    if code != 0 or "Battery Power" not in out:
        return True, "on AC power"
    m = re.search(r"(\d+)%", out)
    if not m:
        return True, "on battery, charge unknown"
    pct = int(m.group(1))
    if pct < MIN_BATTERY_PCT:
        return False, f"on battery at {pct}% (need {MIN_BATTERY_PCT}%)"
    return True, f"on battery at {pct}%"


def should_run(state: dict) -> tuple[bool, str]:
    last_attempt = parse_ts(state.get("last_attempt"))
    last_success = parse_ts(state.get("last_success"))

    if FORCE:
        return True, "forced"

    since_attempt = hours_since(last_attempt)
    if since_attempt < MIN_INTERVAL_HOURS:
        return False, f"upgraded {since_attempt:.1f}h ago (every {MIN_INTERVAL_HOURS}h)"

    # "Overdue" is measured from the last success, or -- on a box that has never had one
    # -- from the first time this ever ran. Without that floor a fresh install counts as
    # infinitely overdue and upgrades at whatever hour it happens to be installed, which
    # is the one time of day someone is certainly sitting at the machine.
    baseline = last_success or parse_ts(state.get("first_seen"))
    overdue = hours_since(baseline) >= OVERDUE_HOURS
    if datetime.now().hour not in QUIET_HOURS and not overdue:
        return False, "outside the quiet-hours window and not overdue"

    ok, why = battery_ok()
    if not ok:
        return False, why

    return True, ("overdue" if overdue else "due") + f", {why}"


def take_lock() -> bool:
    """One run at a time. A lock left by a run that died (or a reboot mid-upgrade) is
    reclaimed once it is LOCK_STALE_HOURS old rather than blocking forever."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        age = (datetime.now() - datetime.fromtimestamp(LOCK_FILE.stat().st_mtime))
        if age < timedelta(hours=LOCK_STALE_HOURS):
            return False
        log(f"  reclaiming a stale lock ({age.total_seconds() / 3600:.1f}h old)")
    except OSError:
        pass
    try:
        LOCK_FILE.write_text(f"{os.getpid()} {datetime.now().isoformat(timespec='seconds')}\n",
                             encoding="utf-8")
        return True
    except OSError:
        return False


def drop_lock() -> None:
    try:
        LOCK_FILE.unlink()
    except OSError:
        pass


# ------------------------------------------------------------------------ the work

def outdated() -> tuple[list[str], list[str]]:
    """(formulae, casks) that have a newer version. `brew outdated --json` is the only
    form that distinguishes the two reliably; a parse failure yields empty lists, which
    makes this pass a no-op rather than an upgrade of something unexamined."""
    code, out = run([str(BREW), "outdated", "--json=v2"], timeout=600)
    if code != 0:
        log(f"  ! brew outdated failed (exit {code})")
        log_output(out, "brew outdated")
        return [], []
    try:
        data = json.loads(out[out.index("{"):])
    except (ValueError, json.JSONDecodeError) as exc:
        log(f"  ! could not parse brew outdated: {exc}")
        return [], []
    formulae = [f.get("name") for f in data.get("formulae", []) if f.get("name")]
    casks = [c.get("name") for c in data.get("casks", []) if c.get("name")]
    return formulae, casks


def upgrade_npm_globals() -> None:
    """`npm update -g`, after the brew pass and never before it: npm lives in the node
    that brew just replaced, and updating globals against the outgoing node is how you
    get packages linked to a prefix that is about to move.

    Failure is logged and shrugged off. The one global that matters here is opencode-ai,
    and doctor.py reinstalls that within a minute of it being broken -- which is the
    whole reason these two daemons share a repo."""
    npm = Path("/opt/homebrew/bin/npm")
    if not npm.exists():
        return
    code, out = run([str(npm), "update", "-g"], timeout=1800, mutating=True)
    if DRY_RUN:
        return  # run() already said what it would have done
    if code == 0:
        log("  npm globals updated")
    else:
        log(f"  ! npm update -g exited {code}")
        log_output(out, "npm", tail=8)


AGENT_CLIS = (("claude", ["update"]), ("codex", ["update"]))


def upgrade_agent_clis() -> None:
    """`claude update` and `codex update`, the native installers' own updaters. Each
    swaps a symlink in ~/.local/bin to a new versioned build, so a session already
    running keeps its binary and the next one starts on the new one.

    Failure is logged and shrugged off, like npm's: a missed day is caught tomorrow."""
    for name, args in AGENT_CLIS:
        exe = Path.home() / ".local" / "bin" / name
        if not exe.exists():
            continue
        code, out = run([str(exe)] + args, timeout=900, mutating=True)
        if DRY_RUN:
            continue
        if code == 0:
            log_output(out, f"{name} update", tail=1)
        else:
            log(f"  ! {name} update exited {code}")
            log_output(out, f"{name} update", tail=8)


def main() -> int:
    trim_log()

    if not BREW.exists():
        log(f"no brew at {BREW}; nothing to do")
        return 0

    state = load_state()
    if not state.get("first_seen"):
        state["first_seen"] = datetime.now().isoformat(timespec="seconds")
        if not DRY_RUN:
            save_state(state)

    go, why = should_run(state)
    if not go:
        return 0  # silent: this happens 23 times a day and is not news
    if not DRY_RUN and not take_lock():
        log("another upgrade run holds the lock; skipping this hour")
        return 0

    started = datetime.now()
    log(f"=== brew upgrade run ({why}) ===")
    if not DRY_RUN:
        state["last_attempt"] = started.isoformat(timespec="seconds")
        save_state(state)

    ok = True
    try:
        code, out = run([str(BREW), "update"], timeout=1800, mutating=True)
        if code != 0:
            log(f"  ! brew update failed (exit {code}); upgrading against the old index anyway")
            log_output(out, "brew update")
            ok = False

        formulae, casks = outdated()
        held = sorted(c for c in casks if c in PROTECTED_CASKS)
        casks = [c for c in casks if c not in PROTECTED_CASKS]

        if not formulae and not casks and not held:
            log("  everything is current")
        else:
            log(f"  outdated: {len(formulae)} formula(e), {len(casks)} cask(s)"
                + (f", {len(held)} held" if held else ""))

        if formulae:
            log(f"  upgrading formulae: {', '.join(sorted(formulae))}")
            code, out = run([str(BREW), "upgrade", "--formula"], timeout=10800, mutating=True)
            log_output(out, "brew upgrade")
            if code != 0:
                log(f"  ! formula upgrade exited {code}")
                ok = False

        if casks:
            # One at a time: a single cask that wants an admin password (or a running
            # app it cannot quit) must not take the rest of the list down with it.
            for cask in sorted(casks):
                code, out = run([str(BREW), "upgrade", "--cask", cask], timeout=5400, mutating=True)
                if code == 0:
                    log(f"  upgraded cask {cask}")
                else:
                    log(f"  ! cask {cask} exited {code}; left for a human")
                    log_output(out, f"cask {cask}", tail=8)

        for cask in held:
            log(f"  HELD: {cask} is outdated -- {PROTECTED_CASKS[cask]}. "
                f"Upgrade it by hand: brew upgrade --cask {cask}")

        upgrade_npm_globals()
        upgrade_agent_clis()

        # Reclaim the space the upgrade just spent. Keeps a week of downloads so a bad
        # upgrade can still be rolled back from the cache.
        for cmd, label in ((["autoremove"], "autoremove"), (["cleanup", "--prune=7"], "cleanup")):
            code, out = run([str(BREW)] + cmd, timeout=1800, mutating=True)
            if code != 0:
                log(f"  ! brew {label} exited {code}")
            elif out.strip():
                log_output(out, f"brew {label}", tail=3)

    finally:
        if not DRY_RUN:
            drop_lock()

    finished = datetime.now()
    if DRY_RUN:
        log("=== dry run; nothing was changed ===")
        return 0
    if ok:
        state["last_success"] = finished.isoformat(timespec="seconds")
    state["last_result"] = "ok" if ok else "partial"
    state["last_duration_seconds"] = int((finished - started).total_seconds())
    save_state(state)

    log(f"=== done in {state['last_duration_seconds']}s ({state['last_result']}) ===")
    return 0


if __name__ == "__main__":
    DRY_RUN = "--dry-run" in sys.argv[1:]
    FORCE = "--force" in sys.argv[1:] or DRY_RUN
    for arg in sys.argv[1:]:
        if arg not in ("--dry-run", "--force"):
            print(f"usage: {Path(__file__).name} [--dry-run] [--force]", file=sys.stderr)
            sys.exit(2)
    sys.exit(main())
