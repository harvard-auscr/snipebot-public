"""L7-GO: golden block-list snapshots and the numbers-hash contract (30 sections 5, 8).

Each scenario builds a fixed ledger, renders it, and asserts byte-exact equality with
its stored golden plus the Block Kit limits (30 section 5.5). The metadata test pins the
canonical numbers_hash literal and proves a name-only change never moves it while a
points change (a selfie award) does.
"""

from __future__ import annotations

import pytest

from snipebot.aggregate import eligible_snipes
from snipebot.config import Cadence, Section, Weekday
from snipebot.report import NameResolver, numbers_payload, render_digest
from tests._helpers_digest import (
    ALL_SECTIONS,
    SEMESTER,
    TZ,
    assert_limits,
    cand,
    check_golden,
    dated,
    mkts,
    report_of,
    roster_of,
    rule,
    selfie,
)

# The one rule variant every scenario uses: the sibfam selfie bonus is in force, so a
# SELFIE-classed pair awards its photo + participation points.
DR = dated(rule(selfie_bonus=True))


def name40(label: str) -> str:
    """A display name padded to exactly the 40-char Slack worst case."""
    padded = (label + " " + "x" * 40)[:40]
    assert len(padded) == 40
    return padded


def _render(kwargs):
    return render_digest(**kwargs)


def _elig(ledger, roster, opted_out=frozenset()):
    return eligible_snipes(ledger, DR, roster, opted_out, (SEMESTER,), TZ, SEMESTER)


def _kwargs(report, period_key, elig, roster, cache, *, opted_out=frozenset(),
            revision=0, selfie_emoji="selfie"):
    return dict(
        report=report,
        period_key=period_key,
        semester=SEMESTER,
        elig=elig,
        roster=roster,
        opted_out=opted_out,
        names=NameResolver(cache, roster),
        tz=TZ,
        revision=revision,
        selfie_emoji=selfie_emoji,
    )


# --------------------------------------------------------------------------- #
# Scenario builders (each returns render_digest kwargs)
# --------------------------------------------------------------------------- #

def scn_names_40char():
    roster = roster_of({"p0": "sibA", "p1": "sibA", "p2": "sibB", "p3": "sibB"})
    cache = {
        "p0": name40("Aurelius"), "p1": name40("Bernadette"),
        "p2": name40("Cassiopeia"), "p3": name40("Demetrius"),
    }
    ledger = [
        cand(mkts(2026, 9, 18, 10, 0, 0), "p0", ("p2",)),
        cand(mkts(2026, 9, 18, 11, 0, 0), "p0", ("p3",)),
        cand(mkts(2026, 9, 18, 12, 0, 0), "p1", ("p2",)),
        cand(mkts(2026, 9, 18, 13, 0, 0), "p2", ("p0",)),
    ]
    report = report_of(ALL_SECTIONS)
    return _kwargs(report, "daily:2026-09-18", _elig(ledger, roster), roster, cache)


def scn_tie_overflow():
    # 60 snipers each land exactly one snipe on a common victim: all tied at 1 point.
    groups = {"victim": "sibV"}
    cache = {"victim": name40("Victim")}
    ledger = []
    for i in range(60):
        u = f"s{i:02d}"
        groups[u] = "sibS"
        cache[u] = name40(f"Sniper{i:02d}")
        ledger.append(cand(mkts(2026, 9, 18, 8, 0, i), u, ("victim",)))
    roster = roster_of(groups)
    report = report_of((Section.DAY, Section.TOP_SNIPERS), top_n=5)
    return _kwargs(report, "daily:2026-09-18", _elig(ledger, roster), roster, cache)


def scn_most_sniped_full_roster():
    # 24 rostered players in a ring: each snipes the next, so every player is sniped once.
    groups = {}
    cache = {}
    ledger = []
    n = 24
    for i in range(n):
        u = f"u{i:02d}"
        groups[u] = "sibA" if i % 2 == 0 else "sibB"
        cache[u] = f"Member {i:02d}"
    for i in range(n):
        src = f"u{i:02d}"
        dst = f"u{(i + 1) % n:02d}"
        ledger.append(cand(mkts(2026, 9, 18, 8, i, 0), src, (dst,)))
    roster = roster_of(groups)
    report = report_of((Section.DAY, Section.MOST_SNIPED), top_n=100)
    return _kwargs(report, "daily:2026-09-18", _elig(ledger, roster), roster, cache)


def scn_empty_day():
    roster = roster_of({"a1": "sibA", "b1": "sibB"})
    cache = {"a1": "Alice", "b1": "Bob"}
    report = report_of((Section.DAY,))
    return _kwargs(report, "daily:2026-09-18", _elig([], roster), roster, cache)


def scn_group_of_one():
    roster = roster_of({"z0": "solo", "a1": "sibA", "a2": "sibA"})
    cache = {"z0": "Solo Ranger", "a1": "Alice", "a2": "Bob"}
    ledger = [
        cand(mkts(2026, 9, 18, 9, 0, 0), "z0", ("a1",)),
        cand(mkts(2026, 9, 18, 10, 0, 0), "z0", ("a2",)),
        cand(mkts(2026, 9, 18, 11, 0, 0), "a1", ("z0",)),
    ]
    report = report_of((Section.DAY, Section.GROUPS))
    return _kwargs(report, "daily:2026-09-18", _elig(ledger, roster), roster, cache)


def scn_opted_out_user():
    roster = roster_of({"opt": "sibA", "a1": "sibA", "b1": "sibB"})
    cache = {"opt": "OptedOutPerson", "a1": "Alice", "b1": "Bob"}
    ledger = [
        cand(mkts(2026, 9, 18, 9, 0, 0), "opt", ("b1",)),
        cand(mkts(2026, 9, 18, 10, 0, 0), "a1", ("opt",)),
        cand(mkts(2026, 9, 18, 11, 0, 0), "a1", ("b1",)),
        cand(mkts(2026, 9, 18, 12, 0, 0), "b1", ("a1",)),
    ]
    elig = _elig(ledger, roster, opted_out=frozenset({"opt"}))
    report = report_of(ALL_SECTIONS)
    return _kwargs(report, "daily:2026-09-18", elig, roster, cache,
                   opted_out=frozenset({"opt"}))


def scn_duplicate_name():
    roster = roster_of({"a1": "sibA", "b1": "sibB", "c1": "sibC"})
    cache = {"a1": "Sam Rivers", "b1": "Sam Rivers", "c1": "Riley Quinn"}
    ledger = [
        cand(mkts(2026, 9, 18, 9, 0, 0), "a1", ("c1",)),
        cand(mkts(2026, 9, 18, 10, 0, 0), "b1", ("c1",)),
        cand(mkts(2026, 9, 18, 11, 0, 0), "c1", ("a1",)),
    ]
    report = report_of((Section.DAY, Section.TOP_SNIPERS, Section.MOST_SNIPED))
    return _kwargs(report, "daily:2026-09-18", _elig(ledger, roster), roster, cache)


def scn_emoji_in_names():
    roster = roster_of({"e1": "sibA", "a1": "sibA"})
    cache = {"e1": "Zoë 🎯 <b&d>", "a1": "Alice"}
    ledger = [
        cand(mkts(2026, 9, 18, 9, 0, 0), "e1", ("a1",)),
        cand(mkts(2026, 9, 18, 10, 0, 0), "a1", ("e1",)),
    ]
    report = report_of((Section.DAY, Section.TOP_SNIPERS, Section.PAIRS))
    return _kwargs(report, "daily:2026-09-18", _elig(ledger, roster), roster, cache)


def scn_revised():
    roster = roster_of({"a1": "sibA", "b1": "sibB"})
    cache = {"a1": "Alice", "b1": "Bob"}
    ledger = [
        cand(mkts(2026, 9, 18, 9, 0, 0), "a1", ("b1",)),
        cand(mkts(2026, 9, 18, 10, 0, 0), "b1", ("a1",)),
    ]
    report = report_of((Section.DAY, Section.TOP_SNIPERS))
    return _kwargs(report, "daily:2026-09-18", _elig(ledger, roster), roster, cache,
                   revision=1)


def scn_selfie_award():
    # An intra-group SELFIE (a1 tags sib a2) plus a plain snipe: the day's Points exceed
    # its Snipes, and the selfie legend appears. This is the canonical numbers_hash case.
    roster = roster_of({"a1": "sibA", "a2": "sibA", "b1": "sibB"})
    cache = {"a1": "Alice", "a2": "Bob", "b1": "Carol"}
    ledger = [
        selfie(mkts(2026, 9, 18, 9, 0, 0), "a1", ("a2",)),
        cand(mkts(2026, 9, 18, 10, 0, 0), "a1", ("b1",)),
        cand(mkts(2026, 9, 18, 11, 0, 0), "b1", ("a2",)),
    ]
    report = report_of(ALL_SECTIONS)
    return _kwargs(report, "daily:2026-09-18", _elig(ledger, roster), roster, cache)


def scn_selfie_award_plain():
    """The selfie scenario with the selfie downgraded to a plain snipe: same IDs, same
    counts, but no photo/participation award — so `points == snipes` and the hash moves."""
    roster = roster_of({"a1": "sibA", "a2": "sibA", "b1": "sibB"})
    cache = {"a1": "Alice", "a2": "Bob", "b1": "Carol"}
    ledger = [
        cand(mkts(2026, 9, 18, 9, 0, 0), "a1", ("a2",)),
        cand(mkts(2026, 9, 18, 10, 0, 0), "a1", ("b1",)),
        cand(mkts(2026, 9, 18, 11, 0, 0), "b1", ("a2",)),
    ]
    report = report_of(ALL_SECTIONS)
    return _kwargs(report, "daily:2026-09-18", _elig(ledger, roster), roster, cache)


def scn_week_header_dates():
    # ISO week 36 is Mon 2026-08-31 .. Sun 2026-09-06; the semester starts 2026-09-01, so
    # the week clips to 2026-09-01 .. 2026-09-06 and the header must read "partial week".
    roster = roster_of({"a1": "sibA", "b1": "sibB"})
    cache = {"a1": "Alice", "b1": "Bob"}
    ledger = [
        cand(mkts(2026, 9, 2, 9, 0, 0), "a1", ("b1",)),
        cand(mkts(2026, 9, 5, 9, 0, 0), "b1", ("a1",)),
    ]
    report = report_of((Section.WEEK, Section.TOP_SNIPERS), name="weekly",
                       cadence=Cadence.WEEKLY, at_hour=20, weekday=Weekday.SUN)
    return _kwargs(report, "weekly:2026-W36", _elig(ledger, roster), roster, cache)


SCENARIOS = {
    "names_40char": scn_names_40char,
    "tie_overflow": scn_tie_overflow,
    "most_sniped_full_roster": scn_most_sniped_full_roster,
    "empty_day": scn_empty_day,
    "group_of_one": scn_group_of_one,
    "opted_out_user": scn_opted_out_user,
    "duplicate_name": scn_duplicate_name,
    "emoji_in_names": scn_emoji_in_names,
    "revised": scn_revised,
    "selfie_award": scn_selfie_award,
    "week_header_dates": scn_week_header_dates,
}


# --------------------------------------------------------------------------- #
# Golden + limit tests
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_golden_blocks(name):
    render = _render(SCENARIOS[name]())
    assert_limits(render.blocks)
    check_golden(name, render.blocks)


def test_tie_overflow_line_present():
    render = _render(scn_tie_overflow())
    text = render.blocks[3]["text"]["text"]  # header, DAY, divider, TOP_SNIPERS
    assert "\n…and +" in text and " others tied at 1" in text


def test_empty_day_heartbeat():
    render = _render(scn_empty_day())
    scope = render.blocks[1]
    assert "_No snipes recorded._" in scope["text"]["text"]
    values = {f["text"] for f in scope["fields"]}
    assert values == {
        "*Snipes*\n0", "*Points*\n0", "*Unique snipers*\n0",
        "*Unique targets*\n0", "*Cooldown rejections*\n0",
    }


def test_opted_out_user_absent_everywhere():
    render = _render(scn_opted_out_user())
    blob = "".join(
        b.get("text", {}).get("text", "")
        + "".join(f["text"] for f in b.get("fields", []))
        for b in render.blocks
    )
    assert "OptedOutPerson" not in blob
    assert "opt" not in blob  # neither the display name nor the raw ID leaks


def test_duplicate_name_group_disambiguation():
    render = _render(scn_duplicate_name())
    top = render.blocks[3]["text"]["text"]  # header, DAY, divider, TOP_SNIPERS
    assert "Sam Rivers (sibA)" in top
    assert "Sam Rivers (sibB)" in top


def test_emoji_name_escaped_and_no_ping():
    render = _render(scn_emoji_in_names())
    top = render.blocks[3]["text"]["text"]  # header, DAY, divider, TOP_SNIPERS
    assert "Zoë 🎯 &lt;b&amp;d&gt;" in top
    assert "<@" not in top


def test_revised_marker_present():
    render = _render(scn_revised())
    assert render.blocks[1] == {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "_revised_", "verbatim": True}],
    }


def test_week_header_states_clipped_dates():
    render = _render(scn_week_header_dates())
    header = render.blocks[0]["text"]["text"]
    assert header == "Snipes: 2026-W36 (2026-09-01 to 2026-09-06, partial week)"
    assert render.text == header


# --------------------------------------------------------------------------- #
# numbers_hash contract (L7-GO-numbers_hash, L7-GO-points-in-hash)
# --------------------------------------------------------------------------- #

# Canonical hash for the selfie_award scenario; a numbers change (award or override)
# moves it, a name-only change does not.
CANONICAL_SELFIE_HASH = "94159aadd3b6e6e8e361d3caaedbf669ec594d7b9fa249405541dc60829f5721"


def test_selfie_award_points_exceed_snipes():
    render = _render(scn_selfie_award())
    scope = render.blocks[1]
    fields = {f["text"].split("\n")[0].strip("*"): f["text"].split("\n")[1]
              for f in scope["fields"]}
    assert int(fields["Points"]) > int(fields["Snipes"])
    # The selfie legend is the final block.
    legend = render.blocks[-1]
    assert legend["type"] == "context"
    assert legend["elements"][0]["text"].startswith(":selfie: sibfam selfie:")


def test_numbers_hash_matches_canonical_literal():
    render = _render(scn_selfie_award())
    assert render.metadata.numbers_hash == CANONICAL_SELFIE_HASH


def test_name_only_change_does_not_move_hash():
    base = scn_selfie_award()
    base_hash = _render(base).metadata.numbers_hash
    # Re-render with a wholly different name cache but identical IDs and standings.
    renamed = dict(base)
    renamed["names"] = NameResolver(
        {"a1": "Zzz Late", "a2": "Aaa Early", "b1": "Mmm Middle"}, base["roster"]
    )
    assert _render(renamed).metadata.numbers_hash == base_hash


def test_points_award_moves_hash():
    with_award = _render(scn_selfie_award()).metadata.numbers_hash
    without_award = _render(scn_selfie_award_plain()).metadata.numbers_hash
    assert with_award != without_award


def test_legend_suppressed_when_no_selfie_emoji():
    kwargs = scn_selfie_award()
    kwargs["selfie_emoji"] = None
    render = _render(kwargs)
    assert render.blocks[-1]["type"] != "context" or "sibfam" not in \
        render.blocks[-1]["elements"][0]["text"]


# --------------------------------------------------------------------------- #
# selfie legend gates on the RENDERED rows, not the whole ledger (ruling S6)
# --------------------------------------------------------------------------- #

def scn_selfie_ranked_earlier_day():
    """The only selfie sits on an earlier semester day than the (empty) scope day, so it
    reaches the digest only through a ranked row (top_snipers). F counts it there, so the
    legend appears even though the scope section's clipped day carries no selfie."""
    roster = roster_of({"a1": "sibA", "a2": "sibA"})
    cache = {"a1": "Alice", "a2": "Bob"}
    ledger = [selfie(mkts(2026, 9, 18, 9, 0, 0), "a1", ("a2",))]
    report = report_of((Section.DAY, Section.TOP_SNIPERS))
    return _kwargs(report, "daily:2026-09-20", _elig(ledger, roster), roster, cache)


def scn_selfie_outside_rendered_rows():
    """The only selfie belongs to a pair ranked below the top_n=2 top_snipers window and
    dated off the scope day, so no rendered row carries it: F == 0 and the legend is
    absent — the whole-ledger count would have wrongly shown it."""
    groups = {"a1": "sibA", "a2": "sibA"}
    cache = {"a1": "Alice", "a2": "Bob"}
    ledger = [selfie(mkts(2026, 9, 17, 9, 0, 0), "a1", ("a2",))]
    counter = 0
    for p, n in (("p0", 5), ("p1", 4), ("p2", 3)):
        groups[p] = "sibP"
        cache[p] = p.upper()
        for j in range(n):
            t = f"t{j}"
            groups.setdefault(t, "sibT")
            cache.setdefault(t, t.upper())
            ledger.append(cand(mkts(2026, 9, 18, 10, counter, 0), p, (t,)))
            counter += 1
    roster = roster_of(groups)
    report = report_of((Section.DAY, Section.TOP_SNIPERS), top_n=2)
    return _kwargs(report, "daily:2026-09-18", _elig(ledger, roster), roster, cache)


def test_selfie_legend_present_from_ranked_row():
    render = _render(scn_selfie_ranked_earlier_day())
    legend = render.blocks[-1]
    assert legend["type"] == "context"
    assert legend["elements"][0]["text"].startswith(":selfie: sibfam selfie:")


def test_selfie_legend_absent_when_outside_rendered_rows():
    render = _render(scn_selfie_outside_rendered_rows())
    assert not any(
        "sibfam" in e.get("text", "")
        for b in render.blocks
        for e in b.get("elements", [])
    )


def _payload_of(kwargs):
    return numbers_payload(
        kwargs["report"], kwargs["period_key"], kwargs["semester"], kwargs["elig"],
        kwargs["roster"], kwargs["opted_out"], kwargs["tz"],
    )


def test_selfie_legend_flip_moves_hash():
    present = scn_selfie_ranked_earlier_day()
    absent = scn_selfie_outside_rendered_rows()
    assert _payload_of(present)[-1] == ["selfie_legend", True]
    assert _payload_of(absent)[-1] == ["selfie_legend", False]
    assert _render(present).metadata.numbers_hash != _render(absent).metadata.numbers_hash


def test_emoji_name_change_does_not_move_hash():
    base = scn_selfie_award()
    base_hash = _render(base).metadata.numbers_hash
    # Only the emoji NAME changes; F (and every hashed field) is unmoved.
    renamed = dict(base)
    renamed["selfie_emoji"] = "camera_with_flash"
    render = _render(renamed)
    assert render.metadata.numbers_hash == base_hash
    assert render.blocks[-1]["elements"][0]["text"].startswith(
        ":camera_with_flash: sibfam selfie:")
