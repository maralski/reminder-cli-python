import io
import json
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest import mock

import reminder_cli as rc

MELBOURNE = rc.zone("Australia/Melbourne")
HOME = rc.ReminderList("L1", "S1", "Fitness", "iCloud", True)
OTHER = rc.ReminderList("L2", "S1", "Groceries", "iCloud", True)


class FakeBackend:
    """Stores reminders in memory, applying the same marker convention as EventKitBackend."""

    def __init__(self, lists=(HOME, OTHER)):
        self._lists = list(lists)
        self.items: dict[str, rc.Reminder] = {}
        self.writes = 0
        self.next = 1

    def status(self):
        return {"authorization": "full-access", "full_access": True, "backend": "fake", "prompts": False}

    def lists(self):
        return list(self._lists)

    def default_source_id(self):
        return "S1"

    def create_list(self, name, source_id):
        self.writes += 1
        new = rc.ReminderList(f"L{len(self._lists) + 1}", source_id, name, "iCloud", True)
        self._lists.append(new)
        return new

    def reminders(self, selected):
        return [r for r in self.items.values() if r.list_id == selected.id]

    def get(self, selected, reminder_id):
        r = self.items.get(reminder_id)
        if r and r.list_id != selected.id:
            raise rc.ReminderError("Reminder belongs to another list; no change.")
        return r

    def create(self, selected, spec):
        self.writes += 1
        key, expires = rc.read_markers(rc.notes_with_markers(spec["notes"], spec["key"], spec["expires"]))
        r = rc.Reminder(f"R{self.next}", selected.id, spec["title"], spec["due"].astimezone(timezone.utc), False, key, expires,
                        spec["due"].astimezone(timezone.utc) if spec["alert"] else None)
        self.next += 1
        self.items[r.id] = r
        return r

    def delete(self, selected, expected):
        if self.items.get(expected.id) != expected:
            raise rc.ReminderError("Deletion target changed since it was read; no change.")
        self.writes += 1
        del self.items[expected.id]

    def add_unmanaged(self, title, due, list_id="L1"):
        r = rc.Reminder(f"U{self.next}", list_id, title, due, False)
        self.next += 1
        self.items[r.id] = r
        return r


def spec(**overrides):
    value = {"title": "Booked: Spin class", "due": "2026-10-21T13:02:00+11:00", "key": "example:a1:1001:booked",
             "expires": "2026-10-27T18:00:00+11:00", "notes": "Cancel 90 minutes before."}
    value.update(overrides)
    value = {k: v for k, v in value.items() if v is not None}
    return rc.spec_from_json(value, "Australia/Melbourne")


class SpecTests(unittest.TestCase):
    def test_naive_times_use_the_selected_zone(self):
        s = rc.spec_from_json({"title": "t", "due": "2026-10-27T18:02:00", "key": "k"}, "Australia/Melbourne")
        self.assertEqual(s["due"].utcoffset(), timedelta(hours=11))
        self.assertTrue(s["alert"])
        self.assertIsNone(s["expires"])

    def test_rejects_unknown_fields_reserved_markers_and_bad_values(self):
        cases = [
            {"title": "t", "due": "2026-10-27T18:00:00+11:00", "key": "k", "url": "x"},
            {"title": "t", "due": "2026-10-27T18:00:00+11:00", "key": "k", "notes": "Reminder-CLI-Key: other"},
            {"title": "t", "due": "not a time", "key": "k"},
            {"title": "", "due": "2026-10-27T18:00:00+11:00", "key": "k"},
            {"title": "t", "due": "2026-10-27T18:00:00+11:00", "key": "k", "alert": "yes"},
            {"title": "t", "due": "2026-10-27T18:00:00+11:00", "key": "k", "expires": "2026-10-27T17:00:00+11:00"},
            {"title": "t\nsecond line", "due": "2026-10-27T18:00:00+11:00", "key": "k"},
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(rc.ReminderError):
                rc.spec_from_json(value, "Australia/Melbourne")

    def test_markers_round_trip_through_notes(self):
        expires = datetime(2026, 10, 27, 7, 0, tzinfo=timezone.utc)
        notes = rc.notes_with_markers("Hello", "k1", expires)
        self.assertEqual(notes.splitlines()[0], "Hello")
        self.assertEqual(rc.read_markers(notes), ("k1", expires))
        self.assertEqual(rc.read_markers("just my own note"), ("", None))


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.service = rc.Service(self.backend)

    def test_create_is_a_preview_until_executed(self):
        out = self.service.create(HOME, spec(), execute=False)
        self.assertEqual((out["operation"], out["dry_run"]), ("create", True))
        self.assertEqual(self.backend.writes, 0)

    def test_create_twice_with_one_key_makes_one_reminder(self):
        first = self.service.create(HOME, spec(), execute=True)
        second = self.service.create(HOME, spec(), execute=True)
        self.assertEqual(first["operation"], "create")
        self.assertEqual(second["operation"], "exists")
        self.assertEqual(second["reminder"]["id"], first["reminder"]["id"])
        self.assertEqual(len(self.backend.items), 1)
        self.assertEqual(first["reminder"]["alarm"], "2026-10-21T13:02:00+11:00")

    def test_a_changed_reminder_with_the_key_is_not_overwritten(self):
        made = self.service.create(HOME, spec(), execute=True)["reminder"]
        self.backend.items[made["id"]] = replace(self.backend.items[made["id"]], title="Edited by hand")
        with self.assertRaises(rc.ReminderError):
            self.service.create(HOME, spec(), execute=True)

    def test_delete_requires_the_exact_key_and_never_touches_unmarked_reminders(self):
        made = self.service.create(HOME, spec(), execute=True)["reminder"]
        mine = self.backend.add_unmanaged("Buy milk", None)
        with self.assertRaises(rc.ReminderError):
            self.service.delete(HOME, made["id"], "some-other-key", execute=True)
        with self.assertRaises(rc.ReminderError):
            self.service.delete(HOME, mine.id, "", execute=True)
        self.assertEqual(self.service.delete(HOME, made["id"], "example:a1:1001:booked", execute=False)["dry_run"], True)
        self.assertIn(made["id"], self.backend.items)
        self.assertEqual(self.service.delete(HOME, made["id"], "example:a1:1001:booked", execute=True)["operation"], "delete")
        self.assertEqual(self.service.delete(HOME, made["id"], "example:a1:1001:booked", execute=True)["operation"], "absent")
        self.assertIn(mine.id, self.backend.items)

    def test_prune_removes_only_expired_managed_reminders(self):
        expired = self.service.create(HOME, spec(), execute=True)["reminder"]
        later = self.service.create(HOME, spec(key="k-later", due="2026-10-23T13:02:00+11:00", expires="2026-10-29T18:00:00+11:00"), execute=True)["reminder"]
        no_expiry = self.service.create(HOME, spec(key="k-forever", expires=None), execute=True)["reminder"]
        mine = self.backend.add_unmanaged("My own reminder", datetime(2020, 1, 1, tzinfo=timezone.utc))
        now = datetime(2026, 10, 28, tzinfo=MELBOURNE)

        preview = self.service.prune(HOME, now, execute=False)
        self.assertEqual(preview["would_delete"], [expired["id"]])
        self.assertIn(expired["id"], self.backend.items)

        done = self.service.prune(HOME, now, execute=True)
        self.assertEqual(done["deleted"], [expired["id"]])
        self.assertEqual(set(self.backend.items), {later["id"], no_expiry["id"], mine.id})
        self.assertEqual(self.service.prune(HOME, now, execute=True)["deleted"], [])

    def test_prune_and_create_stay_inside_the_selected_list(self):
        self.service.create(OTHER, spec(), execute=True)
        self.assertEqual(self.service.prune(HOME, datetime(2030, 1, 1, tzinfo=timezone.utc), execute=True)["deleted"], [])
        self.assertEqual(self.service.create(HOME, spec(), execute=True)["operation"], "create")

    def test_ensure_list_creates_once_then_finds_it(self):
        self.assertEqual(self.service.ensure_list("Gym", None, execute=False)["operation"], "create")
        self.assertEqual(self.backend.writes, 0)
        made = self.service.ensure_list("Gym", None, execute=True)
        again = self.service.ensure_list("Gym", None, execute=True)
        self.assertEqual(made["operation"], "create")
        self.assertEqual(again["operation"], "exists")
        self.assertEqual(again["list"]["id"], made["list"]["id"])

    def test_ensure_list_refuses_duplicates(self):
        backend = FakeBackend(lists=(HOME, replace(HOME, id="L9")))
        with self.assertRaises(rc.ReminderError):
            rc.Service(backend).ensure_list("Fitness", None, execute=True)


class SelectionAndCliTests(unittest.TestCase):
    def test_selection_needs_one_writable_list(self):
        self.assertEqual(rc.select([HOME, OTHER], {"list": "Fitness"}), HOME)
        self.assertEqual(rc.select([HOME, OTHER], {"list_id": "L2", "source_id": "S1"}), OTHER)
        for selector in ({}, {"list": "Nope"}, {"list_id": "L1"}):
            with self.subTest(selector=selector), self.assertRaises(rc.ReminderError):
                rc.select([HOME, OTHER], selector)
        with self.assertRaises(rc.ReminderError):
            rc.select([replace(HOME, writable=False)], {"list": "Fitness"})

    def test_cli_create_reads_stdin_and_reports_json(self):
        backend = FakeBackend()
        args = rc.parser().parse_args(["create", "--list", "Fitness", "--timezone", "Australia/Melbourne", "--execute"])
        body = json.dumps({"title": "Booked: Spin class", "due": "2026-10-27T18:02:00+11:00", "key": "k1"}).encode()
        with mock.patch("sys.stdin", io.TextIOWrapper(io.BytesIO(body))):
            out = rc.run(args, backend)
        self.assertEqual(out["operation"], "create")
        self.assertEqual(out["reminder"]["title"], "Booked: Spin class")

    def test_errors_are_json_on_stderr_without_details(self):
        stderr, stdout = io.StringIO(), io.StringIO()
        with mock.patch.object(rc, "EventKitBackend", side_effect=RuntimeError("secret reminder text")), redirect_stderr(stderr), redirect_stdout(stdout):
            code = rc.main(["status"])
        self.assertEqual(code, 2)
        self.assertNotIn("secret", stderr.getvalue())
        self.assertIn("error", json.loads(stderr.getvalue()))


if __name__ == "__main__":
    unittest.main()
