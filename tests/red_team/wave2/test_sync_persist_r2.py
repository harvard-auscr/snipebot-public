"""Red-team wave 2, round 2 (HOSTILE INPUT / FAILURE INJECTION) against sync steps 8-10,
`post_digests`, and how sync drives `persistence.py` / `ledger.py`.

Each test reproduces one spec violation and FAILS on the current code. A test that passes
would not be a finding and is deleted.
"""

from __future__ import annotations

import pytest

from snipebot.parse import Candidate
from snipebot.rules import MessageVerdict, PairVerdict, Reason, SelfieClass, Status
from snipebot.ledger import LedgerIntegrityError, check_integrity


def _cand(ts: str, *, targets: tuple[str, ...] = ("U0B",)) -> Candidate:
    """A minimal well-formed ledger row at `ts`. `check_integrity` reads only the ts
    (for check 2's ordering/uniqueness and check 3's `blocked_by in row_ts` test)."""
    return Candidate(
        ts=ts, sender="U0A", subtype=None, thread_ts=None, targets=targets,
        live_images=0, live_image_ids=(), live_videos=0, linked_images=0,
        last_edit_ts=None, file_sigs=(), vetoes=(), missing_runs=0,
        first_seen_targets=frozenset(targets), first_sight_edited=False,
        target_edited_in=(),
    )


# --------------------------------------------------------------------------- Finding


def test_check3_accepts_cooldown_blocked_by_a_non_counted_row(tmp_path):
    """20 §7.3 integrity check 3 (`blocked_by points at the attempt that set the cooldown
    anchor`) requires, for every COOLDOWN pair, that "the row at `blocked_by` is the cooldown
    anchor in the same scope: a COUNTED pair, or -- where `rejected_attempts_reset` was in
    force at the anchoring attempt's ts -- any earlier attempt whose rejection advanced the
    anchor".

    `ledger.check_integrity` (the sole implementation of checks 2-3, run at step 8 before any
    write and by `doctor` on the durable files, 20 §7.3) verifies only that `blocked_by` is
    NOT None, is strictly earlier than the pair's ts, and names some ledger row. It never
    checks that the referenced row is actually a COUNTED anchor (or a reset attempt). So a
    COOLDOWN pair whose `blocked_by` points at an earlier row that carries only a NOT_COUNTED
    pair -- a corrupt or hand-edited `verdicts.jsonl`, the "fail closed on bad data" case --
    passes the check unflagged, where the spec requires `LedgerIntegrityError`.
    """
    A = "1000.000001"          # earlier row: NOT an anchor (its pair is NOT_COUNTED)
    B = "1000.000002"          # later row: a COOLDOWN pair pointing its blocked_by at A
    rows = [_cand(A), _cand(B)]

    verdict_a = MessageVerdict(
        ts=A, status=Status.NOT_COUNTED, reason=Reason.LATE_TAG,
        selfie=SelfieClass.NOT_APPLICABLE,
        pairs=(PairVerdict(ts=A, target="U0B", status=Status.NOT_COUNTED,
                           reason=Reason.LATE_TAG, blocked_by=None, selfie=False),),
    )
    verdict_b = MessageVerdict(
        ts=B, status=Status.COOLDOWN, reason=Reason.COOLDOWN,
        selfie=SelfieClass.NOT_APPLICABLE,
        pairs=(PairVerdict(ts=B, target="U0B", status=Status.COOLDOWN,
                           reason=Reason.COOLDOWN, blocked_by=A, selfie=False),),
    )

    # §7.3 check 3 mandates a raise here: A is not a COUNTED (or reset) anchor for B's scope.
    with pytest.raises(LedgerIntegrityError):
        check_integrity(rows, [verdict_a, verdict_b])
