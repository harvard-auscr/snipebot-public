"""Round 3 adversarial invariant/interaction probes for ``snipebot.report``.

Round 3 hunts for properties that must hold across the parse -> rules -> aggregate ->
report pipeline: conservation, and the empty-section contract when the pure aggregate
layer feeds the renderer.

Each test builds its inputs by hand through the real ``eligible_snipes`` boundary (so
the aggregate rows are genuine, not stubbed) and attacks one behaviour against the exact
wording of ``spec/30-aggregate-report.md``. A test here is expected to FAIL: its
docstring quotes the spec sentence the expectation rests on, and the current code
disagrees. Scaffolding mirrors ``tests/_helpers_digest.py`` so a reviewer can diff.
"""

from __future__ import annotations

from snipebot.aggregate import eligible_snipes
from snipebot.config import Cadence, Section
from snipebot.report import NameResolver, render_digest

from tests._helpers_digest import (
    SEMESTER,
    TZ,
    cand,
    dated,
    mkts,
    report_of,
    roster_of,
    rule,
)


def _groups_text(render) -> str:
    for b in render.blocks:
        if b.get("type") == "section":
            t = b["text"]["text"]
            if t.startswith("*Groups*"):
                return t
    raise AssertionError("no groups block found")


# --------------------------------------------------------------------------- #
# report.py -- the groups empty-form must not fire for a group a snipe TOUCHES
# --------------------------------------------------------------------------- #

def test_groups_empty_form_fires_when_a_real_group_was_only_sniped():
    """30 section 5.4 (line 824-825): "**Empty ranked section:** title line then
    ``_No snipes yet._`` (e.g. ``groups`` when **every group has zero counted
    snipes**)."

    An ungrouped player (``U1``) lands one plain COUNTED snipe on ``U2``, the sole
    member of real group ``sibA``. That COUNTED pair touches ``sibA`` (it is one of
    ``sibA``'s ``sniped`` snipes, 30 section 2.4), so ``sibA`` does **not** have "zero
    counted snipes" -- the empty-form's own stated condition is not met, and the
    renderer's own comment scopes the collapse to "when no snipe touches it". ``sibA``
    has members>0 and a counted snipe touching it, so per the sentence above the
    ``groups`` section must render its ranked row
    ``1. sibA - 0.00 pts/member (0 pts, 0/1 made)`` (the point from that plain snipe
    accrues to the ungrouped sniper, so ``sibA``'s ``points`` is 0).

    The renderer instead collapses the whole section to ``*Groups*\\n_No snipes yet._``
    because its empty test is ``all(r.points == 0 for r in window)`` -- a points-only
    proxy that mis-fires for a group that was sniped but earned no points, so a digest
    reports "no snipes yet" for a semester in which a real group was in fact sniped.
    """
    roster = roster_of({"U1": None, "U2": "sibA"})
    ledger = [cand(mkts(2026, 9, 18, 9, 0, 0), "U1", ("U2",))]
    elig = eligible_snipes(ledger, dated(rule()), roster, frozenset(),
                           (SEMESTER,), TZ, SEMESTER)
    report = report_of((Section.GROUPS,), cadence=Cadence.DAILY, top_n=5)
    render = render_digest(
        report=report,
        period_key="daily:2026-09-18",
        semester=SEMESTER,
        elig=elig,
        roster=roster,
        opted_out=frozenset(),
        names=NameResolver({"U1": "Ungrouped One", "U2": "Rostered Two"}, roster),
        tz=TZ,
        revision=0,
        selfie_emoji=None,
    )
    assert _groups_text(render) == (
        "*Groups*\n1. sibA — 0.00 pts/member (0 pts, 0/1 made)"
    )
