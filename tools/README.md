# tools/

The G2 capture tool (spec/10-slack-io.md section 8; PLAN.md section 9 L1 /
section 13 gate G2). Replaces the hand-authored fixtures in
`tests/fixtures/` with real, scrubbed Slack captures, file-for-file, without
changing any test.

## Files

- `scrub.py` -- pure, offline, fully unit-tested. `scrub(payload, scrub_map)`
  applies every rule in the 10-slack-io.md section 8 ID-scrub table to one raw
  Slack payload, using a deterministic map (real id -> placeholder) that the
  caller persists across every file it scrubs. `no_real_identifiers(payload)`
  is the verifier: it returns a list of leftover-identifier problems (empty =
  clean) and is what a capture is checked against before it is ever written.
- `capture_fixtures.py` -- the network-touching tool itself. Posts each L1
  shape with the Slack user token, captures it back with the bot token,
  scrubs and verifies it, and writes it to its fixture path. Also captures
  the seven before/after re-fetch pairs, `reactions.get` responses,
  `users.list`, `conversations.info` for both channels, and one real digest,
  and runs the G2 `thumb_1024` byte-stability probe.
- The map lives in `tests/fixtures/`, split in two. `scrub_map.json` is
  committed and holds only the placeholder vocabulary (each placeholder mapped
  to itself). `scrub_map.local.json` holds the real-id half, is gitignored, and
  stays on the machine that ran the capture: committing it would make every
  scrubbed fixture reversible. A re-run on another machine starts a fresh real
  half and pins the same roles onto the same placeholders.

## Usage

```powershell
# Print the plan and the owner's phone checklist. No network, no tokens needed.
.venv\Scripts\python.exe tools\capture_fixtures.py --dry-run

# Same, restricted to one shape or refetch case.
.venv\Scripts\python.exe tools\capture_fixtures.py --dry-run --only photo-and-tag

# The real thing: requires a throwaway Slack workspace with a bot token
# (channels:history, files:read, channels:read, users:read, reactions:write,
# reactions:read, chat:write -- see PLAN.md section 5) and a user token
# (chat:write as that user) exported as environment variables. Refuses
# cleanly and touches no network if either is unset.
$env:SLACK_BOT_TOKEN = "..."
$env:SLACK_USER_TOKEN = "..."
.venv\Scripts\python.exe tools\capture_fixtures.py

# One shape or refetch case at a time.
.venv\Scripts\python.exe tools\capture_fixtures.py --only heic
```

`--config` points at the config.yaml the watched channel and roster come
from (default `config.yaml`); the tool never hardcodes a channel id, bot
name, or workspace name.

Two shapes need a human: `heic` and `ios-multi-photo-share` are posted from
the owner's phone (the plan prints the exact marker text to include), and
the tool locates them afterwards by scanning recent history for that marker.
`slack-connect-file` is not reproducible on a throwaway workspace and stays
the hand-authored provisional file.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest tests\test_scrub.py -q
```

Covers: the scrub rules against a hand-built, captured-looking payload
(realistic ids, urls, a token, a `client_msg_id`); determinism of the id map;
that `scrub()` is a no-op on every fixture already checked in under
`tests/fixtures/` (the provisional stage); that `no_real_identifiers` catches
each leftover class (a real-looking id, a non-`fixture.invalid` URL, a
token-like key, an un-nulled `permalink`); and that `--dry-run` prints the
plan and exits 0 without constructing a `slack_sdk.WebClient` or needing
either token.
