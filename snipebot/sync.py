"""The `sync` state machine (20 §1-§5, §8-§9): steps 0-8 and 10, plus the step-9
digest hook (`post_digests`, filled in by the digest writer) and the git lease-retry
wrapper `run_sync_git`.

`sync.py` is pure orchestration: it never imports the production Slack client or `cv2`.
The `SlackIO` client and the `FaceDetector` are injected (10 §2, §9); the ledger/verdicts/
state serializers and integrity checks live in `snipebot.ledger`; the git commit/push
primitives live in `snipebot.persistence`. Only counts and Slack IDs ever reach a log line
or a commit message (§9.2): never a name, permalink, rendition URL, file name, hash or byte.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
import warnings

from snipebot.config import (
    Cadence,
    FingerprintGuardError,
    NoRuleInForceError,
    Persistence,
    VetoActor,
    compute_fingerprints,
)
from snipebot.faces import UndecodableImage
from snipebot.ledger import (
    LedgerIntegrityError,
    MalformedLedgerError,
    State,
    check_integrity,
    count_moved_pairs,
    dumps_ledger,
    dumps_state,
    dumps_verdicts,
    load_ledger,
    load_state,
    save_ledger,
    save_state,
    save_verdicts,
)
from snipebot.aggregate import eligible_snipes
from snipebot.parse import (
    Candidate,
    Digest,
    SelfieOverride,
    TargetEdit,
    Veto,
    VetoSource,
    parse,
    rendition_url,
)
from snipebot.periods import most_recent_due
from snipebot.report import DigestTooLargeError, NameResolver, render_digest
from snipebot.persistence import (
    MAX_LEASE_RETRIES,
    LeaseRejected,
    store_for,
)
from snipebot.rules import (
    MessageVerdict,
    Reason,
    SelfieClass,
    Status,
    evaluate,
    needs_review,
)
from snipebot.slack_io import (
    AlreadyReacted,
    AuthError,
    FileTooLarge,
    MessageNotFound,
    MissingScope,
    NoReaction,
    RateLimited,
    SlackAPIError,
    SlackError,
    SlackHTTPError,
    SlackTransportError,
)
from snipebot.ts import US_PER_SECOND, format_ts, parse_ts

if TYPE_CHECKING:  # injected surfaces (never imported at runtime)
    from snipebot.config import Config
    from snipebot.faces import FaceDetector
    from snipebot.slack_io import SlackIO


# --- 1. Public types (20 §1.3) -----------------------------------------------

class Command(str, Enum):
    SYNC = "sync"
    RUN = "run"
    BACKFILL = "backfill"
    VETO = "veto"
    UNVETO = "unveto"
    REJOIN = "rejoin"
    PURGE = "purge"
    ACCEPT_DELETES = "accept_deletes"
    RULES_BUMP = "rules_bump"
    RESTORE = "restore"
    SELFIE = "selfie"


LARGE_MOVEMENT_COMMANDS: frozenset[Command] = frozenset({
    Command.BACKFILL, Command.VETO, Command.UNVETO, Command.REJOIN,
    Command.PURGE, Command.ACCEPT_DELETES, Command.RULES_BUMP, Command.RESTORE,
    Command.SELFIE,
})


@dataclass(frozen=True)
class SyncResult:
    exit_code: int
    ledger_written: bool
    commit_sha: str | None
    newly_deleted: int
    reactions_added: int
    reactions_removed: int
    digests_posted: int
    digests_revised: int
    moved_lines: tuple[str, ...] = ()   # the §8.4 count-by-reason flip lines this run produced
    # 40 §4.2 run-summary counts carried to the CLI for the stdout summary line (counts only,
    # never IDs or names). `counts_by_reason` is the dry-run's absolute §8.4 count-by-reason
    # tokens (the run wrote nothing, so `moved_lines`/deltas are empty). `target_status` is the
    # veto/unveto target row's resulting message-level verdict (40 §4.2 "the message's new verdict").
    rows_scanned: int = 0
    selfies: int = 0
    faces_fetched: int = 0
    ambiguous_selfie: int = 0
    repost: int = 0
    needs_review: int = 0
    breaker_released: bool = False
    counts_by_reason: tuple[str, ...] = ()
    target_verdict: str | None = None
    sealed_sha: str | None = None  # the sealed pre-movement restore point (large movement only)


# The reason whitelist of 00-data §4's "Reacted" rule, restated here (a sync-local constant
# that MUST equal that rule's list, 20 §5.3).
REACTED_NOT_COUNTED_REASONS: frozenset[Reason] = frozenset({
    Reason.SENDER_OFF_ROSTER, Reason.TARGET_OFF_ROSTER, Reason.LATE_TAG,
    Reason.TOO_MANY_TARGETS, Reason.OUT_OF_SEASON, Reason.REPOST,
})

DAY_US: int = 86_400 * US_PER_SECOND
H24_US: int = 24 * 60 * 60 * US_PER_SECOND


# --- 2. Crash-injection hook (20 §2.2) ---------------------------------------

def _boundary(name: str, key: str | None = None) -> None:
    """Kill the process hard (os._exit(137)) when SNIPEBOT_CRASH_AT == name, optionally
    gated on SNIPEBOT_CRASH_KEY. Tests only; unset in production, where it is a no-op."""
    if os.environ.get("SNIPEBOT_CRASH_AT") != name:
        return
    want_key = os.environ.get("SNIPEBOT_CRASH_KEY")
    if want_key is not None and want_key != (key or ""):
        return
    os._exit(137)


def _log(level: str, event: str, **fields: Any) -> None:
    """One event per line to stderr (§9.2). Values are counts and Slack IDs only."""
    now = datetime.now(timezone.utc).isoformat()
    parts = [now, level, event]
    parts.extend(f"{k}={v}" for k, v in fields.items())
    print("  ".join(parts), file=sys.stderr)


# --- 3. Fetch range (20 §3) --------------------------------------------------

def _horizon_floor_us(now_us: int, config: "Config") -> int:
    days = config.sync.history_horizon_days
    if days is None:
        return -(1 << 62)
    return now_us - days * DAY_US


def fetch_oldest_us(now_us: int, state: State, rows, config: "Config",
                    backfill_from_us: int | None) -> int:
    if backfill_from_us is not None:
        return backfill_from_us
    scan_floor_us = now_us - config.sync.scan_days * DAY_US
    terms = [scan_floor_us]
    if state.watermark is not None:
        terms.append(parse_ts(state.watermark))
    pending = [parse_ts(r.ts) for r in rows if r.missing_runs >= 1]
    if pending:
        terms.append(max(min(pending), _horizon_floor_us(now_us, config)))
    return min(terms)


# --- 4. Small shared predicates ----------------------------------------------

def _sib_tagged(row: Candidate, roster, opted_out) -> bool:
    """`sender` and >= 1 current real-user `target` share a sibling group, both members
    at `row.ts` (00-data §2). A self-tag (E-W4-29) or an opted-out sib (E-W4-30) never
    makes a message sib-tagged."""
    ts_us = parse_ts(row.ts)
    group = roster.group_of(row.sender, ts_us)
    if group is None or not roster.is_member_at(row.sender, ts_us):
        return False
    return any(
        t != row.sender and t not in opted_out
        and roster.group_of(t, ts_us) == group and roster.is_member_at(t, ts_us)
        for t in row.targets
    )


def _live_image_count(row: Candidate, config: "Config") -> int:
    rule = config.rules.in_force_at(parse_ts(row.ts))
    return (
        row.live_images
        + (row.live_videos if rule.allow_video else 0)
        + (row.linked_images if rule.count_image_links else 0)
    )


def _base_name(name: str) -> str:
    """A person's reaction matches on its base name: `<name>::skin-tone-<n>` counts as
    `<name>` (E-W4-2). The bot's own feedback reactions stay exact-name."""
    return name.split("::", 1)[0]


def _reaction_full_users(raw: dict, emoji: str, slack: "SlackIO", channel: str,
                         ts: str, *, tolerate_missing: bool = True) -> list[str] | None:
    """The complete reactor list for `emoji` on `raw` (every skin-tone variant folded into
    the base name, E-W4-2), using the payload lists when they are complete
    (`count == len(users)`) and one `reactions_get` otherwise. `None` when the emoji is
    absent. With `tolerate_missing=False` a vanished message re-raises MessageNotFound so
    the caller can keep its facts unchanged."""
    matching = [r for r in raw.get("reactions", []) if _base_name(r.get("name", "")) == emoji]
    if not matching:
        return None
    reactions = matching
    if any(r.get("count", len(r.get("users", []))) != len(r.get("users", []))
           for r in matching):
        try:
            full = slack.reactions_get(channel, ts)
        except MessageNotFound:
            # (a) veto/selfie read of a message that vanished mid-run
            # (aged past the horizon, or deleted): not an error, changes nothing.
            if not tolerate_missing:
                raise
            return None
        reactions = [r for r in full.get("reactions", [])
                     if _base_name(r.get("name", "")) == emoji]
    users: list[str] = []
    for r in reactions:
        for u in r.get("users", []):
            if u not in users:
                users.append(u)
    return users


# --- 5. Merge (20 §4) --------------------------------------------------------

def _accumulate_target_edits(stored: Candidate, fresh: Candidate) -> tuple[TargetEdit, ...]:
    edits = list(stored.target_edited_in)
    known = {te.user for te in edits}
    for t in fresh.targets:
        if t not in stored.first_seen_targets and t not in known:
            edits.append(TargetEdit(user=t, edit_ts=fresh.last_edit_ts))
            known.add(t)
    edits.sort(key=lambda te: te.user)
    return tuple(edits)


def _replace_facts(stored: Candidate, fresh: Candidate) -> Candidate:
    """Wholesale-replace the observed facts, accumulate first-seen target edits, carry the
    faces facts untouched, and carry the stored veto set unchanged: step 5 rebuilds the
    `REACTION` half for scan-window rows only, so a row fetched from below the scan floor
    keeps the vetoes it already had (facts are final after scan_days). `missing_runs`
    resets to 0 (20 §4.1)."""
    return replace(
        stored,
        sender=fresh.sender,
        subtype=fresh.subtype,
        thread_ts=fresh.thread_ts,
        targets=fresh.targets,
        live_images=fresh.live_images,
        live_image_ids=fresh.live_image_ids,
        live_videos=fresh.live_videos,
        linked_images=fresh.linked_images,
        last_edit_ts=fresh.last_edit_ts,
        file_sigs=fresh.file_sigs,
        vetoes=stored.vetoes,
        missing_runs=0,
        target_edited_in=_accumulate_target_edits(stored, fresh),
        face_counts=dict(stored.face_counts),
        rendition_hash=dict(stored.rendition_hash),
        has_file_object=fresh.has_file_object,
    )


def _has_media(c: Candidate) -> bool:
    # Keep a fresh row that carries at least one file object (live or not), a live video,
    # or a linked image (20 §2 step 3; 00-data §2). An image post whose only file was
    # tombstoned before first sight has live_images==0 yet must still be stored, so the gate
    # is on file-object presence, not live media.
    return bool(c.has_file_object or c.live_videos or c.linked_images)


def _crosses_delete_threshold(old_missing: int, new_missing: int) -> bool:
    """The two-miss rule (20 §4.1; `Candidate.deleted`): a row is newly deleted the run
    its `missing_runs` count crosses from 1 to 2, never on the first miss."""
    return old_missing == 1 and new_missing == 2


def _merge(stored_rows, parsed_candidates, returned_ts, oldest_us, scan_floor_us,
           config: "Config", now_us: int) -> tuple[dict[str, Candidate], int]:
    """Fold fresh candidates and observed absences into rows keyed by ts. Returns the merged
    map and `newly_deleted` (rows crossing `missing_runs` 1 -> 2). Logs an IDs-free merge line."""
    stored_by_ts = {r.ts: r for r in stored_rows}
    parsed_by_ts = {c.ts: c for c in parsed_candidates}
    horizon_floor_us = _horizon_floor_us(now_us, config)
    horizon_days = config.sync.history_horizon_days

    merged: dict[str, Candidate] = {}
    added = replaced = pending_miss = newly_deleted = 0

    for ts, stored in stored_by_ts.items():
        fresh = parsed_by_ts.get(ts) if ts in returned_ts else None
        if fresh is not None:
            merged[ts] = _replace_facts(stored, fresh)
            replaced += 1
            continue
        # Absent, or returned only as a message parse rejects (a `subtype: tombstone`
        # parent kept for its replies): the same miss either way (20 §4.2, E-W4-4).
        # A complete fetch that returned zero messages infers no misses (E-W4-16).
        ts_us = parse_ts(ts)
        in_range = ts_us >= oldest_us
        young = horizon_days is None or ts_us >= horizon_floor_us
        if in_range and young and returned_ts:
            new_missing = stored.missing_runs + 1
            if new_missing == 1:
                pending_miss += 1
            if _crosses_delete_threshold(stored.missing_runs, new_missing):
                newly_deleted += 1
            merged[ts] = replace(stored, missing_runs=new_missing)
        else:
            merged[ts] = stored

    for ts, fresh in parsed_by_ts.items():
        if ts in stored_by_ts:
            continue
        if not _has_media(fresh):
            continue  # text-only posts are never stored (20 §4.1)
        merged[ts] = fresh
        added += 1

    _log("INFO", "merge", added=added, replaced=replaced,
         pending_miss=pending_miss, newly_deleted=newly_deleted)
    return merged, newly_deleted


# --- 6. Consent: vetoes, opt-outs, admin selfie (20 §5.1, §5.2, §5.2.1) -------

def _observe_vetoes(merged: dict[str, Candidate], raw_by_ts: dict[str, dict],
                    scan_floor_us: int, config: "Config", slack: "SlackIO",
                    audit: dict[str, list[str]], inserted_ts: set[str]) -> None:
    veto_emoji = config.consent.veto_emoji
    veto_by = config.consent.veto_by
    admins = config.admins
    for ts, row in list(merged.items()):
        # Scan-window rows, plus every row this run inserts for the first time whatever its
        # age (go-live backfill, gap recovery); stored rows below the floor keep their
        # stored reaction facts (20 §5.1, E-W4-11).
        if parse_ts(ts) < scan_floor_us and ts not in inserted_ts:
            continue
        raw = raw_by_ts.get(ts)
        if raw is None:
            # Not returned this run: it only gains a miss; its facts, both veto halves
            # included, stay unchanged (20 §4.2).
            continue
        cli_vetoes = tuple(v for v in row.vetoes if v.source == VetoSource.CLI)
        reaction_vetoes: list[Veto] = []
        try:
            users = _reaction_full_users(raw, veto_emoji, slack, config.channel, ts,
                                         tolerate_missing=False)
        except MessageNotFound:
            continue  # vanished mid-run: changes nothing (10 §3), both veto halves kept
        if users is not None:
            for u in users:
                is_admin = VetoActor.ADMINS in veto_by and u in admins
                is_target = VetoActor.TARGET in veto_by and u in row.targets
                if is_admin or is_target:
                    reaction_vetoes.append(Veto(by=u, source=VetoSource.REACTION))
                else:
                    audit["non_permitted_veto"].append(ts)
        all_vetoes = tuple(sorted(
            cli_vetoes + tuple(reaction_vetoes),
            key=lambda v: (v.by, v.source.value),
        ))
        if all_vetoes != row.vetoes:
            merged[ts] = replace(row, vetoes=all_vetoes)


def _observe_optouts(state: State, config: "Config", slack: "SlackIO",
                     now_us: int) -> None:
    """Read every configured opt-out message with `reactions_get` on every run, in or out
    of the fetch range (20 §5.2, E-W4-3). An unreadable message is skipped and counted in
    one WARN, never an error. Any emoji from anyone opts out. Logs counts only (E-W4-24)."""
    observed = unreadable = 0
    for msg_ts in config.consent.optout_message_ts:
        try:
            full = slack.reactions_get(config.channel, msg_ts)
        except SlackAPIError:
            # deleted, or aged past Slack's horizon: skip it, never an abort (§5.2).
            unreadable += 1
            continue
        reactors = {u for r in full.get("reactions", []) for u in r.get("users", [])}
        for u in sorted(reactors):
            if u not in state.opted_out:
                state.opted_out[u] = now_us
                observed += 1
    for u in config.consent.seed_opted_out:
        if u not in state.opted_out:
            state.opted_out[u] = now_us
            observed += 1
    if unreadable:
        _log("WARN", "optout_unreadable", skipped=unreadable)
    if observed:
        _log("INFO", "optout", observed=observed, total=len(state.opted_out))


def _observe_admin_selfie(merged: dict[str, Candidate], raw_by_ts: dict[str, dict],
                          scan_floor_us: int, config: "Config", slack: "SlackIO",
                          bot_user_id: str, inserted_ts: set[str], opted_out) -> None:
    selfie_emoji = config.feedback.selfie
    if selfie_emoji is None:
        return
    admins = config.admins
    for ts, row in list(merged.items()):
        override = row.selfie_override
        # A CLI override is durable and beats the detector either way: never touched here
        # (§5.2.1). A REACTION-sourced (or unset) override converges like a veto — it is
        # recomputed every in-window sync from the current full admin-reactor set, so the
        # persisted `by` is first-seen and byte-identical across schedules, never a differ.
        # Rows first inserted by this run are observed whatever their age (E-W4-11).
        if (parse_ts(ts) < scan_floor_us and ts not in inserted_ts) or (
            override is not None and override.source == VetoSource.CLI
        ):
            continue
        # Sib-gate the reaction observation exactly like the CLI selfie path (R3, §5.2.1):
        # a selfie reaction on a non-sib-tagged row writes no override.
        if not _sib_tagged(row, config.roster, opted_out):
            continue
        raw = raw_by_ts.get(ts)
        if raw is None:
            continue
        users = _reaction_full_users(raw, selfie_emoji, slack, config.channel, ts)
        if users is None:
            continue
        A = sorted(u for u in users if u in admins and u != bot_user_id)
        if A:
            by = A[0]
            if override is not None and override.value and override.by == by \
                    and override.source == VetoSource.REACTION:
                continue  # already converged to this reactor: nothing to rewrite
            merged[ts] = replace(
                row,
                selfie_override=SelfieOverride(
                    value=True, by=by, source=VetoSource.REACTION
                ),
            )
            _log("INFO", "selfie_override", ts=ts, value="true", by=by, source="reaction")


# --- 7. Face detection (20 §5.2.2) -------------------------------------------

def _detect_faces(merged: dict[str, Candidate], fetched_messages, returned_ts,
                  config: "Config", slack: "SlackIO", detector: "FaceDetector",
                  opted_out) -> int:
    """Fetch, hash and count the uncounted live images of eligible sib-tagged rows, writing
    the faces facts in-memory (persisted atomically at step 8). Returns images fetched."""
    file_by_id: dict[str, dict] = {
        f["id"]: f
        for m in fetched_messages
        for f in m.get("files", [])
        if "id" in f
    }
    fetched = 0
    skip_run = False
    for ts in sorted(merged, key=parse_ts):
        row = merged[ts]
        if row.ts not in returned_ts:
            continue
        if not _sib_tagged(row, config.roster, opted_out):
            continue
        if row.detect_attempts >= config.faces.max_attempts:
            continue
        try:
            rule_in_force = config.rules.in_force_at(parse_ts(row.ts))
        except NoRuleInForceError:
            # Step 5 never aborts (20 §5.2.2). A row with no rule in force is not a
            # detection candidate; step 6 evaluate owns the NoRuleInForceError -> exit 2.
            continue
        if not rule_in_force.selfie_bonus:
            continue
        uncounted = [i for i in sorted(row.live_image_ids) if i not in row.face_counts]
        if not uncounted:
            continue

        face_counts = dict(row.face_counts)
        rendition_hash = dict(row.rendition_hash)
        images_counted = 0
        for image_id in uncounted:
            raw_file = file_by_id.get(image_id)
            if raw_file is None:
                continue
            url = rendition_url(raw_file)
            if url is None:
                continue
            _boundary("faces:before", key=row.ts)
            n: int | None = None
            try:
                data = slack.fetch_file_bytes(url)
                digest = sha256(data).hexdigest()
                n = detector.count_faces(data)
            except (RateLimited, FileTooLarge, SlackHTTPError, SlackTransportError,
                    UndecodableImage):
                pass  # transient/per-image fault: no count this run; continue
            except (MissingScope, AuthError):
                _log("WARN", "faces_skipped", reason="missing_scope")
                skip_run = True
                break  # scope fault: aborts the run; no faces:after on this path (§5.2.2)
            except Exception as exc:
                # Any other detector fault (model file missing or unreadable, cv2.error, a
                # pillow_heif import error) is a failed attempt for this image, never an
                # abort (20 §5.2.2, E-W4-35).
                _log("WARN", "faces_failed", ts=row.ts, kind=type(exc).__name__)
            if n is not None:
                face_counts[image_id] = n
                rendition_hash[image_id] = digest
                images_counted += 1
                fetched += 1
            _boundary("faces:after", key=row.ts)

        if skip_run:
            # Scope/token fault: leave this and every later row's detect_attempts
            # unchanged (doctor owns it, 20 §5.2.2). Any facts written before the fault
            # on this row are kept.
            if images_counted:
                merged[ts] = replace(row, face_counts=face_counts,
                                     rendition_hash=rendition_hash)
            break

        still_uncounted = any(i not in face_counts for i in row.live_image_ids)
        new_attempts = row.detect_attempts + (1 if still_uncounted else 0)
        merged[ts] = replace(
            row,
            face_counts=face_counts,
            rendition_hash=rendition_hash,
            detect_attempts=new_attempts,
        )
        if images_counted or still_uncounted:
            _log("INFO", "faces", ts=row.ts, images=len(uncounted),
                 counted=images_counted, attempts=new_attempts)
    return fetched


# --- 8. Reaction convergence (20 §5.3) ---------------------------------------

def _desired_reactions(mv: MessageVerdict, row: Candidate, config: "Config") -> set[str]:
    if _live_image_count(row, config) == 0:
        return set()
    e = config.feedback.emoji_for(mv.status)
    if mv.status == Status.NOT_COUNTED and mv.reason not in REACTED_NOT_COUNTED_REASONS:
        e = None
    desired = {e} if e is not None else set()
    # The selfie emoji only when the message awards a selfie point: at least one COUNTED
    # intra-group pair, not merely class SELFIE (20 §5.3, E-W4-31).
    if config.feedback.selfie is not None and any(p.selfie for p in mv.pairs):
        desired.add(config.feedback.selfie)
    if config.review.emoji is not None and needs_review(mv, row, config.review):
        desired.add(config.review.emoji)
    return desired


def _observed_reactions(raw: dict, slack: "SlackIO", channel: str, ts: str,
                        bot_user_id: str) -> set[str]:
    observed: set[str] = set()
    for reaction in raw.get("reactions", []):
        users = reaction.get("users", [])
        if bot_user_id in users:
            observed.add(reaction["name"])
        elif reaction.get("count", len(users)) != len(users):
            try:
                full = slack.reactions_get(channel, ts)
            except MessageNotFound:
                # (b) the row vanished mid-run: not an error, skip it (§5.3 / 10 §3).
                return observed
            return {
                r["name"] for r in full.get("reactions", [])
                if bot_user_id in r.get("users", [])
            }
    return observed


def _ends_step7(exc: SlackError) -> bool:
    """RateLimited after its bounded retries, an auth/scope fault, or a failure below the
    API layer (transport, HTTP status) ends step 7 for this run; any other SlackAPIError
    only skips that one reaction (20 step 7, E-W4-21)."""
    return isinstance(exc, (RateLimited, AuthError, MissingScope)) \
        or not isinstance(exc, SlackAPIError)


def _error_code(exc: SlackError) -> str:
    return getattr(exc, "error", None) or type(exc).__name__


def _converge_reactions(merged: dict[str, Candidate], verdicts, raw_by_ts, scan_floor_us,
                        config: "Config", slack: "SlackIO", bot_user_id: str) -> tuple[int, int]:
    """Step 7 never aborts a run (E-W4-21): a failed reaction is logged
    `WARN reaction_failed` and skipped, a run-level fault ends step 7 with one WARN, and
    reactions converge on a later run."""
    added = removed = 0
    mv_by_ts = {mv.ts: mv for mv in verdicts}
    for ts in sorted(merged, key=parse_ts):
        if parse_ts(ts) < scan_floor_us:
            continue
        mv = mv_by_ts.get(ts)
        if mv is None:
            continue
        row = merged[ts]
        desired = _desired_reactions(mv, row, config)
        raw = raw_by_ts.get(ts, {})
        try:
            observed = _observed_reactions(raw, slack, config.channel, ts, bot_user_id)
        except SlackError as exc:
            if _ends_step7(exc):
                _log("WARN", "reactions_stopped", error=_error_code(exc))
                return added, removed
            _log("WARN", "reaction_failed", ts=ts, error=_error_code(exc))
            continue
        if mv.reason in (Reason.SENDER_OPTED_OUT, Reason.TARGET_OPTED_OUT):
            # An opt-out never removes the bot's existing status reaction: tearing several
            # off at once would announce the opt-out to the channel (PLAN §6; 00-data §4).
            # Both opt-out reasons are covered — a target opt-out promoted to the message
            # reason must stay as silent as a sender opt-out.
            # Treat observed as desired so observed - desired is empty; additions still
            # stay governed by desired - observed (empty for an opt-out).
            desired = desired | observed
        # Removes first, then adds (§5.3); each op tolerates its own benign codes.
        ops = [(slack.reactions_remove, e, (NoReaction, MessageNotFound), False)
               for e in sorted(observed - desired)]
        ops += [(slack.reactions_add, e, (AlreadyReacted, MessageNotFound), True)
                for e in sorted(desired - observed)]
        for call, emoji, tolerated, is_add in ops:
            _boundary("reaction:before", key=ts)
            try:
                call(config.channel, ts, emoji)
            except tolerated:
                pass
            except SlackError as exc:
                if _ends_step7(exc):
                    _log("WARN", "reactions_stopped", error=_error_code(exc))
                    return added, removed
                _log("WARN", "reaction_failed", ts=ts, error=_error_code(exc))
            else:
                if is_add:
                    added += 1
                else:
                    removed += 1
            _boundary("reaction:after", key=ts)
        if desired != observed:
            _log("INFO", "react", ts=ts,
                 add="+".join(sorted(desired - observed)) or "none",
                 remove="+".join(sorted(observed - desired)) or "none")
    return added, removed


# --- 9. Digest posting hook (step 9; filled in by the digest writer) ----------

@dataclass(frozen=True)
class PostedDigest:              # built from a snipe_digest message's metadata (00-data §9)
    channel: str
    ts: str
    report: str
    period_key: str
    semester: str
    numbers_hash: str
    revision: int


def _build_name_cache(slack: "SlackIO") -> dict[str, str]:
    """(Re)build the id->display-name cache from `users.list` when the caller supplied
    none (20 §2 storage; 10 §2): display name, falling back to real name."""
    cache: dict[str, str] = {}
    for user in slack.users_list():
        uid = user.get("id")
        if not uid:
            continue
        profile = user.get("profile") or {}
        cache[uid] = (profile.get("display_name") or profile.get("real_name") or "")
    return cache


def _semester_named(config: "Config", name: str):
    return next((s for s in config.semesters if s.name == name), None)


def _period_key_fits(report, period_key: str, semester: str) -> bool:
    """Whether `period_key` parses under `report`'s CURRENT cadence (00-data §8):
    `<report>:YYYY-MM-DD` daily, `<report>:YYYY-Www` weekly, `<report>:<semester>` final.
    A key minted under an older cadence is skipped by Pass B (20 §6.2, E-W4-12)."""
    prefix = f"{report.name}:"
    if not period_key.startswith(prefix):
        return False
    tail = period_key[len(prefix):]
    try:
        if report.cadence is Cadence.DAILY:
            return len(tail) == 10 and date.fromisoformat(tail).isoformat() == tail
        if report.cadence is Cadence.WEEKLY:
            year_s, sep, week_s = tail.partition("-W")
            if not (sep and len(year_s) == 4 and len(week_s) == 2
                    and year_s.isdigit() and week_s.isdigit()):
                return False
            date.fromisocalendar(int(year_s), int(week_s), 1)
            return True
        if report.cadence is Cadence.FINAL:
            return tail == semester
    except ValueError:
        return False
    return False


def post_digests(slack: "SlackIO", config: "Config", *, ledger, verdicts, digests,
                 now_us: int, users_cache, scan_floor_us: int,
                 boundary: Callable[[str], None],
                 opted_out: set[str] | None = None,
                 skip_channels: frozenset[str] = frozenset()) -> tuple[int, int]:
    """Post the most-recent-due digest per report (Pass A) and revise changed in-window
    digests (Pass B), keyed on `(channel, period_key)` derived from the channels actually
    posted to (20 §6). Returns (posted, revised). A report whose `post_to` is in
    `skip_channels` (step 2 could not read it) posts and revises nothing (E-W4-34).

    `render_digest` may raise `DigestTooLargeError` and the Slack posts may raise
    `SlackError`; both are untolerated here and surface to the run's exit-1 mapping
    (20 §9.1). The `boundary` hook fires `digest:before`/`digest:after` around each
    `post_message`/`update_message` with key `f"{channel}:{period_key}"` (20 §2.2)."""
    # The durable opt-out set (`state.opted_out`) drives both eligibility and render in
    # Pass A and Pass B (20 §6.2); it is threaded in from the sync driver.
    opted_out = set(opted_out or ())

    cache: dict[str, str] | None = users_cache

    def names() -> NameResolver:
        nonlocal cache
        if cache is None:
            cache = _build_name_cache(slack)
        return NameResolver(cache, config.roster)

    # Dedup/revision are derived from the channels posted to, never from state: index the
    # snipe_digest messages seen in step 3 by (channel, period_key), keeping the latest
    # revision when a period appears more than once.
    posted: dict[tuple[str, str], PostedDigest] = {}
    for d in digests:
        key = (d.channel, d.period_key)
        prev = posted.get(key)
        if prev is None or d.revision > prev.revision:
            posted[key] = PostedDigest(
                channel=d.channel, ts=d.ts, report=d.report,
                period_key=d.period_key, semester=d.semester,
                numbers_hash=d.numbers_hash, revision=d.revision,
            )

    posted_count = 0
    revised_count = 0

    # Pass A: post the most-recent-due period per report, once per (channel, period_key).
    for report in config.reports:
        target = report.post_to or config.channel
        if target in skip_channels:
            continue                       # unreadable post_to: no dedup possible (E-W4-34)
        due = most_recent_due(report, now_us, config.tz, config.semesters)
        if due is None or now_us - due.anchor_us >= H24_US:
            continue                       # nothing due, or past the 24 h window
        if (target, due.period_key) in posted:
            continue                       # already present; a numbers change is Pass B's job
        sem = _semester_named(config, due.semester)
        if sem is None:
            continue
        elig = eligible_snipes(ledger, config.rules, config.roster, opted_out,
                               config.semesters, config.tz, sem)
        rendered = render_digest(report, due.period_key, sem, elig, config.roster,
                                 opted_out, names(), config.tz,
                                 revision=0, selfie_emoji=config.feedback.selfie)
        bkey = f"{target}:{due.period_key}"
        boundary("digest:before", bkey)
        slack.post_message(target, text=rendered.text, blocks=list(rendered.blocks),
                           metadata=rendered.metadata.to_wire(target))
        boundary("digest:after", bkey)
        _log("INFO", "digest post", channel=target, period=due.period_key, revision=0)
        posted_count += 1

    # Pass B: revise changed in-window digests (past-window digests are never revised).
    for p in posted.values():
        if parse_ts(p.ts) < scan_floor_us:
            continue
        if p.channel in skip_channels:
            continue
        rpt = next((r for r in config.reports if r.name == p.report), None)
        if rpt is None:
            continue
        if not _period_key_fits(rpt, p.period_key, p.semester):
            # Minted under the report's older cadence: skipped, never an error (E-W4-12).
            _log("WARN", "digest_skipped", channel=p.channel, reason="cadence")
            continue
        sem = _semester_named(config, p.semester)
        if sem is None:
            continue
        elig = eligible_snipes(ledger, config.rules, config.roster, opted_out,
                               config.semesters, config.tz, sem)
        rendered = render_digest(rpt, p.period_key, sem, elig, config.roster,
                                 opted_out, names(), config.tz,
                                 revision=p.revision + 1, selfie_emoji=config.feedback.selfie)
        if rendered.metadata.numbers_hash == p.numbers_hash:
            continue                       # unchanged -> no chat.update (prevents update loops)
        bkey = f"{p.channel}:{p.period_key}"
        boundary("digest:before", bkey)
        slack.update_message(p.channel, p.ts, text=rendered.text,
                             blocks=list(rendered.blocks),
                             metadata=rendered.metadata.to_wire(p.channel))
        boundary("digest:after", bkey)
        _log("INFO", "digest revise", channel=p.channel, period=p.period_key,
             revision=p.revision + 1)
        revised_count += 1

    return posted_count, revised_count


# --- 10. The state machine (20 §2) -------------------------------------------

def _local_day(now_us: int, tz) -> str:
    return (
        datetime.fromtimestamp(now_us // US_PER_SECOND, tz=timezone.utc)
        .astimezone(tz)
        .strftime("%Y-%m-%d")
    )


# The fixed per-Status/Reason delta lines of the §8.4 commit body, in block order.
_COMMIT_DELTA_ORDER: tuple[str, ...] = ("counted", "cooldown", "deleted", "selfie", "repost")


def _verdict_reason_counts(verdicts) -> dict[str, int]:
    """Per-`Status`/`Reason` message counts for the §8.4 commit body (counts only)."""
    counts = {name: 0 for name in _COMMIT_DELTA_ORDER}
    for mv in verdicts:
        if mv.status == Status.COUNTED:
            counts["counted"] += 1
        elif mv.status == Status.COOLDOWN:
            counts["cooldown"] += 1
        if mv.reason == Reason.DELETED:
            counts["deleted"] += 1
        if mv.reason == Reason.REPOST:
            counts["repost"] += 1
        if mv.selfie == SelfieClass.SELFIE:
            counts["selfie"] += 1
        _count_other_reason(counts, mv.reason.value if mv.reason is not None else None)
    return counts


def _count_other_reason(counts: dict[str, int], reason: str | None) -> None:
    """Count a message-level `reason` outside the five fixed §8.4 lines (the
    `<other changed status/reason>` lines, e.g. `vetoed`, `late_tag`)."""
    if reason is not None and reason not in _COMMIT_DELTA_ORDER:
        counts[reason] = counts.get(reason, 0) + 1


def _reason_deltas(cur: dict[str, int], base: dict[str, int]) -> dict[str, int]:
    """§8.4 deltas over the union of keys, a missing key counting as 0."""
    return {name: cur.get(name, 0) - base.get(name, 0) for name in set(cur) | set(base)}


def _delta_line_names(deltas: dict[str, int]) -> list[str]:
    """The five fixed names in block order, then every other changed name, sorted."""
    return list(_COMMIT_DELTA_ORDER) + sorted(
        n for n in deltas if n not in _COMMIT_DELTA_ORDER and deltas[n] != 0
    )


def _baseline_reason_counts(text: str) -> dict[str, int]:
    """The §8.4 counts from a serialized `verdicts.jsonl` baseline. The message-level `reason`
    field is now serialized (00-data §4), so `deleted`/`repost` are recovered from it; the
    `counted`/`cooldown`/`selfie` counts come from the row `status`/`selfie` fields."""
    import json
    counts = {name: 0 for name in _COMMIT_DELTA_ORDER}
    if not text:
        return counts
    for line in text.split("\n"):
        if not line:
            continue
        obj = json.loads(line)
        if obj.get("status") == Status.COUNTED.value:
            counts["counted"] += 1
        elif obj.get("status") == Status.COOLDOWN.value:
            counts["cooldown"] += 1
        if obj.get("reason") == Reason.DELETED.value:
            counts["deleted"] += 1
        if obj.get("reason") == Reason.REPOST.value:
            counts["repost"] += 1
        if obj.get("selfie") == SelfieClass.SELFIE.value:
            counts["selfie"] += 1
        _count_other_reason(counts, obj.get("reason"))
    return counts


def _baseline_row_ts(text: str) -> set[str]:
    """The distinct row `ts` present in a serialized `verdicts.jsonl` cumulative baseline
    (20 §8.1-8.2). Used to measure the §8.4 `rows +<added> -<dropped>` line against the same
    sealed baseline as the by-reason deltas, so a string of small amends cannot slide its row
    count under the sealed baseline."""
    import json
    out: set[str] = set()
    if not text:
        return out
    for line in text.split("\n"):
        if not line:
            continue
        ts = json.loads(line).get("ts")
        if ts is not None:
            out.add(ts)
    return out


def _commit_message(command: Command, day: str, large_movement: bool,
                    trigger: str, added: int, dropped: int,
                    deltas: dict[str, int] | None = None) -> str:
    header = f"{command.value} {day}"
    if large_movement:
        header += f" [movement:{trigger}]"
    d = deltas or {}
    lines = [header, "", f"rows +{added} -{dropped}"]
    for name in _COMMIT_DELTA_ORDER:
        lines.append(f"{name} {d.get(name, 0):+d}")
    for name in _delta_line_names(d)[len(_COMMIT_DELTA_ORDER):]:
        lines.append(f"{name}: {d[name]:+d}")
    return "\n".join(lines) + "\n"


def _empty_result(exit_code: int, newly_deleted: int = 0) -> SyncResult:
    return SyncResult(
        exit_code=exit_code, ledger_written=False, commit_sha=None,
        newly_deleted=newly_deleted, reactions_added=0, reactions_removed=0,
        digests_posted=0, digests_revised=0,
    )


def run_sync(
    slack: "SlackIO",
    config: "Config",
    *,
    detector: "FaceDetector",
    ledger_path: Path,
    state_path: Path,
    now_us: int,
    command: Command = Command.SYNC,
    dry_run: bool = False,
    no_react: bool = False,
    no_post: bool = False,
    backfill_from_us: int | None = None,
    reevaluate: bool = False,
    accept_deletes: int | None = None,
    selfie_ts: str | None = None,
    selfie_value: bool = True,
    selfie_by: str | None = None,
    veto_ts: str | None = None,
    veto_by: str | None = None,
    veto_remove: bool = False,
    rejoin_user: str | None = None,
) -> SyncResult:
    _boundary("start")

    # Step 0: kill switch (admin commands proceed even while paused).
    if config.enabled is False and command in {Command.SYNC, Command.RUN}:
        return _empty_result(0)
    if config.enabled is False:
        # Admin commands still update the ledger and commit, but NO command writes to
        # Slack while paused: steps 7 and 9 are skipped (20 step 0, E-W4-23).
        no_react = no_post = True

    day = _local_day(now_us, config.tz)
    scan_floor_us = now_us - config.sync.scan_days * DAY_US
    audit: dict[str, list[str]] = {
        "text_blocks_disagree": [], "non_permitted_veto": [], "late_tag": [],
        "ambiguous_selfie": [], "repost": [], "likely_repost": [],
        "first_sight_edited": [], "off_roster": [], "near_cooldown": [],
        "needs_review": [], "files_deleted_after_posting": [],
    }

    # Step 1: load + fingerprint guard.
    try:
        stored_rows = load_ledger(ledger_path)
        state = load_state(state_path)
    except MalformedLedgerError:
        _log("ERROR", "malformed_ledger")
        return _empty_result(4)
    row_ts_us = [parse_ts(r.ts) for r in stored_rows]
    if not reevaluate:
        try:
            from snipebot.config import fingerprint_guard
            # Recompute at the stored `fingerprints_at` when present, else the ledger's
            # newest row (20 §2.4, E-W4-17).
            h_stored = (parse_ts(state.fingerprints_at)
                        if state.fingerprints_at is not None else None)
            fingerprint_guard(config, row_ts_us, state.fingerprints, h_stored)
        except FingerprintGuardError as exc:
            _log("ERROR", "fingerprint_guard")
            print(str(exc), file=sys.stderr)
            return _empty_result(3)
    _boundary("after_load")

    # Step 2: fetch.
    oldest_us = fetch_oldest_us(now_us, state, stored_rows, config, backfill_from_us)
    unreadable_channels: set[str] = set()
    try:
        identity = slack.auth_identity()
        bot_user_id = identity.user_id
        own_bot_id = getattr(identity, "bot_id", "") or ""
        fetched_messages = slack.history(
            config.channel, format_ts(oldest_us), format_ts(now_us)
        )
        extra_messages: list[dict] = []
        seen_channels = {config.channel}
        for rpt in config.reports:
            target = rpt.post_to
            if target is None or target in seen_channels:
                continue
            seen_channels.add(target)
            try:
                found = slack.history(
                    target, format_ts(now_us - config.sync.scan_days * DAY_US),
                    format_ts(now_us),
                )
            except SlackError as exc:
                # An unreadable post_to channel only skips its reports' digests; only the
                # watched channel's fetch aborts the run (20 §3, E-W4-34).
                _log("WARN", "post_to_unreadable", channel=target, error=_error_code(exc))
                unreadable_channels.add(target)
                continue
            for m in found:
                m["_channel"] = target      # step 3 parses each in the channel it was found in
                extra_messages.append(m)
        if oldest_us > scan_floor_us:
            # backfill --from after the scan floor: the watched channel's digests between
            # the scan floor and `oldest_us` still feed already-posted dedup (20 §6.2).
            for m in slack.history(
                config.channel, format_ts(scan_floor_us), format_ts(oldest_us)
            ):
                m["_channel"] = config.channel
                extra_messages.append(m)
    except SlackError:
        _log("ERROR", "fetch_failed")
        return _empty_result(5)
    returned_ts = {m["ts"] for m in fetched_messages}
    raw_by_ts = {m["ts"]: m for m in fetched_messages}
    _log("INFO", "fetch", channel=config.channel, oldest=format_ts(oldest_us),
         returned=len(returned_ts))
    _boundary("after_fetch")

    # Step 3: parse (candidates + digests; parse anomalies -> audit).
    parsed_candidates: list[Candidate] = []
    digests: list[Digest] = []

    def _own_digest(m: dict) -> bool:
        # Only digests this bot posted feed Pass A dedup and Pass B revision; a person
        # posting a snipe_digest through an app's user token is ignored (20 §6.2, E-W4-22).
        return m.get("user") == bot_user_id or (
            bool(own_bot_id) and m.get("bot_id") == own_bot_id)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            for m in fetched_messages:
                result = parse(m, config.channel, bot_user_id)
                if isinstance(result, Candidate):
                    parsed_candidates.append(result)
                elif isinstance(result, Digest) and _own_digest(m):
                    digests.append(result)
            for m in extra_messages:
                ch = m.get("_channel", config.channel)
                result = parse(m, ch, bot_user_id)
                if isinstance(result, Digest) and _own_digest(m):
                    digests.append(result)
        except Exception:
            # 20 §2 step 3: parse errors are impossible on real payloads; a raised
            # parse error fails the run closed (exit 1) with zero side effects (step 3
            # precedes the step-7 write boundary). IDs only reach the log line.
            _log("ERROR", "parse_error")
            return _empty_result(1)
    # ParseAnomaly carries IDs only; a text/blocks mention disagreement is audited, but
    # only for messages step 4 keeps as rows (40 §4.2), so it is gathered here first.
    disagree_ts: list[str] = []
    for w in caught:
        text = str(w.message)
        if "disagree" in text:
            ts_token = next((p[3:] for p in text.split() if p.startswith("ts=")), "")
            disagree_ts.append(ts_token)
    _boundary("after_parse")

    # Step 4: merge + delete breaker.
    merged, newly_deleted = _merge(
        stored_rows, parsed_candidates, returned_ts, oldest_us, scan_floor_us,
        config, now_us,
    )
    audit["text_blocks_disagree"].extend(ts for ts in disagree_ts if ts in merged)
    # A stored row returned only as a non-candidate (a tombstone parent) was treated as
    # absent by merge, so steps 5 and 7 must not observe it either: its facts stay
    # unchanged exactly as an unreturned row's do (20 §4.2, E-W4-4).
    kept_ts = {c.ts for c in parsed_candidates}
    raw_by_ts = {ts: m for ts, m in raw_by_ts.items() if ts in kept_ts}
    if newly_deleted > config.sync.max_deletes_per_run:
        if accept_deletes is None:
            _log("ERROR", "delete_breaker", newly_deleted=newly_deleted,
                 cap=config.sync.max_deletes_per_run, hint="accept-deletes")
            return _empty_result(6, newly_deleted)
        if accept_deletes != newly_deleted:
            _log("ERROR", "accept_deletes_mismatch", given=accept_deletes,
                 newly_deleted=newly_deleted, hint=f"retry-with-{newly_deleted}")
            return _empty_result(7, newly_deleted)
        # release the breaker once; ACCEPT_DELETES is already a large movement.
    _boundary("after_merge")

    # Step 5: consent + faces.
    try:
        # Rows this run inserts for the first time are observed whatever their age (E-W4-11).
        inserted_ts = set(merged) - {r.ts for r in stored_rows}
        _observe_vetoes(merged, raw_by_ts, scan_floor_us, config, slack, audit, inserted_ts)
        _observe_optouts(state, config, slack, now_us)
        _observe_admin_selfie(merged, raw_by_ts, scan_floor_us, config, slack, bot_user_id,
                              inserted_ts, state.opted_out)
    except SlackError as exc:
        # An untolerated SlackError raised by a step-5 reactions_get (a truncated-reaction
        # read hitting RateLimited or a fatal class) fails the run closed exactly like a
        # step-2 fetch failure: exit 5, nothing persisted (step 5 precedes the step-8 write
        # boundary). MessageNotFound is tolerated inside each observer (§5.2). IDs only.
        _log("ERROR", "consent_read_failed", kind=type(exc).__name__)
        return _empty_result(5, newly_deleted)

    if command == Command.SELFIE:
        row = merged.get(selfie_ts) if selfie_ts is not None else None
        if row is None or not _sib_tagged(row, config.roster, state.opted_out):
            _log("ERROR", "selfie_bad_ts")
            return _empty_result(2)
        by = selfie_by or (config.admins[0] if config.admins else "")
        merged[selfie_ts] = replace(
            row,
            selfie_override=SelfieOverride(
                value=selfie_value, by=by, source=VetoSource.CLI
            ),
        )
        _log("INFO", "selfie_override", ts=selfie_ts,
             value=str(selfie_value).lower(), by=by, source="cli")

    # CLI veto/unveto applied inside the write boundary (persisted atomically at step 8
    # only if the step-2 fetch succeeded), so a run that aborts earlier writes nothing.
    if command in (Command.VETO, Command.UNVETO) and veto_ts is not None:
        row = merged.get(veto_ts)
        if row is not None:
            if veto_remove:
                def _keep_cli(v: Veto) -> bool:
                    if v.source != VetoSource.CLI:
                        return True
                    # A given `by` removes only that actor's CLI veto; a None `by`
                    # removes every CLI veto on the row (40 §4.2 veto/unveto).
                    return veto_by is not None and v.by != veto_by
                merged[veto_ts] = replace(
                    row, vetoes=tuple(v for v in row.vetoes if _keep_cli(v))
                )
            else:
                merged[veto_ts] = replace(
                    row, vetoes=row.vetoes + (Veto(by=veto_by, source=VetoSource.CLI),)
                )

    # CLI rejoin: the opt-out removal is applied to durable state here and persisted
    # only in the step-8 write, so an abort in steps 0-6 leaves state.json unchanged.
    if command == Command.REJOIN and rejoin_user is not None:
        state.opted_out.pop(rejoin_user, None)

    faces_fetched = 0
    if not dry_run:
        faces_fetched = _detect_faces(
            merged, fetched_messages, kept_ts, config, slack, detector, state.opted_out
        )
    _boundary("after_faces")
    _boundary("after_consent")

    # Drop rows that are deleted AND out of the scan window (20 §4.4). Evaluation and
    # persistence use the same set, so the verdicts staleness check holds by construction.
    rows_final = [
        merged[ts] for ts in sorted(merged, key=parse_ts)
        if not (merged[ts].deleted and parse_ts(ts) < scan_floor_us)
    ]
    rows_by_ts = {r.ts: r for r in rows_final}

    # Step 6: evaluate.
    opted_out = set(state.opted_out)
    try:
        verdicts = evaluate(
            rows_final, config.rules, config.roster, opted_out,
            config.semesters, config.tz,
        )
    except NoRuleInForceError:
        _log("ERROR", "no_rule_in_force")
        return _empty_result(2)
    for mv in verdicts:
        if mv.selfie == SelfieClass.AMBIGUOUS:
            audit["ambiguous_selfie"].append(mv.ts)
        # Review flag: a COUNTED message tagging review.min_targets or more people is its
        # own L8 audit category (00-data §4, 40 §4). The AMBIGUOUS-selfie review trigger is
        # already surfaced under ambiguous_selfie, so this category is the many-targets one.
        row = rows_by_ts.get(mv.ts)
        if (
            row is not None
            and config.review.min_targets is not None
            and mv.status == Status.COUNTED
            and len(row.targets) >= config.review.min_targets
        ):
            audit["needs_review"].append(mv.ts)
        if mv.reason == Reason.REPOST:
            audit["repost"].append(mv.ts)
        if mv.reason in (Reason.SENDER_OFF_ROSTER, Reason.TARGET_OFF_ROSTER):
            audit["off_roster"].append(mv.ts)
        else:
            # target-off-roster surfaces per pair (like late-tag) on an otherwise-counted row.
            if any(p.reason == Reason.TARGET_OFF_ROSTER for p in mv.pairs):
                audit["off_roster"].append(mv.ts)
        cooldown_us = config.rules.in_force_at(parse_ts(mv.ts)).cooldown.microseconds
        for pair in mv.pairs:
            if pair.reason == Reason.COOLDOWN and pair.blocked_by is not None:
                remaining = cooldown_us - (parse_ts(mv.ts) - parse_ts(pair.blocked_by))
                if 0 <= remaining < 60 * US_PER_SECOND:
                    audit["near_cooldown"].append(mv.ts)
                    break
        for pair in mv.pairs:
            if pair.reason == Reason.LATE_TAG:
                audit["late_tag"].append(mv.ts)
                break

    # Heuristic L8 flags off the merged rows that back the verdicts (00-data §2, 40 §4):
    # same-sender file_sig collisions (a likely repost) and rows first observed already
    # edited (tag assumed present at posting). Counts/IDs only reach the audit list.
    seen_sigs_by_sender: dict[str, set[str]] = {}
    for row in rows_final:
        if row.first_sight_edited:
            audit["first_sight_edited"].append(row.ts)
        # A message posted with an uploaded image whose file was tombstoned after posting:
        # it still carries a file object but has no live media left (40 §4). IDs only.
        if row.has_file_object and row.live_images == 0 and row.live_videos == 0:
            audit["files_deleted_after_posting"].append(row.ts)
        sigs = set(row.file_sigs)
        prev = seen_sigs_by_sender.setdefault(row.sender, set())
        if sigs & prev:
            audit["likely_repost"].append(row.ts)
        prev.update(sigs)
    _boundary("after_evaluate")

    counted = sum(1 for mv in verdicts if mv.status == Status.COUNTED)
    cooldown = sum(1 for mv in verdicts if mv.status == Status.COOLDOWN)
    selfies = sum(1 for mv in verdicts if mv.selfie == SelfieClass.SELFIE)
    review = sum(
        1 for mv in verdicts
        if (row := rows_by_ts.get(mv.ts)) is not None
        and needs_review(mv, row, config.review)
    )

    # Dry run: no reactions, no writes, no posts; print the audit list and exit 0.
    if dry_run:
        _print_audit(audit)
        _log("INFO", "summary", counted=counted, cooldown=cooldown, selfies=selfies,
             faces_fetched=faces_fetched, ambiguous_selfie=len(audit["ambiguous_selfie"]),
             repost=len(audit["repost"]), review=review)
        # Carry the §8.4 absolute verdict counts by reason so `backfill --dry-run` can print
        # them to stdout (the whole point of a dry run); the L8 audit list stays on stderr.
        dry_counts = _verdict_reason_counts(verdicts)
        return SyncResult(
            0, False, None, newly_deleted, 0, 0, 0, 0,
            rows_scanned=len(rows_final), selfies=selfies, faces_fetched=faces_fetched,
            ambiguous_selfie=len(audit["ambiguous_selfie"]), repost=len(audit["repost"]),
            needs_review=review,
            counts_by_reason=tuple(
                f"{name} {dry_counts[name]}" for name in _delta_line_names(dry_counts)
            ),
        )

    # Step 7: reactions. `reactions: false` gates the step for every command, whatever its
    # flags (E-W4-43), so no admin path can react when a deployment wants pure stats.
    reactions_added = reactions_removed = 0
    if not no_react and not config.reactions:
        _log("INFO", "reactions skipped", reason="reactions_off")
    if not no_react and config.reactions:
        # Step 7 never aborts a run: its Slack faults are logged and skipped inside
        # `_converge_reactions`; steps 8 and 9 still run (20 §9.1, E-W4-21).
        reactions_added, reactions_removed = _converge_reactions(
            merged, verdicts, raw_by_ts, scan_floor_us, config, slack, bot_user_id
        )
    _boundary("after_reactions")

    # Step 8: integrity (before any write), then persist (write files, then commit + push).
    try:
        check_integrity(rows_final, verdicts)  # checks 1-4
        fresh = dumps_verdicts(evaluate(
            rows_final, config.rules, config.roster, opted_out, config.semesters, config.tz
        ))
        if dumps_verdicts(verdicts) != fresh:  # check 5: verdicts staleness (20 §7.3)
            raise LedgerIntegrityError("verdicts staleness check failed")
    except LedgerIntegrityError:
        _log("ERROR", "integrity_failed")
        return _empty_result(8)

    # Update durable state: the watermark is `format_ts(now_us)` of this COMPLETE fetch —
    # reaching step 8 means step 2 paged history to exhaustion — not the newest message ts
    # (R1, §3). It advances only when this fetch's `oldest` was at or before the stored
    # watermark (or none was stored): a narrow `backfill --from X` with X after it leaves the
    # stretch before X unobserved, so the watermark stays (E-W4-15). A failed/partial fetch
    # aborts before this point.
    if state.watermark is None or oldest_us <= parse_ts(state.watermark):
        state.watermark = format_ts(now_us)
    # The fingerprints and the H they cover are written together (20 §2.4, E-W4-17).
    h_row = max(rows_final, key=lambda r: parse_ts(r.ts), default=None)
    state.fingerprints = compute_fingerprints(
        config, parse_ts(h_row.ts) if h_row is not None else None)
    state.fingerprints_at = h_row.ts if h_row is not None else None

    _boundary("before_persist")
    verdicts_path = ledger_path.with_name("verdicts.jsonl")
    new_ledger_text = dumps_ledger(rows_final)
    new_verdicts_text = dumps_verdicts(verdicts)
    new_state_text = dumps_state(state)

    def _on_disk_bytes(p: Path) -> bytes | None:
        try:
            return p.read_bytes()
        except FileNotFoundError:
            return None

    files_changed = (
        _on_disk_bytes(ledger_path) != new_ledger_text.encode("utf-8")
        or _on_disk_bytes(verdicts_path) != new_verdicts_text.encode("utf-8")
        or _on_disk_bytes(state_path) != new_state_text.encode("utf-8")
    )

    store = store_for(config, ledger_path.parent)
    baseline = store.baseline_verdicts(day)
    baseline_text = baseline.decode("utf-8") if baseline is not None else ""
    # §8.4 by-reason deltas vs the cumulative sealed baseline; deleted/repost are now
    # recoverable from the baseline verdicts' message-level `reason` field (R5).
    cur_counts = _verdict_reason_counts(verdicts)
    base_counts = _baseline_reason_counts(baseline_text)
    deltas = _reason_deltas(cur_counts, base_counts)

    if not files_changed:
        # No-op run: the three data files are byte-identical to what is already on disk, so
        # there is nothing to persist and nothing to commit — no empty amend (R5). The run
        # moved nothing, so `moved_lines` is empty and `ledger_written` is False.
        commit_sha: str | None = None
        sealed_sha: str | None = None
        ledger_written = False
        moved_lines: tuple[str, ...] = ()
        _log("INFO", "no_change", day=day)
    else:
        prior_verdicts = _on_disk_bytes(verdicts_path)
        save_ledger(ledger_path, rows_final)
        save_verdicts(verdicts_path, verdicts)
        save_state(state_path, state)

        cumulative_flips = count_moved_pairs(baseline_text, new_verdicts_text)
        large_movement = (
            command in LARGE_MOVEMENT_COMMANDS
            or reevaluate
            or cumulative_flips > config.sync.large_movement_rows
        )
        trigger = "admin" if (command in LARGE_MOVEMENT_COMMANDS or reevaluate) else "cumulative"
        # §8.4: a daily commit's counts are relative to the cumulative sealed baseline. A
        # movement commit seals its parent as the restore point, so its counts are what this
        # movement changed relative to that parent (E-W4-37); the tree still holds the tip's
        # files here. The large-movement test above stays cumulative (§8.2).
        if large_movement and config.persistence == Persistence.GIT:
            parent_text = prior_verdicts.decode("utf-8") if prior_verdicts is not None else ""
            deltas = _reason_deltas(cur_counts, _baseline_reason_counts(parent_text))
            baseline_ts = _baseline_row_ts(parent_text)
        else:
            baseline_ts = _baseline_row_ts(baseline_text)
        final_ts = {r.ts for r in rows_final}
        added = sum(1 for ts in final_ts if ts not in baseline_ts)
        dropped = sum(1 for ts in baseline_ts if ts not in final_ts)
        message = _commit_message(command, day, large_movement, trigger, added,
                                  max(dropped, 0), deltas)
        result = store.commit_and_push(
            local_day=day, large_movement=large_movement, message=message, boundary=_boundary,
        )
        commit_sha = result.sha or None
        sealed_sha = result.sealed_sha or None
        ledger_written = True
        # Under persistence: files there is no sealed baseline (it is always empty), so the
        # moved: line is this run's flips against the verdicts file it replaced (40 §4.3).
        line_deltas = deltas
        if config.persistence == Persistence.FILES:
            prior_text = prior_verdicts.decode("utf-8") if prior_verdicts is not None else ""
            line_deltas = _reason_deltas(cur_counts, _baseline_reason_counts(prior_text))
        moved_lines = tuple(
            f"{name} {line_deltas[name]:+d}" for name in _delta_line_names(line_deltas)
            if line_deltas[name] != 0
        )
        _log("INFO", "commit", sha=commit_sha or "none", day=day,
             movement=("large" if large_movement else "cumulative"))
    _boundary("after_persist")

    # Step 9: digests. `recaps: false` gates both passes for every command (20 §6.2,
    # E-W4-39); reactions, the ledger and its commit above are untouched.
    digests_posted = digests_revised = 0
    if not no_post and not config.recaps:
        _log("INFO", "digests skipped", reason="recaps_off")
    elif not no_post:
        try:
            digests_posted, digests_revised = post_digests(
                slack, config, ledger=rows_final, verdicts=verdicts, digests=digests,
                now_us=now_us, users_cache=None, scan_floor_us=scan_floor_us,
                boundary=_boundary, opted_out=set(state.opted_out),
                # passed only when a post_to fetch failed, so an injected hook with the
                # older signature keeps working (E-W4-34)
                **({"skip_channels": frozenset(unreadable_channels)}
                   if unreadable_channels else {}),
            )
        except (DigestTooLargeError, SlackError) as exc:
            # Untolerated step-9 failure (20 §9.1 exit 1). The ledger is already persisted;
            # signal the run failed so the workflow alerts, without corrupting the write.
            _log("ERROR", "digest_failed", kind=type(exc).__name__)
            return SyncResult(
                exit_code=1, ledger_written=ledger_written, commit_sha=commit_sha,
                newly_deleted=newly_deleted, reactions_added=reactions_added,
                reactions_removed=reactions_removed, digests_posted=0,
                digests_revised=0, moved_lines=moved_lines, sealed_sha=sealed_sha,
            )
    _boundary("after_digests")

    _log("INFO", "summary", counted=counted, cooldown=cooldown, selfies=selfies,
         faces_fetched=faces_fetched, ambiguous_selfie=len(audit["ambiguous_selfie"]),
         repost=len(audit["repost"]), review=review)
    _boundary("done")
    return SyncResult(
        exit_code=0, ledger_written=ledger_written, commit_sha=commit_sha,
        newly_deleted=newly_deleted, reactions_added=reactions_added,
        reactions_removed=reactions_removed, digests_posted=digests_posted,
        digests_revised=digests_revised, moved_lines=moved_lines, sealed_sha=sealed_sha,
        rows_scanned=len(rows_final), selfies=selfies, faces_fetched=faces_fetched,
        ambiguous_selfie=len(audit["ambiguous_selfie"]), repost=len(audit["repost"]),
        needs_review=review, breaker_released=accept_deletes is not None,
        # 40 §4.2 veto/unveto "the message's new verdict": the target row's dominant
        # message-level reason (VETOED after a CLI veto, 00-data §4; the counted/cooldown/…
        # reason after an unveto), which reason_status maps to the message-level Status.
        target_verdict=(
            next((mv.reason.value for mv in verdicts if mv.ts == veto_ts), None)
            if veto_ts is not None else None
        ),
    )


def _print_audit(audit: dict[str, list[str]]) -> None:
    print("AUDIT", file=sys.stderr)
    for category, entries in audit.items():
        for ts in entries:
            print(f"AUDIT {category} ts={ts}", file=sys.stderr)


# --- 11. Lease-retry wrapper (persistence: git, 20 §1.4) ----------------------

def run_sync_git(
    slack: "SlackIO",
    config: "Config",
    *,
    detector: "FaceDetector",
    ledger_path: Path,
    state_path: Path,
    now_us: int,
    command: Command = Command.SYNC,
    now_fn: Callable[[], int] | None = None,
    **flags: Any,
) -> SyncResult:
    """Run `run_sync`; on a rejected `--force-with-lease` push, refresh to the new data-branch
    tip and re-run the whole sync with a fresh `now_us`. Bounded to `MAX_LEASE_RETRIES` total
    attempts; exhausting them exits 9 (20 §8.3 step 6)."""
    # §8.3 step 6: every re-run carries a FRESH now_us — never the stale attempt_now.
    # A caller (the CLI) injects `now_fn` = the real wall clock; with no injection the
    # default is a strictly-increasing clock seeded from `now_us`, so a re-run's now_us is
    # always fresh (never reused) yet deterministic for harnesses that freeze the instant.
    if now_fn is None:
        _tick = now_us

        def now_fn() -> int:
            nonlocal _tick
            _tick += 1
            return _tick

    store = store_for(config, ledger_path.parent)
    store.refresh()
    attempt_now = now_us
    for attempt in range(1, MAX_LEASE_RETRIES + 1):
        try:
            return run_sync(
                slack, config, detector=detector, ledger_path=ledger_path,
                state_path=state_path, now_us=attempt_now, command=command, **flags,
            )
        except LeaseRejected:
            _log("WARN", "lease_rejected", attempt=attempt, of=MAX_LEASE_RETRIES)
            store.refresh()
            attempt_now = now_fn()
    return _empty_result(9)
