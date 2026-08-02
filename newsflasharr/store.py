"""Newsflasharr durable state: state.json, notifications.jsonl ledger, failed/.

Stdlib only. Leader-only writer (enforced by the collector, not here).
Spec §4.8, §8.3.
"""
from __future__ import annotations

import json
import os


class Store:
    def __init__(self, base_dir, max_ledger_mb=10, failed_keep_days=14,
                 redact=None):
        self.base_dir = base_dir
        self.spool_dir = os.path.join(base_dir, "spool")
        self.pending_dir = os.path.join(base_dir, "pending")
        self.failed_dir = os.path.join(base_dir, "failed")
        self.state_path = os.path.join(base_dir, "state.json")
        self.ledger_path = os.path.join(base_dir, "notifications.jsonl")
        self.max_ledger_bytes = int(max_ledger_mb * 1024 * 1024)
        self.failed_keep_s = failed_keep_days * 86400.0
        self._redact = redact or (lambda t: t)
        # True when the MOST RECENT `load_state` sidelined a corrupt file and
        # returned a rebuilt, empty state. `_corrupt_recovered` inside the
        # returned dict cannot carry that fact reliably: `gates.Gates` POPS it
        # while consuming the conservative-cap recovery, and that happens
        # inside `Deliverer.tick`, before the collector reaches any other
        # decision that depends on knowing the state was rebuilt. This
        # attribute is not popped by anyone, so a later caller in the same tick
        # can still tell the difference between "no keys yet because this
        # install is fresh" and "no keys because the file was just lost".
        self.last_load_corrupt = False

    def ensure_dirs(self):
        for d in (self.spool_dir, self.pending_dir, self.failed_dir):
            os.makedirs(d, exist_ok=True)
        try:
            os.chmod(self.base_dir, 0o700)
        except OSError:
            pass
        self._ensure_ticker_file()

    def _ensure_ticker_file(self):
        """Create an empty ticker.txt if absent. Load-bearing.

        If an operator has configured a drawtext OutputProfile pointing at
        this path and the file is missing, drawtext fails to initialise the
        whole filtergraph and ffmpeg exits -- but Dispatcharr has already
        marked the transcode ACTIVE, so the client attaches to a buffer that
        never fills. The symptom is a HUNG CHANNEL with no error, respawned
        on every reconnect. This runs at the top of every tick, so the file
        is recreated within one poll of anything deleting it.

        Inlined rather than importing ticker.py: store.py is
        standalone-loadable and must not grow a sibling import.
        """
        path = os.path.join(self.base_dir, "ticker.txt")
        try:
            if not os.path.exists(path):
                with open(path, "w", encoding="utf-8") as f:
                    f.write("")
        except Exception:
            # Swallowed DELIBERATELY, and only because the condition is
            # reported elsewhere: `show_status` checks for this file directly
            # on disk and raises "ticker.txt: MISSING" as an error whenever the
            # ticker is enabled. That independent check is what justifies the
            # silence here -- this runs on every tick and must never raise.
            #
            # An earlier version of this fix RETURNED a bool instead. Nothing
            # read it, which is the same dead-signal pattern as
            # `_corrupt_recovered` and the `spool_full` marker: a value written
            # to satisfy a reviewer rather than to reach an operator. Reporting
            # from the surface the operator actually looks at is the fix; a
            # return value nobody consumes is not.
            pass

    def load_state(self, now):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                state = json.load(f)
            self.last_load_corrupt = False
            return state
        except FileNotFoundError:
            self.last_load_corrupt = False
            return {}
        except (OSError, ValueError):
            try:
                os.replace(self.state_path,
                           self.state_path + f".corrupt-{int(now)}")
            except OSError:
                pass
            self.last_load_corrupt = True
            return {"_corrupt_recovered": True}

    def save_state(self, state, now):
        try:
            body = dict(state)
            body["written_at"] = now
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(body, f, separators=(",", ":"), default=str)
            os.replace(tmp, self.state_path)
            return True
        except OSError:
            return False

    def note_tick_error(self, exc, now):
        """Persist WHY a tick raised, TYPE NAME ONLY.

        The collector used to stash this in an in-memory dict that nothing
        read -- and it could not have worked from memory anyway: only the
        LEADER ticks, while `show_status` is served by whichever worker took
        the click, which is usually not the leader. Disk is the only channel
        between them, which is why this lives on the Store.

        Never `str(exc)`: `state.json` is surfaced by `show_status` and
        `docs/API.md` warns those dumps get pasted into issues -- an OSError
        string carries a filesystem path. Same rule as `_note_settings_error`,
        `_note_lock_error` and the smtp `detail`.

        Best-effort: if the state file cannot be written there is nothing
        further to be done, and the stale heartbeat is then the signal.
        """
        try:
            state = self.load_state(now)
            prev = state.get("tick_error") or {}
            state["tick_error"] = {
                "type": type(exc).__name__,
                "at": now,
                "count": prev.get("count", 0) + 1,
            }
            self.save_state(state, now)
        except Exception:
            pass

    @staticmethod
    def clear_tick_error(state):
        """Drop a recorded tick error from an in-memory state dict.

        Takes the dict rather than re-reading, so the caller's next
        `save_state` persists it -- a tick that succeeded is already writing
        state, and a second read/write pair every tick would be waste.
        """
        state.pop("tick_error", None)

    def ledger(self, record):
        try:
            line = self._redact(
                json.dumps(record, separators=(",", ":"), default=str)) + "\n"
            try:
                if os.path.getsize(self.ledger_path) >= self.max_ledger_bytes:
                    os.replace(self.ledger_path, self.ledger_path + ".1")
            except OSError:
                pass
            with open(self.ledger_path, "a", encoding="utf-8") as f:
                f.write(line)
            return True
        except OSError:
            return False

    def ledger_tail(self, n=30):
        try:
            with open(self.ledger_path, encoding="utf-8") as f:
                lines = f.readlines()[-n:]
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    def move_failed(self, path, reason, now):
        """Quarantine a poison file. Returns True only if it actually MOVED.

        The bool matters because the caller ledgers the outcome: this used to
        swallow the OSError, so `malformed` was written for a file that never
        left `pending/` -- re-parsed every tick, emitting a fresh row forever,
        burning send budget and burying real rows while the ledger asserted it
        had been quarantined. A MISLEADING row is worse than an absent one.

        The `.reason` sidecar failing is not fatal: the file is quarantined
        either way, which is what the caller needs to know.
        """
        name = os.path.basename(path)
        try:
            os.makedirs(self.failed_dir, exist_ok=True)
            os.replace(path, os.path.join(self.failed_dir, name))
        except OSError:
            return False
        try:
            with open(os.path.join(self.failed_dir, name + ".reason"),
                      "w", encoding="utf-8") as f:
                f.write(self._redact(f"{now}: {reason}"))
        except OSError:
            pass
        return True

    def prune_failed(self, now):
        removed = 0
        try:
            for name in os.listdir(self.failed_dir):
                p = os.path.join(self.failed_dir, name)
                try:
                    if now - os.path.getmtime(p) > self.failed_keep_s:
                        os.remove(p)
                        removed += 1
                except OSError:
                    pass
        except OSError:
            pass
        return removed
