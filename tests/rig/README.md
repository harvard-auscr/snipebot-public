# The L6 self-driving rig

A script that plays the ordered scenario of [50-test-matrix.md](../../spec/50-test-matrix.md)
section 7 against a **throwaway** free Slack workspace (never a real one; gate G1),
posting as a real human on the user token, running the real `snipebot sync` against
the real API, then reading everything back through the bot token and asserting:

- the ledger verdicts (COUNTED / COOLDOWN with its `blocked_by` / VETOED / LATE_TAG / SELFIE),
- the reactions **actually on** each message (`reactions.get`, full users list),
- the digests **actually in** the channel and their metadata keys,
- the `post_to` second channel (R8), and
- API-vs-phone candidate **parity** (R11, the L6→G2 bridge).

## Layout

| File | What |
|---|---|
| `rig_scenario.py` | `plan()` (the ordered R0..R13 script as pure data) and `run(env)` (the live driver) |
| `assertions.py` | read-back assertion helpers over the API's own dicts |
| `config.rig.yaml` | the config template with `{{PLACEHOLDER}}` IDs the runner fills |
| `test_rig.py` | the live pytest entry (marker `rig`, opt-in): one test per `L6-RIG-*` row |
| `test_rig_offline.py` | the offline halves: `plan()`, the assertions, template render/load, the env gate, and the `CTL-RIG-NOREACT` positive control |

## Setting up the workspace (gate G1, about ten minutes, done once)

1. Create a throwaway free Slack workspace with your own account (never a real one).
   Your account there is `U_HUMAN`.
2. Invite one more member with any alias email — that is `U_TARGET`. It only has to
   accept the invite; it never acts.
3. Create two public channels: one to watch (for example `snipes`) and one for the
   officer digest (for example `officers`).
4. At api.slack.com/apps choose **Create New App → From a manifest**, pick the throwaway
   workspace and paste the repo's `slack-app-manifest.yaml`.
5. Under **OAuth & Permissions → User Token Scopes** add `chat:write`, `files:write` and
   `reactions:write` (the rig posts as you; production installs never need these).
6. **Install to Workspace** and copy the Bot User OAuth Token (`xoxb-…`) and the User
   OAuth Token (`xoxp-…`).
7. In both channels run `/invite` for the app's bot user, and add `U_TARGET` to the
   watched channel (`/invite @<target>` there, or channel details → Add people); R0's
   `DOC-ROSTER-RESOLVE` is a FAIL check and needs every roster member in the watched
   channel.
8. Put the two tokens in the environment of the shell that runs the rig (on Windows
   `setx` in a separate terminal keeps them out of any transcript).
9. For the G2 capture (`tools/capture_fixtures.py`), post two phone-only shapes into the
   watched channel from a phone, tagging `U_TARGET`: one HEIC photo with the text
   `[G2-capture] heic`, and one iOS multi-photo share (three photos in one message) with
   the text `[G2-capture] ios-multi-photo-share`. The tool finds them by those markers.

## Running it

The rig is **opt-in**. Without the five environment variables below every test in
`test_rig.py` skips with a clear reason and nothing touches the network, so the rig
is safe to leave in the normal suite. Only the offline tests run there.

To run it live, set all five and run the `rig` marker:

```bash
export SLACK_BOT_TOKEN=xoxb-…      # the app's bot token on the throwaway workspace
export SLACK_USER_TOKEN=xoxp-…     # a user token: files:write (getUploadURLExternal +
                                   #   completeUploadExternal), chat:write, reactions:write
export SNIPEBOT_RIG_CHANNEL=…      # C_MAIN — the watched channel NAME (e.g. "snipes")
export SNIPEBOT_RIG_OFFICERS=…     # C_OFF — the post_to second channel NAME (bot is a member)
export SNIPEBOT_RIG_TARGET=…       # U_TARGET — the second member's id or handle

.venv/Scripts/python.exe -m pytest tests/rig/test_rig.py -q -m rig
```

Everything else — the bot's own id, the channel IDs, `U_HUMAN` (the user token's own
id), the sibling group membership — is resolved through the API at run time. **No bot
or workspace name is ever hardcoded.** Tokens come from the environment and are never
printed; logs and the temp data dir carry user IDs only.

The runner substitutes the resolved IDs into `config.rig.yaml` and writes a resolved
`config.yaml` plus a `data/` dir under a fresh temp folder (`persistence: files`),
runs each `sync` through `snipebot.cli.main` with the default factories (real
transport, real YuNet), and on the way out **deletes the messages it posted**.

## Rig-specific config

Per PLAN.md section 9 L6 and the section 7 DECISION, the template shortens the
`cooldown` to 60 s, sets `allow_bots: true`, makes `U_HUMAN` the sole `admins` entry
(so it may cast the R5 veto under the shipped default `veto.by: [admins]`), puts
`U_HUMAN` and `U_TARGET` in one sibling group, rosters `U_BOT` (the bot's own user,
from `auth.test`) with no group so the R3 bot target counts, turns `selfie_bonus` on, and gives one
report a `post_to` second channel. These exercise the **Slack round trip only**; the
cooldown value and `allow_bots` are covered by L2 and unit tests, never by the rig.

## Positive control

`CTL-RIG-NOREACT` (the controls registry, 50 section 1.3) builds `snipebot` with
`sync`'s reaction convergence (section 7.2 "step 7") skipped. R1/R5's reaction
assertions must then turn red, proving the read-back inspects Slack rather than
restating the ledger. It is driven offline against `FakeSlack` by
`test_rig_offline.py::test_ctl_rig_noreact_positive_control`, so the control runs in
CI without a workspace.
