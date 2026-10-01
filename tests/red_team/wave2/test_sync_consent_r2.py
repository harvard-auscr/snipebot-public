"""Red-team wave 2 / round 2 — HOSTILE INPUT & SPEC CONFORMANCE for snipebot/sync.py
steps 5-7 (consent observation, evaluate/audit, reaction convergence).

Each test targets one break in the shipped code and FAILS on it. Inputs are built with
the FakeSlack authoring API (tests/fake_slack.py) and the shared sync helpers
(tests/_helpers_sync.py). No production or other test file is modified.

The L8 audit list is emitted (one `AUDIT <category> ts=<ts>` line per flagged verdict)
only under `--dry-run` (`run_sync` calls `_print_audit` in the dry-run branch, 40 §4:
"with `--dry-run` also the L8 audit list"). The run's one summary line is the
`INFO  summary  ...` line on stderr (20 §9.2).
"""

from __future__ import annotations

from snipebot.faces import FakeFaceDetector
from snipebot.sync import run_sync
from snipebot.ts import parse_ts
from snipebot.ledger import load_ledger

from tests.fake_slack import FakeSlack, FakeUser
from tests._helpers_sync import (
    BOT,
    CHANNEL,
    image_file,
    make_config,
    mkts,
    roster_of,
)


def _users(*ids: str) -> dict[str, FakeUser]:
    out = {BOT: FakeUser(id=BOT, is_bot=True)}
    for uid in ids:
        out[uid] = FakeUser(id=uid)
    return out


def _paths(tmp_path):
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    return d / "ledger.jsonl", d / "state.json"


def _run(slack, config, tmp_path, *, now, detector=None, **kw):
    led, st = _paths(tmp_path)
    return run_sync(
        slack, config, detector=detector or FakeFaceDetector({}),
        ledger_path=led, state_path=st, now_us=parse_ts(now), no_post=True, **kw,
    )


def _audit_lines(err: str) -> list[str]:
    return [ln for ln in err.splitlines() if ln.startswith("AUDIT")]


def _summary_line(err: str) -> str:
    hits = [ln for ln in err.splitlines() if " summary " in ln]
    return hits[-1] if hits else ""


def test_needs_review_message_absent_from_l8_audit(tmp_path, capsys):
    """00-data §4 (Review flag): needs_review "drives exactly three things: one extra
    reaction beside the status emoji (20 §5.3; only when review.emoji is set), **one L8
    audit category**, and a count in the sync summary line (40 §4)." 40 §4 restates the
    category: the L8 audit list flags "counted messages tagging `review.min_targets` or
    more people (`needs_review`, `00-data.md` §4)".

    The shipped `run_sync` audit dict has categories for text_blocks_disagree,
    non_permitted_veto, late_tag, ambiguous_selfie, repost, likely_repost,
    first_sight_edited, off_roster and near_cooldown — but NONE for the review flag, and
    `needs_review` is consulted only in `_desired_reactions` (the reaction). A plainly
    COUNTED message tagging `review.min_targets` people therefore reaches the ❓ reaction
    yet is flagged in no L8 audit line, so an operator scanning `--dry-run` output for the
    verdicts most likely to be wrong never sees it.
    """
    tgts = [f"U0T{i}" for i in range(5)]
    # sender and targets in different groups -> plain cross-group snipes (not sib-tagged,
    # so the SELFIE/AMBIGUOUS path is off): the ONLY reason this row needs review is the
    # tag count, which is exactly the missing category.
    roster = roster_of({"U0A": "red", **{t: "blue" for t in tgts}})
    cfg = make_config(roster=roster, review_min_targets=5, review_emoji="question")
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", *tgts))
    text = " ".join(f"<@{t}>" for t in tgts)
    ts = slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL, text=text,
                    files=[image_file("F01", b"pic")])

    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12), dry_run=True)
    audit = _audit_lines(capsys.readouterr().err)
    # spec: a counted message tagging >= review.min_targets is on the L8 audit list.
    assert any(ts in ln for ln in audit), (
        f"needs_review message {ts} never appears in the L8 audit list: {audit}"
    )


def test_needs_review_count_absent_from_summary_line(tmp_path, capsys):
    """00-data §4 (Review flag): needs_review drives "a count in the sync summary line
    (40 §4)"; 40 §4 (sync Output): the one summary line reports "messages flagged for
    review (`needs_review`, `00-data.md` §4; a count only)".

    The shipped summary line (`_log("INFO", "summary", ...)`) carries only counted,
    cooldown, selfies, faces_fetched, ambiguous_selfie and repost — there is no
    review-flag count, so the summary hides how many messages this run flagged for a human
    look.
    """
    tgts = [f"U0T{i}" for i in range(5)]
    roster = roster_of({"U0A": "red", **{t: "blue" for t in tgts}})
    cfg = make_config(roster=roster, review_min_targets=5, review_emoji="question")
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", *tgts))
    text = " ".join(f"<@{t}>" for t in tgts)
    slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL, text=text,
               files=[image_file("F01", b"pic")])

    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12))
    summary = _summary_line(capsys.readouterr().err)
    assert summary, "no summary line emitted"
    # spec: the summary line carries a count of messages flagged for review.
    assert "review" in summary, (
        f"summary line has no review-flag count: {summary!r}"
    )


def test_deleted_file_absent_from_l8_audit(tmp_path, capsys):
    """40 §4 (L8 audit list): the list flags, IDs only, among other things "files deleted
    after posting". A message posted with an uploaded image whose file is tombstoned after
    posting is exactly that case; the tombstoned file leaves the message with zero live
    images while the row is still stored (`has_file_object` is true), so an admin should be
    able to eyeball whether evidence was pulled down.

    The shipped `run_sync` audit dict has no "files deleted after posting" category and
    nothing compares a row's file objects across runs, so such a message is flagged in no
    L8 audit line.
    """
    roster = roster_of({"U0A": "fam", "U0B": "fam"})
    cfg = make_config(roster=roster)
    slack = FakeSlack(now=mkts(2026, 9, 18, 12), bot_user_id=BOT, users=_users("U0A", "U0B"))
    ts = slack.post(at=mkts(2026, 9, 18, 10), user="U0A", channel=CHANNEL, text="<@U0B>",
                    files=[image_file("F01", b"pic")])
    slack.delete_file(at=mkts(2026, 9, 18, 10, 30), ts=ts, channel=CHANNEL, file_index=0)

    # A real run first: prove the row is stored (it carried a file object at posting, so
    # `_has_media` keeps it) yet now has zero live images -- i.e. the file WAS deleted after
    # posting -- so "not flagged" is not merely "the message was never processed".
    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 11))
    rows = {r.ts: r for r in load_ledger(_paths(tmp_path)[0])}
    assert ts in rows and rows[ts].live_images == 0

    slack.as_of(mkts(2026, 9, 18, 12))
    _run(slack, cfg, tmp_path, now=mkts(2026, 9, 18, 12), dry_run=True)
    audit = _audit_lines(capsys.readouterr().err)
    assert any(ts in ln for ln in audit), (
        f"message with a file deleted after posting is in no L8 audit line: {audit}"
    )
