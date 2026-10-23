#!/usr/bin/env python3
"""An on-demand macOS EventKit CLI for Apple Reminders. Writes require --execute; no permission prompts."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import threading
import time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

KEY_MARKER = "Reminder-CLI-Key: "
EXPIRES_MARKER = "Reminder-CLI-Expires: "
MAX_INPUT = 65536
FETCH_TIMEOUT = 30
LOCK_TIMEOUT = 30
DEFAULT_STATE = Path.home() / "Library/Application Support/reminder-cli/state.json"


class ReminderError(Exception):
    pass


@dataclass(frozen=True)
class ReminderList:
    id: str
    source_id: str
    name: str
    source: str
    writable: bool


@dataclass(frozen=True)
class Reminder:
    id: str
    list_id: str
    title: str
    due: datetime | None
    completed: bool
    key: str = ""
    expires: datetime | None = None
    alarm: datetime | None = None
    revision: str = ""  # Private fingerprint of fields not shown above; never emitted.


def text(value: Any, label: str, limit: int = 512) -> str:
    # splitlines() also breaks on U+2028/U+2029/U+0085/U+001C-1E, which could otherwise forge a notes marker line.
    if (not isinstance(value, str) or len(value) > limit or not value.strip() or value.splitlines() != [value]
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ReminderError(f"Invalid {label}; use a bounded single-line string.")
    return value


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ReminderError("Unknown IANA timezone.") from None


def parse_time(value: Any, tz: ZoneInfo) -> datetime:
    text(value, "timestamp", 64)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        raise ReminderError("Timestamps must be ISO 8601.") from None
    if parsed.tzinfo is None:
        if parsed.replace(tzinfo=tz).utcoffset() != parsed.replace(tzinfo=tz, fold=1).utcoffset():
            raise ReminderError("Ambiguous or nonexistent local time; add an explicit UTC offset.")
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def notes_with_markers(notes: str, key: str, expires: datetime | None) -> str:
    lines = [notes] if notes else []
    lines.append(KEY_MARKER + key)
    if expires is not None:
        lines.append(EXPIRES_MARKER + expires.astimezone(timezone.utc).isoformat())
    return "\n".join(lines)


def read_markers(notes: str) -> tuple[str, datetime | None]:
    """Markers are informational and identify the key. They are not proof of origin: the ledger is."""
    key, expires = "", None
    for line in notes.splitlines():
        if line.startswith(KEY_MARKER):
            key = line[len(KEY_MARKER):]
        elif line.startswith(EXPIRES_MARKER):
            try:
                parsed = datetime.fromisoformat(line[len(EXPIRES_MARKER):])
                expires = parsed if parsed.tzinfo is not None else None
            except ValueError:
                expires = None
    return key, expires


def spec_from_json(value: Any, timezone_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReminderError("Input must be one JSON object.")
    unknown = set(value) - {"title", "notes", "due", "alert", "key", "expires", "timezone"}
    if unknown:
        raise ReminderError("Unknown input fields.")
    tz = zone(value.get("timezone", timezone_name))
    title = text(value.get("title"), "title")
    key = text(value.get("key"), "key", 200)
    notes = value.get("notes", "")
    if not isinstance(notes, str) or len(notes) > 32768 or "\x00" in notes:
        raise ReminderError("Notes must be a string of at most 32768 characters without NUL.")
    if any(line.startswith((KEY_MARKER, EXPIRES_MARKER)) for line in notes.splitlines()):
        raise ReminderError("Notes may not contain reserved management markers.")
    due = parse_time(value.get("due"), tz)
    alert = value.get("alert", True)
    if not isinstance(alert, bool):
        raise ReminderError("alert must be true or false.")
    expires = parse_time(value["expires"], tz) if value.get("expires") is not None else None
    if expires is not None and expires < due:
        raise ReminderError("expires must not be before due.")
    return {"title": title, "notes": notes, "due": due, "alert": alert, "key": key, "expires": expires, "timezone": tz.key}


def json_object(raw: str) -> dict:
    if len(raw.encode("utf-8")) > MAX_INPUT:
        raise ReminderError("JSON input exceeds the 64 KiB limit.")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ReminderError("Duplicate JSON object keys are not accepted.")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ReminderError("Non-finite JSON numbers are not accepted.")))
    except (ValueError, RecursionError):
        raise ReminderError("Malformed or excessively nested JSON input.") from None
    if not isinstance(value, dict):
        raise ReminderError("Input must be a JSON object.")
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if depth > 16:
            raise ReminderError("JSON nesting exceeds the 16-level limit.")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
    return value


def input_object(path: Path | None) -> dict:
    if path is None:
        try:
            raw = sys.stdin.read(MAX_INPUT + 1)
        except UnicodeError:
            raise ReminderError("Input must be UTF-8 JSON.") from None
        return json_object(raw)
    try:
        fd = os.open(path.expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ReminderError("JSON input/configuration must be a regular file.")
            data = os.read(fd, MAX_INPUT + 1)
        finally:
            os.close(fd)
        return json_object(data.decode("utf-8"))
    except (OSError, UnicodeError):
        raise ReminderError("Cannot read a regular UTF-8 JSON file; symlink files are rejected.") from None


def public(r: Reminder, tz: ZoneInfo) -> dict[str, Any]:
    local = lambda d: d.astimezone(tz).isoformat() if d else None
    return {"id": r.id, "list_id": r.list_id, "title": r.title, "due": local(r.due), "alarm": local(r.alarm),
            "completed": r.completed, "managed": bool(r.key), "expires": local(r.expires)}


class Ledger:
    """Private record of the reminders this tool created on this Mac.

    Only reminders recorded here are treated as this tool's own: markers in notes can be copied or typed by
    anyone who can edit the list, so they never authorize a delete on their own.
    """

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().absolute()

    def _check_ancestors(self) -> None:
        if ".." in self.path.parts:
            raise ReminderError("State paths may not contain parent traversal components.")
        for part in reversed(self.path.parents):
            try:
                info = part.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) and str(part) not in ("/tmp", "/var", "/etc"):
                raise ReminderError("State paths may not contain symlinks.")
            if info.st_uid not in (0, os.getuid()):
                raise ReminderError("State ancestors must be owned by you or root.")
            if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
                raise ReminderError("State ancestors may not be writable by other users.")

    def load(self) -> dict[str, dict[str, Any]]:
        self._check_ancestors()
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return {}
        except OSError:
            raise ReminderError("The state file cannot be opened; symlinks are rejected.") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                raise ReminderError("The state file must be a private (0600), unlinked regular file you own.")
            data = os.read(fd, 8 * 1024 * 1024)
        finally:
            os.close(fd)
        try:
            value = json.loads(data.decode("utf-8"))
            entries = value["reminders"]
            if value.get("version") != 1 or not isinstance(entries, dict):
                raise ValueError
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise ReminderError("The state file is corrupt; inspect it before retrying. It was not changed.") from None
        return entries

    def save(self, entries: dict[str, dict[str, Any]]) -> None:
        self._check_ancestors()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, json.dumps({"version": 1, "reminders": entries}, indent=2, sort_keys=True).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.path)

    @contextmanager
    def locked(self):
        self._check_ancestors()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path.with_name(self.path.name + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            deadline = time.monotonic() + LOCK_TIMEOUT
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise ReminderError("Another reminder-cli run holds the lock; no change.") from None
                    time.sleep(0.2)
            yield
        finally:
            os.close(fd)


def entry_key(selected: ReminderList, key: str) -> str:
    return f"{selected.id}|{key}"


class EventKitBackend:
    """Direct PyObjC bindings to EventKit's reminder store."""

    def __init__(self) -> None:
        try:
            import EventKit
            import Foundation
        except ImportError:
            raise ReminderError("PyObjC is missing. Install the pinned requirements in a macOS virtual environment.") from None
        self.ek, self.foundation = EventKit, Foundation
        self.date, self.ns_tz = Foundation.NSDate, Foundation.NSTimeZone
        self._store = None

    def status(self) -> dict[str, Any]:
        value = int(self.ek.EKEventStore.authorizationStatusForEntityType_(self.ek.EKEntityTypeReminder))
        names = {0: "not-determined", 1: "restricted", 2: "denied", 3: "full-access", 4: "write-only"}
        return {"authorization": names.get(value, "unknown"), "full_access": value == 3, "backend": "PyObjC/EventKit", "prompts": False}

    @property
    def store(self):
        if not self.status()["full_access"]:
            raise ReminderError("Reminders Full Access is not granted to this execution host. No permission request was made.")
        if self._store is None:
            self._store = self.ek.EKEventStore.alloc().init()
        return self._store

    def _list_record(self, c) -> ReminderList:
        return ReminderList(str(c.calendarIdentifier()), str(c.source().sourceIdentifier()), str(c.title()),
                            str(c.source().title()), bool(c.allowsContentModifications()))

    def lists(self) -> list[ReminderList]:
        return [self._list_record(c) for c in self.store.calendarsForEntityType_(self.ek.EKEntityTypeReminder)]

    def default_source(self) -> tuple[str, str, bool]:
        """Returns the default Reminders list's account as (id, title, is_local)."""
        default = self.store.defaultCalendarForNewReminders()
        if default is None:
            raise ReminderError("No default Reminders list; pass --source-id.")
        source = default.source()
        return str(source.sourceIdentifier()), str(source.title()), int(source.sourceType()) == int(self.ek.EKSourceTypeLocal)

    def create_list(self, name: str, source_id: str) -> ReminderList:
        source = next((s for s in self.store.sources() if str(s.sourceIdentifier()) == source_id), None)
        if source is None:
            raise ReminderError("Unknown source ID.")
        c = self.ek.EKCalendar.calendarForEntityType_eventStore_(self.ek.EKEntityTypeReminder, self.store)
        c.setTitle_(name)
        c.setSource_(source)
        ok, _ = self.store.saveCalendar_commit_error_(c, True, None)
        if not ok:
            raise ReminderError("EventKit could not create the list.")
        return self._list_record(c)

    def _list(self, selected: ReminderList):
        c = self.store.calendarWithIdentifier_(selected.id)
        if c is None or str(c.source().sourceIdentifier()) != selected.source_id:
            raise ReminderError("List identity changed; select it again before proceeding.")
        return c

    def _gregorian(self):
        return self.foundation.NSCalendar.calendarWithIdentifier_(self.foundation.NSCalendarIdentifierGregorian)

    def _date(self, d) -> datetime | None:
        return datetime.fromtimestamp(float(d.timeIntervalSince1970()), timezone.utc) if d is not None else None

    def _record(self, r) -> Reminder:
        notes = str(r.notes() or "")
        key, expires = read_markers(notes)
        due_components = r.dueDateComponents()
        due = self._date(self._gregorian().dateFromComponents_(due_components)) if due_components else None
        alarms = [a for a in (r.alarms() or []) if a.absoluteDate() is not None]
        modified = r.lastModifiedDate()
        revision = hashlib.sha256(json.dumps([notes, str(r.URL() or ""), int(r.priority()), bool(r.hasRecurrenceRules()),
                                              float(modified.timeIntervalSince1970()) if modified else None]).encode()).hexdigest()
        return Reminder(str(r.calendarItemIdentifier()), str(r.calendar().calendarIdentifier()), str(r.title() or ""), due,
                        bool(r.isCompleted()), key, expires, self._date(alarms[0].absoluteDate()) if alarms else None, revision)

    def reminders(self, selected: ReminderList) -> list[Reminder]:
        predicate = self.store.predicateForRemindersInCalendars_([self._list(selected)])
        done, found = threading.Event(), []

        def collect(items):
            found.extend(items or [])
            done.set()

        self.store.fetchRemindersMatchingPredicate_completion_(predicate, collect)
        if not done.wait(FETCH_TIMEOUT):
            raise ReminderError("Reminders fetch timed out.")
        return [self._record(r) for r in found]

    def get(self, selected: ReminderList, reminder_id: str) -> Reminder | None:
        r = self.store.calendarItemWithIdentifier_(reminder_id)
        if r is None or not isinstance(r, self.ek.EKReminder):
            return None
        if str(r.calendar().calendarIdentifier()) != selected.id:
            raise ReminderError("Reminder belongs to another list; no change.")
        return self._record(r)

    def create(self, selected: ReminderList, spec: dict[str, Any]) -> Reminder:
        c = self._list(selected)
        r = self.ek.EKReminder.reminderWithEventStore_(self.store)
        r.setCalendar_(c)
        r.setTitle_(spec["title"])
        r.setNotes_(notes_with_markers(spec["notes"], spec["key"], spec["expires"]))
        due = self.date.dateWithTimeIntervalSince1970_(spec["due"].timestamp())
        calendar = self._gregorian()
        calendar.setTimeZone_(self.ns_tz.timeZoneWithName_(spec["timezone"]))
        f = self.foundation
        units = f.NSCalendarUnitYear | f.NSCalendarUnitMonth | f.NSCalendarUnitDay | f.NSCalendarUnitHour | f.NSCalendarUnitMinute | f.NSCalendarUnitSecond
        components = calendar.components_fromDate_(units, due)
        components.setTimeZone_(self.ns_tz.timeZoneWithName_(spec["timezone"]))
        r.setDueDateComponents_(components)
        if spec["alert"]:
            r.addAlarm_(self.ek.EKAlarm.alarmWithAbsoluteDate_(due))
        ok, _ = self.store.saveReminder_commit_error_(r, True, None)
        if not ok:
            raise ReminderError("EventKit save failed; inspect the list before retrying.")
        return self._record(r)

    def delete(self, selected: ReminderList, expected: Reminder) -> None:
        r = self.store.calendarItemWithIdentifier_(expected.id)
        if r is None or not r.refresh() or self._record(r) != expected:
            raise ReminderError("Deletion target changed since it was read; no change.")
        ok, _ = self.store.removeReminder_commit_error_(r, True, None)
        if not ok:
            raise ReminderError("EventKit deletion failed; inspect the list before retrying.")


def select(lists: list[ReminderList], selector: dict[str, str | None]) -> ReminderList:
    for label, value in selector.items():
        if value is not None:
            text(value, label)
    if selector.get("list_id") or selector.get("source_id"):
        if not (selector.get("list_id") and selector.get("source_id")):
            raise ReminderError("Pass both --list-id and --source-id.")
        matches = [l for l in lists if l.id == selector["list_id"] and l.source_id == selector["source_id"]]
    elif selector.get("list") or selector.get("source"):
        if not (selector.get("list") and selector.get("source")):
            raise ReminderError("A list name must be paired with its account (--source).")
        matches = [l for l in lists if l.name == selector["list"] and l.source.casefold() == selector["source"].casefold()]
    else:
        raise ReminderError("A list must be explicitly selected.")
    if len(matches) != 1:
        raise ReminderError("List selection is missing or ambiguous; use lists to obtain exact IDs.")
    if not matches[0].writable:
        raise ReminderError("List is read-only.")
    return matches[0]


class Service:
    def __init__(self, backend, ledger: Ledger) -> None:
        self.backend, self.ledger = backend, ledger

    def ensure_list(self, name: str, source_id: str | None, execute: bool) -> dict[str, Any]:
        text(name, "list name", 200)
        if source_id:
            source_title = next((l.source for l in self.backend.lists() if l.source_id == source_id), "")
        else:
            source_id, source_title, is_local = self.backend.default_source()
            if is_local:
                raise ReminderError("The default Reminders account is local to this Mac and does not sync; pass --source-id.")
        matches = [l for l in self.backend.lists() if l.name == name and l.source_id == source_id]
        if len(matches) > 1:
            raise ReminderError("Several lists share that name in the account; no change.")
        if matches:
            if not matches[0].writable:
                raise ReminderError("A list with that name exists but is read-only; no change.")
            return {"operation": "exists", "dry_run": not execute, "list": asdict(matches[0])}
        if not execute:
            return {"operation": "create", "dry_run": True, "list": {"name": name, "source_id": source_id, "source": source_title}}
        return {"operation": "create", "dry_run": False, "list": asdict(self.backend.create_list(name, source_id))}

    def create(self, selected: ReminderList, spec: dict[str, Any], execute: bool) -> dict[str, Any]:
        tz = zone(spec["timezone"])
        planned = {"title": spec["title"], "due": spec["due"].isoformat(), "expires": spec["expires"].isoformat() if spec["expires"] else None}

        def decide(entries):
            marked = [r for r in self.backend.reminders(selected) if r.key == spec["key"]]
            known = entries.get(entry_key(selected, spec["key"]))
            if known:
                mine = [r for r in marked if r.id == known["id"]]
                if not mine:
                    raise ReminderError("The reminder created for this key is missing or moved; no change.")
                if mine[0].title != spec["title"]:
                    raise ReminderError("The reminder created for this key was changed; no change.")
                return "exists", mine[0]
            if marked:
                raise ReminderError("A reminder carrying this key exists but was not created by this tool here; no change.")
            return "create", None

        if not execute:
            op, found = decide(self.ledger.load())
            return {"operation": op, "dry_run": True, "list": asdict(selected), "reminder": public(found, tz) if found else {}, "planned": planned}
        with self.ledger.locked():
            entries = self.ledger.load()
            op, found = decide(entries)
            if op == "create":
                found = self.backend.create(selected, spec)
                entries[entry_key(selected, spec["key"])] = {"id": found.id, "expires": spec["expires"].astimezone(timezone.utc).isoformat() if spec["expires"] else None}
                self.ledger.save(entries)
        return {"operation": op, "dry_run": False, "list": asdict(selected), "reminder": public(found, tz), "planned": planned}

    def delete(self, selected: ReminderList, reminder_id: str, key: str, execute: bool) -> dict[str, Any]:
        def target(entries):
            known = entries.get(entry_key(selected, key))
            if not known or known["id"] != reminder_id:
                raise ReminderError("Only a reminder this tool created with this exact key can be deleted.")
            r = self.backend.get(selected, reminder_id)
            if r is not None and r.key != key:
                raise ReminderError("The reminder no longer carries this key; no change.")
            return r

        if not execute:
            r = target(self.ledger.load())
            return {"operation": "delete" if r else "absent", "dry_run": True}
        with self.ledger.locked():
            entries = self.ledger.load()
            r = target(entries)
            if r is not None:
                self.backend.delete(selected, r)
            del entries[entry_key(selected, key)]
            self.ledger.save(entries)
        return {"operation": "delete" if r else "absent", "dry_run": False}

    def prune(self, selected: ReminderList, now: datetime, execute: bool) -> dict[str, Any]:
        """Deletes this tool's reminders whose recorded expiry has passed. Expiry comes from the ledger, not notes."""
        prefix = f"{selected.id}|"

        def expired(entries):
            return sorted((k, v) for k, v in entries.items()
                          if k.startswith(prefix) and v.get("expires") and datetime.fromisoformat(v["expires"]) <= now)

        if not execute:
            return {"operation": "prune", "dry_run": True, "would_delete": [v["id"] for _, v in expired(self.ledger.load())]}
        deleted, skipped, forgotten = [], [], []
        with self.ledger.locked():
            entries = self.ledger.load()
            for k, v in expired(entries):
                key = k[len(prefix):]
                try:
                    r = self.backend.get(selected, v["id"])
                    if r is None:
                        forgotten.append(v["id"])
                    elif r.key != key:
                        skipped.append(v["id"])
                        continue
                    else:
                        self.backend.delete(selected, r)
                        deleted.append(v["id"])
                except ReminderError:
                    skipped.append(v["id"])
                    continue
                del entries[k]
                self.ledger.save(entries)
        return {"operation": "prune", "dry_run": False, "deleted": deleted, "skipped": skipped, "forgotten": forgotten}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, help="Local JSON defaults (list_id, source_id, timezone) outside the repository")
    p.add_argument("--state", type=Path, default=DEFAULT_STATE, help="Private ledger of reminders this tool created; never written by previews")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Existing Reminders permission status; no prompts")
    lists = sub.add_parser("lists", help="Reminder list metadata only")
    lists.add_argument("--name")
    ensure = sub.add_parser("ensure-list", help="Find or create a reminder list by exact name")
    ensure.add_argument("--name", required=True)
    ensure.add_argument("--source-id", help="Account to create the list in; default is the default Reminders list's account")
    ensure.add_argument("--execute", action="store_true")
    for name in ("reminders", "create", "delete", "prune"):
        s = sub.add_parser(name)
        s.add_argument("--list-id")
        s.add_argument("--source-id")
        s.add_argument("--list", help="Exact list name; requires --source")
        s.add_argument("--source", help="Account name of --list")
        s.add_argument("--timezone", default=None, help="IANA timezone for naive times and output; default UTC")
        if name == "create":
            s.add_argument("--input", type=Path, help="JSON input file; omitted reads stdin")
        if name == "delete":
            s.add_argument("--id", required=True)
            s.add_argument("--key", required=True)
        if name == "prune":
            s.add_argument("--now", help="Treat this ISO time as now (default: the current time)")
        if name != "reminders":
            s.add_argument("--execute", action="store_true", help="Commit the operation; omitted means preview only")
    return p


def run(args, backend=None) -> dict[str, Any]:
    backend = backend or EventKitBackend()
    if args.command == "status":
        return backend.status()
    settings: dict[str, Any] = {}
    if args.config:
        settings = input_object(args.config)
        if set(settings) - {"list_id", "source_id", "list", "source", "timezone"}:
            raise ReminderError("Unknown local configuration fields.")
    if args.command == "lists":
        return {"lists": [asdict(l) for l in backend.lists() if not args.name or l.name == args.name]}
    service = Service(backend, Ledger(args.state))
    if args.command == "ensure-list":
        return service.ensure_list(args.name, args.source_id, args.execute)

    selector = {k: getattr(args, k, None) or settings.get(k) for k in ("list_id", "source_id", "list", "source")}
    if args.list_id or args.source_id:
        selector["list"] = selector["source"] = None
    elif args.list or args.source:
        selector["list_id"] = selector["source_id"] = None
    selected = select(backend.lists(), selector)
    timezone_name = args.timezone or settings.get("timezone", "UTC")
    tz = zone(timezone_name)

    if args.command == "reminders":
        items = sorted(backend.reminders(selected), key=lambda r: (r.due or datetime.max.replace(tzinfo=timezone.utc), r.id))
        return {"list": asdict(selected), "reminders": [public(r, tz) for r in items[:1000]], "truncated": len(items) > 1000}
    if args.command == "create":
        return service.create(selected, spec_from_json(input_object(args.input), timezone_name), args.execute)
    if args.command == "delete":
        return service.delete(selected, text(args.id, "reminder ID"), text(args.key, "management key", 200), args.execute)
    now = parse_time(args.now, tz) if args.now else datetime.now(timezone.utc)
    return service.prune(selected, now, args.execute)


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        result = run(args)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        # A prune that skipped items still reports what it deleted, but must not look like a clean run.
        return 1 if result.get("skipped") else 0
    except ReminderError as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=True), file=sys.stderr)
        return 2
    except OSError:
        print(json.dumps({"error": "Local file operation failed."}), file=sys.stderr)
        return 2
    except Exception:
        # No provider exception repr/traceback: these can contain reminder text and account names.
        print(json.dumps({"error": "Operation failed. No provider details were logged; inspect the list before retrying a write."}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
