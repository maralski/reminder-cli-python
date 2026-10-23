# reminder-cli-python

A small macOS command-line tool for Apple Reminders. It calls EventKit directly through
[PyObjC](https://pyobjc.readthedocs.io/en/latest/apinotes/EventKit.html); it does not use
AppleScript. Reminders does not need to be open. Changes sync through the Mac's configured
accounts, so a reminder created here alerts on your other devices too.

Every write is a preview unless you add `--execute`.

## Setup

Requires macOS 14 or newer and Python 3.10 or newer.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements.txt
./reminder-cli status
```

## Permission

`status` reports Reminders access without prompting. Every other command needs **Full Access
to Reminders** for the program that runs this tool (Terminal, or the app that launches it).
The tool never asks for access itself; grant it in System Settings → Privacy & Security →
Reminders, or through the launching app's own request.

## Lists

```sh
./reminder-cli lists
./reminder-cli ensure-list --name Gym              # preview
./reminder-cli ensure-list --name Gym --execute    # create once; later runs find the same list
```

`ensure-list` creates the list in the account of your default Reminders list unless you pass
`--source-id`. It refuses a local "On My Mac" account, which never syncs, unless you name it with
`--source-id`. Commands that work on reminders take `--list NAME` together with `--source ACCOUNT`,
`--list-id` with `--source-id`, or the same keys from a `--config` JSON file:

```json
{ "list_id": "LIST_ID", "source_id": "SOURCE_ID", "timezone": "Australia/Melbourne" }
```

## Create

The reminder comes from stdin as JSON:

```sh
./reminder-cli create --list Gym --timezone Australia/Melbourne --execute <<'JSON'
{
  "title": "Booked: Spin class, Tue 27 Oct, 6:00 pm",
  "due": "2026-10-21T13:02:00+11:00",
  "expires": "2026-10-27T18:00:00+11:00",
  "key": "example-booking-001",
  "notes": "Cancel at least 90 minutes before class."
}
JSON
```

- `title`, `due`, and `key` are required. A time without an offset uses `--timezone`.
- `alert` (default `true`) adds an alert at the due time.
- `expires` marks when `prune` may delete the reminder.
- `key` makes creation repeatable. A second `create` with the same key reports `exists` instead
  of adding another reminder. If that reminder's title was changed, or it was deleted or moved,
  `create` stops rather than overwrite or recreate it.

## The ledger

Every reminder this tool creates is recorded, with its key and expiry, in a private ledger at
`~/Library/Application Support/reminder-cli/state.json` (choose another with `--state`, before the
subcommand). The file must be a 0600 regular file you own; symlinks are rejected. A lock file next
to it makes overlapping runs wait instead of racing. Previews never write it.

The key and expiry are also written into the reminder's notes so you can see them, but anyone who
can edit the list can copy or type those lines. The ledger, not the notes, decides what this tool
may delete: a reminder carrying the key that is not in the ledger makes `create` stop, and `delete`
and `prune` never touch it.

## Delete and prune

```sh
./reminder-cli delete --list Gym --id REMINDER_ID --key example-booking-001 --execute
./reminder-cli prune --list Gym --execute
```

`delete` removes one reminder only when the ledger records it under exactly that key. `prune`
deletes the reminders the ledger records for the list whose expiry has passed, and forgets ledger
entries whose reminder is already gone. A reminder that changed since it was read, or no longer
carries its key, is skipped and kept in the ledger.

`./reminder-cli reminders --list Gym` lists a list's reminders.

## Output and errors

Results are JSON on stdout, with non-ASCII characters escaped. Errors are JSON on stderr with exit
code 2, and never include reminder text or provider error details. A `prune` that skipped any
reminder still prints what it deleted, and exits with code 1.

## Tests

```sh
python3 -m unittest -v test_reminder_cli.py
```

The tests use an in-memory fake backend and never touch real reminders.

Dependencies are pinned to exact versions and every published wheel hash; install them with
`--require-hashes` as shown above.
