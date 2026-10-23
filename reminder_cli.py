#!/usr/bin/env python3
"""An on-demand macOS EventKit CLI for Apple Reminders. Writes require --execute; no permission prompts."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import threading
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

KEY_MARKER = "Reminder-CLI-Key: "
EXPIRES_MARKER = "Reminder-CLI-Expires: "
MAX_INPUT = 65536
FETCH_TIMEOUT = 30


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


def text(value: Any, label: str, limit: int = 512) -> str:
    if not isinstance(value, str) or len(value) > limit or not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
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
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ReminderError("Timestamps must be ISO 8601.") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
        if parsed.utcoffset() != parsed.replace(fold=1).utcoffset():
            raise ReminderError("Ambiguous local time; add an explicit UTC offset.")
        if parsed.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) != parsed.replace(tzinfo=None):
            raise ReminderError("Nonexistent local time.")
    return parsed.astimezone(tz)


def notes_with_markers(notes: str, key: str, expires: datetime | None) -> str:
    lines = [notes] if notes else []
    lines.append(KEY_MARKER + key)
    if expires is not None:
        lines.append(EXPIRES_MARKER + expires.astimezone(timezone.utc).isoformat())
    return "\n".join(lines)


def read_markers(notes: str) -> tuple[str, datetime | None]:
    key, expires = "", None
    for line in notes.splitlines():
        if line.startswith(KEY_MARKER):
            key = line[len(KEY_MARKER):]
        elif line.startswith(EXPIRES_MARKER):
            try:
                expires = datetime.fromisoformat(line[len(EXPIRES_MARKER):])
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


def input_object(path: Path | None) -> Any:
    raw = path.read_bytes()[: MAX_INPUT + 1] if path else sys.stdin.buffer.read(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT:
        raise ReminderError("Input is larger than 64 KiB.")
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ReminderError("Input must be valid UTF-8 JSON.") from None


def public(r: Reminder, tz: ZoneInfo) -> dict[str, Any]:
    local = lambda d: d.astimezone(tz).isoformat() if d else None
    return {"id": r.id, "list_id": r.list_id, "title": r.title, "due": local(r.due), "alarm": local(r.alarm),
            "completed": r.completed, "managed": bool(r.key), "expires": local(r.expires)}


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

    def default_source_id(self) -> str:
        default = self.store.defaultCalendarForNewReminders()
        if default is None:
            raise ReminderError("No default Reminders list; pass --source-id.")
        return str(default.source().sourceIdentifier())

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
        key, expires = read_markers(str(r.notes() or ""))
        due_components = r.dueDateComponents()
        due = self._date(self._gregorian().dateFromComponents_(due_components)) if due_components else None
        alarms = [a for a in (r.alarms() or []) if a.absoluteDate() is not None]
        return Reminder(str(r.calendarItemIdentifier()), str(r.calendar().calendarIdentifier()), str(r.title() or ""), due,
                        bool(r.isCompleted()), key, expires, self._date(alarms[0].absoluteDate()) if alarms else None)

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
    if selector.get("list_id") or selector.get("source_id"):
        if not (selector.get("list_id") and selector.get("source_id")):
            raise ReminderError("Pass both --list-id and --source-id.")
        matches = [l for l in lists if l.id == selector["list_id"] and l.source_id == selector["source_id"]]
    elif selector.get("list"):
        matches = [l for l in lists if l.name == selector["list"] and (not selector.get("source") or l.source.casefold() == selector["source"].casefold())]
    else:
        raise ReminderError("A list must be explicitly selected.")
    if len(matches) != 1:
        raise ReminderError("List selection is missing or ambiguous; use lists to obtain exact IDs.")
    if not matches[0].writable:
        raise ReminderError("List is read-only.")
    return matches[0]


class Service:
    def __init__(self, backend) -> None:
        self.backend = backend

    def ensure_list(self, name: str, source_id: str | None, execute: bool) -> dict[str, Any]:
        text(name, "list name", 200)
        source_id = source_id or self.backend.default_source_id()
        matches = [l for l in self.backend.lists() if l.name == name and l.source_id == source_id]
        if len(matches) > 1:
            raise ReminderError("Several lists share that name in the source; no change.")
        if matches:
            return {"operation": "exists", "dry_run": not execute, "list": asdict(matches[0])}
        if not execute:
            return {"operation": "create", "dry_run": True, "list": {"name": name, "source_id": source_id}}
        return {"operation": "create", "dry_run": False, "list": asdict(self.backend.create_list(name, source_id))}

    def create(self, selected: ReminderList, spec: dict[str, Any], execute: bool) -> dict[str, Any]:
        tz = zone(spec["timezone"])
        marked = [r for r in self.backend.reminders(selected) if r.key == spec["key"]]
        if len(marked) > 1:
            raise ReminderError("Several reminders carry this key; no change.")
        planned = {"title": spec["title"], "due": spec["due"].isoformat(), "expires": spec["expires"].isoformat() if spec["expires"] else None}
        if marked:
            r = marked[0]
            if r.title != spec["title"] or r.due is None or abs((r.due - spec["due"]).total_seconds()) >= 60:
                raise ReminderError("A reminder with this key exists but was changed; no change.")
            return {"operation": "exists", "dry_run": not execute, "list": asdict(selected), "reminder": public(r, tz), "planned": planned}
        if not execute:
            return {"operation": "create", "dry_run": True, "list": asdict(selected), "reminder": {}, "planned": planned}
        return {"operation": "create", "dry_run": False, "list": asdict(selected), "reminder": public(self.backend.create(selected, spec), tz), "planned": planned}

    def delete(self, selected: ReminderList, reminder_id: str, key: str, execute: bool) -> dict[str, Any]:
        r = self.backend.get(selected, reminder_id)
        if r is None:
            return {"operation": "absent", "dry_run": not execute}
        if not r.key or r.key != key:
            raise ReminderError("Only a reminder carrying this exact management key can be deleted.")
        if execute:
            self.backend.delete(selected, r)
        return {"operation": "delete", "dry_run": not execute, "id": r.id}

    def prune(self, selected: ReminderList, now: datetime, execute: bool) -> dict[str, Any]:
        expired = [r for r in self.backend.reminders(selected) if r.key and r.expires is not None and r.expires <= now]
        if execute:
            for r in expired:
                self.backend.delete(selected, r)
        return {"operation": "prune", "dry_run": not execute, "deleted" if execute else "would_delete": [r.id for r in expired]}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, help="Local JSON defaults (list_id, source_id, timezone) outside the repository")
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
        s.add_argument("--list", help="Exact list name")
        s.add_argument("--source", help="Account name, to disambiguate --list")
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
        if not isinstance(settings, dict) or set(settings) - {"list_id", "source_id", "list", "source", "timezone"}:
            raise ReminderError("Unknown local configuration fields.")
    if args.command == "lists":
        return {"lists": [asdict(l) for l in backend.lists() if not args.name or l.name == args.name]}
    service = Service(backend)
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
        print(json.dumps(run(args), ensure_ascii=False, indent=2))
        return 0
    except ReminderError as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
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
