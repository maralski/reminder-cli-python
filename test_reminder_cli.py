"""Offline tests. FakeBackend never imports EventKit or touches real reminders."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest import mock

import reminder_cli as rc

MELBOURNE = rc.zone("Australia/Melbourne")
HOME = rc.ReminderList("L1", "S1", "Fitness", "iCloud", True)
OTHER = rc.ReminderList("L2", "S1", "Groceries", "iCloud", True)
KEY = "example:a1:1001:booked"


class FakeBackend:
    """Stores reminders in memory, applying the same marker convention as EventKitBackend."""

    def __init__(self, lists=(HOME, OTHER), local_default=False):
        self._lists = list(lists)
        self.items: dict[str, rc.Reminder] = {}
        self.writes = 0
        self.next = 1
        self.local_default = local_default
        self.fail_delete: set[str] = set()

    def status(self):
        return {"authorization": "full-access", "full_access": True, "backend": "fake", "prompts": False}

    def lists(self):
        return list(self._lists)

    def default_source(self):
        return ("S1", "On My Mac" if self.local_default else "iCloud", self.local_default)

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
        due = spec["due"].astimezone(timezone.utc)
        r = rc.Reminder(f"R{self.next}", selected.id, spec["title"], due, False, key, expires, due if spec["alert"] else None)
        self.next += 1
        self.items[r.id] = r
        return r

    def delete(self, selected, expected):
        if expected.id in self.fail_delete or self.items.get(expected.id) != expected:
            raise rc.ReminderError("Deletion target changed since it was read; no change.")
        self.writes += 1
        del self.items[expected.id]

    def add(self, title, key="", expires=None, list_id="L1"):
        """A reminder this tool did not create, e.g. one typed or copied by a person."""
        r = rc.Reminder(f"U{self.next}", list_id, title, None, False, key, expires)
        self.next += 1
        self.items[r.id] = r
        return r


def spec(**overrides):
    value = {"title": "Booked: Spin class", "due": "2026-10-21T13:02:00+11:00", "key": KEY,
             "expires": "2026-10-27T18:00:00+11:00", "notes": "Cancel 90 minutes before."}
    value.update(overrides)
    return rc.spec_from_json({k: v for k, v in value.items() if v is not None}, "Australia/Melbourne")


class TempState(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=Path.home() / "Library/Caches" if (Path.home() / "Library/Caches").is_dir() else None))
        os.chmod(self.dir, 0o700)
        self.ledger = rc.Ledger(self.dir / "state" / "state.json")
        self.backend = FakeBackend()
        self.service = rc.Service(self.backend, self.ledger)

    def tearDown(self):
        for root, dirs, files in os.walk(self.dir, topdown=False):
            for f in files:
                os.unlink(os.path.join(root, f))
            for d in dirs:
                os.rmdir(os.path.join(root, d))
        os.rmdir(self.dir)


class SpecTests(unittest.TestCase):
    def test_naive_times_use_the_selected_zone_and_z_suffix_is_utc(self):
        s = rc.spec_from_json({"title": "t", "due": "2026-10-27T18:02:00", "key": "k"}, "Australia/Melbourne")
        self.assertEqual(s["due"].utcoffset(), timedelta(hours=11))
        z = rc.spec_from_json({"title": "t", "due": "2026-10-27T07:02:00.000Z", "key": "k"}, "Australia/Melbourne")
        self.assertEqual(z["due"], s["due"])

    def test_rejects_unknown_fields_reserved_markers_and_bad_values(self):
        cases = [
            {"title": "t", "due": "2026-10-27T18:00:00+11:00", "key": "k", "url": "x"},
            {"title": "t", "due": "2026-10-27T18:00:00+11:00", "key": "k", "notes": "Reminder-CLI-Key: other"},
            {"title": "t", "due": "not a time", "key": "k"},
            {"title": "", "due": "2026-10-27T18:00:00+11:00", "key": "k"},
            {"title": "t", "due": "2026-10-27T18:00:00+11:00", "key": "k", "alert": "yes"},
            {"title": "t", "due": "2026-10-27T18:00:00+11:00", "key": "k", "expires": "2026-10-27T17:00:00+11:00"},
            {"title": "t\nsecond line", "due": "2026-10-27T18:00:00+11:00", "key": "k"},
            {"title": "t", "due": "2026-10-27T18:00:00+11:00", "key": "k Reminder-CLI-Expires: 2000-01-01T00:00:00+00:00"},
            {"title": "t", "due": "2026-10-27T18:00:00+11:00", "key": "k\x85x"},
            {"title": "t", "due": "2026-04-05T02:30:00", "key": "k"},
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(rc.ReminderError):
                rc.spec_from_json(value, "Australia/Melbourne")

    def test_markers_round_trip_and_a_hand_typed_naive_expiry_is_ignored(self):
        expires = datetime(2026, 10, 27, 7, 0, tzinfo=timezone.utc)
        notes = rc.notes_with_markers("Hello", "k1", expires)
        self.assertEqual(notes.splitlines()[0], "Hello")
        self.assertEqual(rc.read_markers(notes), ("k1", expires))
        self.assertEqual(rc.read_markers("Reminder-CLI-Key: k\nReminder-CLI-Expires: 2000-01-01"), ("k", None))

    def test_json_rejects_duplicate_keys_deep_nesting_and_nonfinite_numbers(self):
        for raw in ('{"key": "a", "key": "b"}', "[" * 40 + "]" * 40, '{"a": NaN}', '{"a": ' * 20 + "1" + "}" * 20):
            with self.subTest(raw=raw[:30]), self.assertRaises(rc.ReminderError):
                rc.json_object(raw)


class ServiceTests(TempState):
    def test_create_is_a_preview_until_executed_and_previews_write_no_state(self):
        out = self.service.create(HOME, spec(), execute=False)
        self.assertEqual((out["operation"], out["dry_run"]), ("create", True))
        self.assertEqual(self.backend.writes, 0)
        self.assertFalse(self.ledger.path.exists())

    def test_create_twice_with_one_key_makes_one_reminder_even_if_the_due_time_moves(self):
        first = self.service.create(HOME, spec(), execute=True)
        second = self.service.create(HOME, spec(due="2026-10-21T14:10:00+11:00"), execute=True)
        self.assertEqual((first["operation"], second["operation"]), ("create", "exists"))
        self.assertEqual(second["reminder"]["id"], first["reminder"]["id"])
        self.assertEqual(len(self.backend.items), 1)
        self.assertEqual(first["reminder"]["alarm"], "2026-10-21T13:02:00+11:00")
        self.assertEqual(oct(self.ledger.path.stat().st_mode & 0o777), "0o600")

    def test_a_planted_reminder_with_the_key_blocks_rather_than_suppresses(self):
        self.backend.add("Booked: Spin class", key=KEY)
        with self.assertRaises(rc.ReminderError):
            self.service.create(HOME, spec(), execute=True)
        self.assertEqual(self.backend.writes, 0)

    def test_a_changed_or_missing_created_reminder_is_not_overwritten_or_recreated(self):
        made = self.service.create(HOME, spec(), execute=True)["reminder"]
        self.backend.items[made["id"]] = replace(self.backend.items[made["id"]], title="Edited by hand")
        with self.assertRaises(rc.ReminderError):
            self.service.create(HOME, spec(), execute=True)
        del self.backend.items[made["id"]]
        with self.assertRaises(rc.ReminderError):
            self.service.create(HOME, spec(), execute=True)

    def test_delete_only_reaches_a_reminder_this_tool_recorded(self):
        made = self.service.create(HOME, spec(), execute=True)["reminder"]
        planted = self.backend.add("Copied reminder", key=KEY)
        mine = self.backend.add("Buy milk")
        for reminder_id, key in ((made["id"], "other-key"), (planted.id, KEY), (mine.id, "")):
            with self.subTest(reminder_id=reminder_id), self.assertRaises(rc.ReminderError):
                self.service.delete(HOME, reminder_id, key or "x", execute=True)
        self.assertEqual(self.service.delete(HOME, made["id"], KEY, execute=False)["dry_run"], True)
        self.assertIn(made["id"], self.backend.items)
        self.assertEqual(self.service.delete(HOME, made["id"], KEY, execute=True)["operation"], "delete")
        self.assertEqual(set(self.backend.items), {planted.id, mine.id})

    def test_prune_deletes_only_recorded_reminders_past_their_recorded_expiry(self):
        expired = self.service.create(HOME, spec(), execute=True)["reminder"]
        later = self.service.create(HOME, spec(key="k-later", expires="2026-10-29T18:00:00+11:00"), execute=True)["reminder"]
        forever = self.service.create(HOME, spec(key="k-forever", expires=None), execute=True)["reminder"]
        spoofed = self.backend.add("My own reminder", key="anything", expires=datetime(2000, 1, 1, tzinfo=timezone.utc))
        copied = self.backend.add("Booked: Spin class", key=KEY, expires=datetime(2000, 1, 1, tzinfo=timezone.utc))
        now = datetime(2026, 10, 28, tzinfo=MELBOURNE)

        self.assertEqual(self.service.prune(HOME, now, execute=False)["would_delete"], [expired["id"]])
        self.assertIn(expired["id"], self.backend.items)

        done = self.service.prune(HOME, now, execute=True)
        self.assertEqual((done["deleted"], done["skipped"]), ([expired["id"]], []))
        self.assertEqual(set(self.backend.items), {later["id"], forever["id"], spoofed.id, copied.id})
        self.assertEqual(self.service.prune(HOME, now, execute=True)["deleted"], [])

    def test_prune_reports_partial_failure_and_keeps_the_failed_entry(self):
        a = self.service.create(HOME, spec(key="a"), execute=True)["reminder"]
        b = self.service.create(HOME, spec(key="b"), execute=True)["reminder"]
        self.backend.fail_delete.add(a["id"])
        out = self.service.prune(HOME, datetime(2030, 1, 1, tzinfo=timezone.utc), execute=True)
        self.assertEqual((out["deleted"], out["skipped"]), ([b["id"]], [a["id"]]))
        self.backend.fail_delete.clear()
        self.assertEqual(self.service.prune(HOME, datetime(2030, 1, 1, tzinfo=timezone.utc), execute=True)["deleted"], [a["id"]])

    def test_prune_forgets_reminders_that_were_already_deleted_by_hand(self):
        made = self.service.create(HOME, spec(), execute=True)["reminder"]
        del self.backend.items[made["id"]]
        out = self.service.prune(HOME, datetime(2030, 1, 1, tzinfo=timezone.utc), execute=True)
        self.assertEqual(out["forgotten"], [made["id"]])
        self.assertEqual(self.ledger.load(), {})

    def test_prune_and_create_stay_inside_the_selected_list(self):
        self.service.create(OTHER, spec(), execute=True)
        self.assertEqual(self.service.prune(HOME, datetime(2030, 1, 1, tzinfo=timezone.utc), execute=True)["deleted"], [])
        self.assertEqual(self.service.create(HOME, spec(), execute=True)["operation"], "create")

    def test_ensure_list_creates_once_then_finds_it(self):
        self.assertEqual(self.service.ensure_list("Gym", None, execute=False)["list"]["source"], "iCloud")
        self.assertEqual(self.backend.writes, 0)
        made = self.service.ensure_list("Gym", None, execute=True)
        again = self.service.ensure_list("Gym", None, execute=True)
        self.assertEqual((made["operation"], again["operation"]), ("create", "exists"))
        self.assertEqual(again["list"]["id"], made["list"]["id"])

    def test_ensure_list_refuses_duplicates_read_only_matches_and_local_default_accounts(self):
        for backend in (FakeBackend(lists=(HOME, replace(HOME, id="L9"))), FakeBackend(lists=(replace(HOME, writable=False),)),
                        FakeBackend(local_default=True)):
            with self.subTest(), self.assertRaises(rc.ReminderError):
                rc.Service(backend, self.ledger).ensure_list("Fitness", None, execute=True)

    def test_state_file_must_be_private_and_not_a_symlink(self):
        self.service.create(HOME, spec(), execute=True)
        os.chmod(self.ledger.path, 0o644)
        with self.assertRaises(rc.ReminderError):
            self.ledger.load()
        os.chmod(self.ledger.path, 0o600)
        link = rc.Ledger(self.dir / "state" / "link.json")
        os.symlink(self.ledger.path, link.path)
        with self.assertRaises(rc.ReminderError):
            link.load()

    def test_a_second_writer_waits_then_gives_up_instead_of_racing(self):
        with mock.patch.object(rc, "LOCK_TIMEOUT", 0.3), self.ledger.locked():
            with self.assertRaises(rc.ReminderError):
                with rc.Ledger(self.ledger.path).locked():
                    pass


class SelectionAndCliTests(TempState):
    def test_selection_needs_one_writable_list_and_a_name_needs_its_account(self):
        self.assertEqual(rc.select([HOME, OTHER], {"list": "Fitness", "source": "icloud"}), HOME)
        self.assertEqual(rc.select([HOME, OTHER], {"list_id": "L2", "source_id": "S1"}), OTHER)
        for selector in ({}, {"list": "Fitness"}, {"list": "Nope", "source": "iCloud"}, {"list_id": "L1"}, {"list_id": "L1 ", "source_id": "S1"}):
            with self.subTest(selector=selector), self.assertRaises(rc.ReminderError):
                rc.select([HOME, OTHER], selector)
        with self.assertRaises(rc.ReminderError):
            rc.select([replace(HOME, writable=False)], {"list": "Fitness", "source": "iCloud"})

    def test_input_files_must_be_regular_files_not_symlinks_or_pipes(self):
        real = self.dir / "in.json"
        real.write_text('{"title": "t"}')
        self.assertEqual(rc.input_object(real), {"title": "t"})
        link = self.dir / "link.json"
        os.symlink(real, link)
        fifo = self.dir / "fifo"
        os.mkfifo(fifo)
        for path in (link, fifo):
            with self.subTest(path=path.name), self.assertRaises(rc.ReminderError):
                rc.input_object(path)

    def test_cli_create_reads_stdin_and_escapes_non_ascii_output(self):
        args = rc.parser().parse_args(["--state", str(self.ledger.path), "create", "--list-id", "L1", "--source-id", "S1",
                                       "--timezone", "Australia/Melbourne", "--execute"])
        body = json.dumps({"title": "Booked: Spin class ‮", "due": "2026-10-27T18:02:00+11:00", "key": "k1"})
        with mock.patch("sys.stdin", io.StringIO(body)):
            out = rc.run(args, self.backend)
        self.assertEqual(out["operation"], "create")
        stdout = io.StringIO()
        with mock.patch.object(rc, "run", return_value=out), redirect_stdout(stdout):
            self.assertEqual(rc.main(["status"]), 0)
        self.assertNotIn("‮", stdout.getvalue())
        self.assertIn("\\u202e", stdout.getvalue())

    def test_a_prune_with_skipped_items_exits_nonzero(self):
        with mock.patch.object(rc, "run", return_value={"operation": "prune", "deleted": [], "skipped": ["R1"]}), redirect_stdout(io.StringIO()):
            self.assertEqual(rc.main(["status"]), 1)

    def test_errors_are_json_on_stderr_without_details(self):
        stderr = io.StringIO()
        with mock.patch.object(rc, "EventKitBackend", side_effect=RuntimeError("secret reminder text")), redirect_stderr(stderr), redirect_stdout(io.StringIO()):
            self.assertEqual(rc.main(["status"]), 2)
        self.assertNotIn("secret", stderr.getvalue())
        self.assertIn("error", json.loads(stderr.getvalue()))


if __name__ == "__main__":
    unittest.main()
