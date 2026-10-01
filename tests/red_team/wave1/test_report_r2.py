"""Round 2 adversarial spec-conformance probes for ``snipebot.report`` — hostile and
degenerate inputs (empty tables, no activity).

Each test builds inputs by hand (a ``Roster``, an ``Eligibility`` of ``EligibleSnipe``
rows, a ``ReportSpec``) and attacks one behaviour against the exact wording of
``spec/30-aggregate-report.md``. A test here is expected to FAIL: its docstring quotes
the spec sentence the expectation rests on, and the current code disagrees. Scaffolding
mirrors ``tests/_helpers_digest.py`` so a reviewer can diff.
"""

from __future__ import annotations

from snipebot.aggregate import Eligibility
from snipebot.config import Cadence, Roster, RosterEntry, Section
from snipebot.report import NameResolver, render_digest

from tests._helpers_digest import SEMESTER, TZ, report_of


def _groups_text(render) -> str:
    for b in render.blocks:
        if b.get("type") == "section":
            t = b["text"]["text"]
            if t.startswith("*Groups*"):
                return t
    raise AssertionError("no groups block found")


# --------------------------------------------------------------------------- #
# report.py — the groups digest section is EMPTY when no group has any snipe
# --------------------------------------------------------------------------- #

def test_groups_section_empty_when_every_group_has_zero_counted_snipes():
    """30 section 5.4 (line 824-825): "**Empty ranked section:** title line then
    ``_No snipes yet._`` (e.g. ``groups`` when every group has zero counted snipes)."

    Two real sibling groups exist with rostered, non-opted-out members, but the period
    (indeed the whole semester) has zero counted snipes. Per the sentence above the
    ``groups`` section must collapse to the empty-section form ``*Groups*\\n_No snipes
    yet._``, exactly as ``top_snipers``/``most_sniped``/``pairs`` do when there is no
    activity. The current renderer instead emits a ``0.00 pts/member (0 pts, 0/N made)``
    row for every members>0 group (its ``_No snipes yet._`` branch fires only when the
    ranked window is empty, and a members>0 group keeps a window row regardless of
    whether any snipe touches it), so a heartbeat digest early in a semester shows a
    wall of zero-point group rows the spec says should read "no snipes yet".
    """
    roster = Roster(
        entries={
            "U1": RosterEntry(user="U1", join_us=0, group="sibA", is_bot=False),
            "U2": RosterEntry(user="U2", join_us=0, group="sibB", is_bot=False),
        },
        count_intra_group=True,
    )
    elig = Eligibility(semester=SEMESTER, snipes=(), rejections=())
    report = report_of((Section.GROUPS,), cadence=Cadence.DAILY, top_n=5)
    render = render_digest(
        report=report,
        period_key="daily:2026-09-18",
        semester=SEMESTER,
        elig=elig,
        roster=roster,
        opted_out=frozenset(),
        names=NameResolver({"U1": "Alice", "U2": "Bob"}, roster),
        tz=TZ,
        revision=0,
        selfie_emoji=None,
    )
    assert _groups_text(render) == "*Groups*\n_No snipes yet._"
