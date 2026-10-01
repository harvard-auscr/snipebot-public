# Snipe tracker

A small bot that watches one Slack channel and keeps score in the sniping game: a
snipe is a photo of someone, posted with an @-tag of the person in it. The bot counts
snipes per day, per person and per sibling group, exports the standings each week, and marks
each message with a reaction so people can see what was counted and why.

A snipe must be posted in the channel itself, not inside a thread: Slack's channel history
never returns thread replies, so a reply can count only when it is also sent to the channel
and `count_thread_replies` is on.

It runs as a scheduled command (`snipebot sync`), not a server. Every run re-reads the
channel, rebuilds its verdicts from the facts it has recorded, and converges the reactions
and digests in Slack to match. Nothing depends on a run having happened at any particular
time.

## What it stores

The ledger holds Slack user IDs, message timestamps and file IDs only — never names,
message text, links or image bytes. Standings are rendered from that ledger with names
resolved at print time.

Sibling-group photos get a small bonus when the photo is a selfie with a sib rather than a
snipe of one. To tell the two apart, the bot downloads the photo, counts how many faces it
contains, keeps only that number, and discards the image. No face is identified, matched
or stored.

## Layout

- `snipebot/` — the package (`python -m snipebot <command>`).
- `config.example.yaml` — every setting, with the defaults.
- `slack-app-manifest.yaml` — the Slack app definition; scopes are the minimum the commands need.
- `tests/` — the test suite; `tests/rig/` needs a real workspace and is opt-in.

## Running it

```
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt
cp config.example.yaml config.yaml   # then edit; for a local run set persistence: files
                                      # (git mode needs a data-branch worktree, see below)
$env:SLACK_BOT_TOKEN = "xoxb-..."    # PowerShell; bash: export SLACK_BOT_TOKEN=xoxb-...
                                      # from the environment only, never a file
.venv/Scripts/Activate.ps1           # PowerShell; bash: source .venv/Scripts/activate
python -m snipebot doctor
python -m snipebot backfill --from <SlackTs|Date|semester> --dry-run
```

With the venv active, `python -m snipebot --help` lists the commands.

To run in the default `persistence: git` mode on a box, keep the ledger in its own
checkout of the `data` branch (never the code clone): `git worktree add ../snipe-data data`,
then set `SNIPEBOT_DATA_REPO` to `../snipe-data` in the environment.

## Deploying (one-time setup)

The ledger lives on a branch named `data`, an orphan branch with no shared history with
`main`. The sync and admin workflows check it out at `_data` and write to `_data/data`, so
the first scheduled sync fails at "Checkout data branch" until it exists. The commands in
the blocks below run as written in PowerShell and in bash.

1. Keep the code in a private repo: the real `config.yaml` and the ledger hold Slack IDs.
   A fork of a public repo stays public, so start from a new private repo and push this
   code to it instead of forking.
2. From a clean clone of `main`, create the `data` branch with its `data/` directory:

   ```
   git switch --orphan data
   git hash-object -w /dev/null
   git update-index --add --cacheinfo 100644,e69de29bb2d1d6434b8b29ae775ad8c2e48c5391,data/.gitkeep
   git commit -m "init data branch"
   git push -u origin data
   git switch main
   ```

   The second and third lines put an empty `data/.gitkeep` in the branch (git does not
   track an empty directory).
3. Leave `data` without branch protection against force-push: `purge --rewrite-history`
   force-pushes it.
4. In the repo's Settings > Secrets and variables > Actions, add the secret
   `SLACK_BOT_TOKEN` (the bot token, `xoxb-...`). It is the only secret the workflows read.
   The variable `SNIPEBOT_GIT_AUTHOR` (`Name <email>`, the author of `rules-bump` commits) is
   optional.
5. In the organisation's Settings > Actions > General, set "Workflow permissions" to "Read
   and write permissions": the sync and admin workflows push the `data` branch (the export
   workflow only reads).
6. Copy `config.example.yaml` to `config.yaml`, fill in the real channel, players, admins,
   semesters and opt-out message, keep `persistence: git`, then commit and push it to `main`.
7. Check the setup with the token in the environment (as in Running it):

   ```
   python -m snipebot doctor
   ```

8. Record the semester so far without reacting or posting. Check out the `data` branch in
   its own worktree, set `SNIPEBOT_DATA_REPO` to `../snipe-data`
   (`$env:SNIPEBOT_DATA_REPO = "../snipe-data"` in PowerShell,
   `export SNIPEBOT_DATA_REPO=../snipe-data` in bash), then run the backfill from the start
   of the semester (put your semester's name or its start date, `YYYY-MM-DD`, after `--from`):

   ```
   git worktree add ../snipe-data data
   python -m snipebot backfill --from fall-2026 --no-react --no-post
   ```

9. In the Actions tab, run the `sync` workflow once by hand and check that it goes green.
   After that it runs on its schedule.

### Operating notes

- Whoever posts the pinned opt-out message must not react to it: everyone who reacts to it
  opts out.
- Emoji names in `config.yaml` (the veto, feedback and review reactions) must be Slack's
  canonical names, not aliases. A misspelt name shows up in the sync log as
  `WARN reaction_failed ... error=invalid_name`.
- After a `purge --rewrite-history`, GitHub keeps serving the old commits until GitHub
  Support runs garbage collection on the repo. Ask Support for it after every purge.

## Standings data

The `export` workflow builds the standings every Sunday evening (New York time). To
download them: Actions tab -> `export` workflow -> the latest
run -> Artifacts -> `standings`. It is a zip of the semester's tables as CSV files plus one
XLSX workbook with a sheet per table. To get a fresh copy between Sundays, open the `export`
workflow and use Run workflow. Between semesters it keeps exporting the semester that just
ended, even once the next one is in `config.yaml`. Runs keep their artifact for 90 days.

The artifact holds display names. It is visible only to people with access to this private
repo; the names are never committed and never printed in the workflow log.

## Photo review

`snipebot review` lists the snipe photos the face detector reads ambiguously, so an admin
can look at each one and mark it yes (a fine snipe) or no (should not have counted). It
runs locally and only reads Slack: it never reacts, posts, or touches the ledger or the
`data` branch, and a label never changes a score. To take a snipe off the board, veto it.

```
python -m snipebot review open
python -m snipebot review list
python -m snipebot review stats
python -m snipebot review label --ts 1790000000.000100 no
```

`open` reads new photos, then opens the gallery in a browser; `list` prints the same queue
as text, with Slack links; `stats` shows how your labels fell, per reason and per face
threshold.

`open` and `list` need `SLACK_BOT_TOKEN`; `stats` and `label` work offline. A post is
queued when its photo was also posted elsewhere, it tags `feedback.review.min_targets` or
more people, a photo could not be read, no face clears `faces.score_threshold`, or the
clear faces outnumber or fall short of the people tagged. Add `--all` to see every tagged photo post. The gallery runs on
127.0.0.1 only, draws the detected faces (solid green clear, dashed amber faint), and saves
each click at once; keys y, n and u (clear the label) work on the focused card.

Face boxes and labels are kept in `review/` (gitignored): message timestamps, file ids,
boxes and labels only, never names, links or images. `stats` is what the labels are for:
it shows whether a rule such as "no clear face" lines up with your no's before any rule
changes.

## Recaps in Slack

The bot does not post standings digests in Slack by default: `config.example.yaml` ships
`recaps: false`. A digest also needs an entry under `reports`, so with `reports: []`
nothing is posted either way. To switch digests on or off without editing the YAML, run
one of these locally:

```
python -m snipebot recaps on
python -m snipebot recaps off
```

then commit and push `config.yaml` so the scheduled sync sees it. Or run the `admin` workflow
with the `recaps-on` or `recaps-off` command, which commits the change to `config.yaml` for
you. `python -m snipebot recaps` alone prints the current setting.
