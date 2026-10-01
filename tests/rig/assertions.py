"""Read-back assertions for the L6 rig (50-test-matrix.md section 7.3).

Every helper judges a *read-back* value against an expectation and raises
``RigAssertionError`` (an ``AssertionError`` subclass) with an ID-only message on a
mismatch, or returns ``None`` on success. The read-back values are plain dicts and
lists exactly as the bot token returns them, so these helpers are pure and testable
offline against canned dicts:

- ledger rows      -- a list of row dicts (``ts``/``status``/``blocked_by``/``selfie``),
- reactions        -- a ``reactions.get`` message dict (``{"reactions": [{"name",
                      "users", "count"}, ...]}``); the bot's own reactions are the
                      ones whose ``users`` include ``bot_user_id``,
- digests          -- channel messages carrying Slack ``metadata``
                      (``{"event_type": "snipe_digest", "event_payload": {...}}``),
- parity           -- two parsed ``Candidate``-shaped values (``targets`` /
                      ``live_images`` / ``file_sigs``),
- group points     -- an integer rebuilt from the read-back tables.

The assertions never look at the local ledger alone: the caller feeds them what the
API actually returned (the reactions actually on the message, the digest actually in
the channel), which is what makes the rig a real round-trip test rather than a
restatement of the ledger fact. Nothing here prints a token.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

DIGEST_EVENT_TYPE = "snipe_digest"


class RigAssertionError(AssertionError):
    """A rig read-back did not match the expected shipped behaviour."""


# --------------------------------------------------------------------------- #
# Ledger rows (read back from the stored ledger the sync wrote).
# --------------------------------------------------------------------------- #

def find_row(rows: Sequence[Mapping[str, Any]], ts: str) -> Mapping[str, Any]:
    """The single ledger row at ``ts``. Raises if absent or duplicated."""
    matches = [r for r in rows if r.get("ts") == ts]
    if not matches:
        raise RigAssertionError(f"no ledger row at ts {ts}")
    if len(matches) > 1:
        raise RigAssertionError(f"{len(matches)} ledger rows share ts {ts}")
    return matches[0]


def assert_status(rows: Sequence[Mapping[str, Any]], ts: str, expected: str) -> None:
    """The row at ``ts`` carries verdict ``expected`` (e.g. ``COUNTED``)."""
    row = find_row(rows, ts)
    actual = row.get("status")
    if actual != expected:
        raise RigAssertionError(
            f"row {ts}: status {actual!r}, expected {expected!r}"
        )


def assert_blocked_by(rows: Sequence[Mapping[str, Any]], ts: str, expected: str | None) -> None:
    """The row at ``ts`` is blocked by ``expected`` (an earlier ts, or ``None``)."""
    row = find_row(rows, ts)
    actual = row.get("blocked_by")
    if actual != expected:
        raise RigAssertionError(
            f"row {ts}: blocked_by {actual!r}, expected {expected!r}"
        )


def assert_selfie(rows: Sequence[Mapping[str, Any]], ts: str, expected: str) -> None:
    """The row at ``ts`` carries selfie class ``expected`` (e.g. ``SELFIE``/``SNIPE``)."""
    row = find_row(rows, ts)
    actual = row.get("selfie")
    if actual != expected:
        raise RigAssertionError(
            f"row {ts}: selfie {actual!r}, expected {expected!r}"
        )


# --------------------------------------------------------------------------- #
# Reactions actually on the message (reactions.get, full users list).
# --------------------------------------------------------------------------- #

def bot_reactions(message: Mapping[str, Any], bot_user_id: str) -> set[str]:
    """The set of emoji the bot itself has placed on the message.

    A reaction counts as the bot's iff ``bot_user_id`` is in its ``users`` list --
    the same rule the sync's convergence uses, so a reaction from a human (the R5
    veto, an admin's confirming selfie) is never read as the bot's own.
    """
    out: set[str] = set()
    for reaction in message.get("reactions", []):
        if bot_user_id in reaction.get("users", []):
            out.add(reaction["name"])
    return out


def reactors(message: Mapping[str, Any], emoji: str) -> set[str]:
    """Every user who reacted with ``emoji`` (used for the R5 human veto check)."""
    out: set[str] = set()
    for reaction in message.get("reactions", []):
        if reaction.get("name") == emoji:
            out.update(reaction.get("users", []))
    return out


def assert_reaction_present(message: Mapping[str, Any], bot_user_id: str, emoji: str) -> None:
    """The bot has placed ``emoji`` on the message (R1 counted, R12 selfie)."""
    have = bot_reactions(message, bot_user_id)
    if emoji not in have:
        raise RigAssertionError(
            f"bot reaction {emoji!r} missing; bot has {sorted(have)}"
        )


def assert_reaction_absent(message: Mapping[str, Any], bot_user_id: str, emoji: str) -> None:
    """The bot has NOT placed ``emoji`` (R5 counted removed, R13 selfie removed)."""
    have = bot_reactions(message, bot_user_id)
    if emoji in have:
        raise RigAssertionError(
            f"bot reaction {emoji!r} present but should be absent; bot has {sorted(have)}"
        )


def assert_bot_reactions_exact(
    message: Mapping[str, Any], bot_user_id: str, expected: Iterable[str]
) -> None:
    """The bot's reactions are EXACTLY ``expected`` -- neither missing nor extra.

    Catches both a dropped emoji (a missing 🤳 after R12) and a stray one (a bot
    reaction the convergence should have removed), which a present-only check would
    let through.
    """
    have = bot_reactions(message, bot_user_id)
    want = set(expected)
    if have != want:
        missing = sorted(want - have)
        extra = sorted(have - want)
        raise RigAssertionError(
            f"bot reactions {sorted(have)} != expected {sorted(want)} "
            f"(missing {missing}, extra {extra})"
        )


# --------------------------------------------------------------------------- #
# The digest actually posted in the channel, and its metadata key.
# --------------------------------------------------------------------------- #

def _payload(message: Mapping[str, Any]) -> Mapping[str, Any] | None:
    meta = message.get("metadata")
    if not isinstance(meta, Mapping):
        return None
    if meta.get("event_type") != DIGEST_EVENT_TYPE:
        return None
    payload = meta.get("event_payload")
    return payload if isinstance(payload, Mapping) else None


def digests_for(messages: Sequence[Mapping[str, Any]], period_key: str) -> list[Mapping[str, Any]]:
    """Every ``snipe_digest`` message in the channel for ``period_key``."""
    out = []
    for message in messages:
        payload = _payload(message)
        if payload is not None and payload.get("period_key") == period_key:
            out.append(message)
    return out


def assert_one_digest(
    messages: Sequence[Mapping[str, Any]], period_key: str
) -> Mapping[str, Any]:
    """Exactly one digest for ``period_key`` is in the channel (R7/R8/R9)."""
    found = digests_for(messages, period_key)
    if len(found) != 1:
        raise RigAssertionError(
            f"expected exactly one digest for {period_key!r}, found {len(found)}"
        )
    return found[0]


def assert_no_digest(messages: Sequence[Mapping[str, Any]], period_key: str) -> None:
    """No digest for ``period_key`` is in the channel (R8: none duplicated in C_MAIN)."""
    found = digests_for(messages, period_key)
    if found:
        raise RigAssertionError(
            f"expected no digest for {period_key!r}, found {len(found)}"
        )


def assert_digest_metadata(
    message: Mapping[str, Any],
    *,
    report: str,
    period_key: str,
    channel: str,
    semester: str,
    numbers_hash: str,
    revision: int,
) -> None:
    """The digest's metadata event key matches, field by field (R7/R9).

    Checks ``report``/``period_key``/``channel``/``semester``/``numbers_hash``/
    ``revision`` against ``event_payload``; a wrong ``numbers_hash`` (a digest that
    was not re-rendered for a number change) or a stale ``revision`` fails here.
    """
    payload = _payload(message)
    if payload is None:
        raise RigAssertionError("message carries no snipe_digest metadata")
    expected = {
        "report": report,
        "period_key": period_key,
        "channel": channel,
        "semester": semester,
        "numbers_hash": numbers_hash,
        "revision": revision,
    }
    for key, want in expected.items():
        got = payload.get(key)
        if got != want:
            raise RigAssertionError(
                f"digest metadata {key}: {got!r}, expected {want!r}"
            )


def assert_post_to(
    main_messages: Sequence[Mapping[str, Any]],
    off_messages: Sequence[Mapping[str, Any]],
    period_key: str,
) -> Mapping[str, Any]:
    """R8: the post_to report's digest lands in C_OFF only, none in C_MAIN.

    Dedup is per ``(channel, period_key)``: the same period key may key a digest in
    C_OFF without duplicating one in the watched channel.
    """
    assert_no_digest(main_messages, period_key)
    return assert_one_digest(off_messages, period_key)


# --------------------------------------------------------------------------- #
# R11 parity: API-posted vs phone-posted captures must parse the same.
# --------------------------------------------------------------------------- #

def assert_parity(candidate_api: Any, candidate_phone: Any) -> None:
    """The two captures ``parse`` to the same candidate (R11, the L6->G2 bridge).

    Compares ``targets`` and ``live_images`` exactly and ``file_sigs`` modulo file
    identity (the same number of live uploads; the raw signature carries the file's
    own name/size, which differs between two uploads of the same photo). A mismatch
    means the API upload is not a faithful stand-in for a phone payload, and the rig
    reports itself invalid rather than passing.
    """
    a_targets = tuple(getattr(candidate_api, "targets"))
    p_targets = tuple(getattr(candidate_phone, "targets"))
    if a_targets != p_targets:
        raise RigAssertionError(
            f"parity: targets diverge -- API {a_targets} vs phone {p_targets}"
        )
    a_live = getattr(candidate_api, "live_images")
    p_live = getattr(candidate_phone, "live_images")
    if a_live != p_live:
        raise RigAssertionError(
            f"parity: live_images diverge -- API {a_live} vs phone {p_live}"
        )
    a_sigs = len(tuple(getattr(candidate_api, "file_sigs")))
    p_sigs = len(tuple(getattr(candidate_phone, "file_sigs")))
    if a_sigs != p_sigs:
        raise RigAssertionError(
            f"parity: file_sig count diverges -- API {a_sigs} vs phone {p_sigs}"
        )


# --------------------------------------------------------------------------- #
# Group points, rebuilt from the read-back tables (R12 == 2, R13 == 1).
# --------------------------------------------------------------------------- #

def assert_group_points(points: int, expected: int) -> None:
    """The sibling group's points, rebuilt from the API read-back, equal ``expected``."""
    if points != expected:
        raise RigAssertionError(
            f"sibling group points {points}, expected {expected}"
        )
