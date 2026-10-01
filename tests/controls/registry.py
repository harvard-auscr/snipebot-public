"""The controls registry (50-test-matrix section 1.3).

One canonical broken build per layer, plus the paired one-input-flip fixtures.
Each entry maps a control name to a `Control(layer, patch, expected_red)`:

- `layer`   the ladder layer the control lives on (L1..L8).
- `patch`   a callable that, given a `pytest` `MonkeyPatch`, installs the single
            break by replacing exactly one module function; `None` until the wave
            that owns the break lands it.
- `expected_red`   the pytest node ids that MUST turn red once the break is
            applied. For a control whose `patch` is filled in, these are runnable
            node ids the registry test drives in a subprocess. For a control still
            carrying `patch=None`, the tuple records the canonical test names from
            the section 1.3 table so the row stays self-documenting until its wave
            wires up the executable break.

Wired breaks are monkeypatched at one seam each: `parse`, `rules`, `sync`, `report`
and `slack_fixtures`/`faces_fixtures` for the fixture swaps. `CTL-RIG-NOREACT` skips
`sync`'s reaction convergence (50 §7.2 "step 7") and is driven offline against
`FakeSlack` by its paired positive control, so the "reactions off" break turns red
without a live workspace. Only `CTL-MUT-NOOP` still carries `patch=None` -- the
mutation run itself is the broken build, not a monkeypatch this registry can install.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Callable, Optional

Patch = Callable[[object], None]   # object is a pytest MonkeyPatch


@dataclass(frozen=True)
class Control:
    layer: str
    patch: Optional[Patch]
    expected_red: tuple[str, ...]


# --------------------------------------------------------------------------- #
# The seam-level breaks (parse / rules), each a one-function swap.
# --------------------------------------------------------------------------- #

def _patch_parse_mime(mp: object) -> None:
    """CTL-PARSE-MIME: `parse._is_live` drops its gating and counts any file.

    With the check dropped a Slack Connect stub (`file_access ==
    "check_file_info"`) is read as a live upload, so a message that should land
    NO_LIVE_IMAGE is COUNTED.
    """
    from snipebot import parse

    mp.setattr(parse, "_is_live", lambda f: True)  # type: ignore[attr-defined]


def _patch_rules_boundary(mp: object) -> None:
    """CTL-RULES-BOUNDARY: the cooldown edge test rejects instead of admitting.

    The shipped sweep admits an attempt that lands exactly one cooldown window
    after its anchor (`elapsed < window` is False at the boundary). Widening the
    window by a single microsecond reproduces the `>` vs `>=` off-by-one: the
    boundary attempt now falls one microsecond inside the window and cools down.
    """
    from snipebot import rules

    original = rules.evaluate

    def broken(facts, rules_, roster, opted_out, semesters, tz):  # type: ignore[no-untyped-def]
        widened = dataclasses.replace(
            rules_,
            entries=tuple(
                dataclasses.replace(
                    entry,
                    cooldown=dataclasses.replace(
                        entry.cooldown, microseconds=entry.cooldown.microseconds + 1
                    ),
                )
                for entry in rules_.entries
            ),
        )
        return original(facts, widened, roster, opted_out, semesters, tz)

    mp.setattr(rules, "evaluate", broken)


def _patch_faces_offbyone(mp: object) -> None:
    """CTL-FACES-OFFBYONE: the selfie classifier reads a selfie one face too high.

    The shipped classifier calls a photo a SELFIE when every image carries
    exactly `len(targets) + 1` faces (the sniper's own face on top of the
    targets). Shifting that to `+ 2` misses the real T+1 selfie (it falls through
    to AMBIGUOUS) and mislabels a T+2 group shot as a selfie.
    """
    from snipebot import rules
    from snipebot.rules import SelfieClass, Status

    def broken(row, roster, rule, m_status, sib_tagged):  # type: ignore[no-untyped-def]
        if not (m_status is Status.COUNTED and sib_tagged and rule.selfie_bonus):
            return SelfieClass.NOT_APPLICABLE
        override = row.selfie_override
        if override is not None:
            return SelfieClass.SELFIE if override.value else SelfieClass.SNIPE
        t = len(row.targets)
        counts = [row.face_counts.get(fid) for fid in row.live_image_ids]
        if all(c is not None for c in counts) and all(c == t for c in counts):
            return SelfieClass.SNIPE
        if all(c is not None for c in counts) and all(c == t + 2 for c in counts):
            return SelfieClass.SELFIE
        return SelfieClass.AMBIGUOUS

    mp.setattr(rules, "_classify_selfie", broken)  # type: ignore[attr-defined]


# Node ids the seam-level breaks turn red. These live in this workstream's own
# fixture module so the registry test is self-contained; the canonical section
# 1.3 test names each break maps to are noted alongside.
_MIME_RED = (
    # canonical: L1-FX-slack-connect-file (a stub file counts as a live image)
    "tests/test_rules_fixtures.py::test_fx_verdict[slack-connect-file]",
)
_BOUNDARY_RED = (
    # canonical: L2-PR-cooldown-boundary
    "tests/test_rules_fixtures.py::test_ctl_cooldown_boundary_counts",
)
_SELFIE_RED = (
    # canonical: L2-PR-selfie-class, L2-PR-selfie-points
    "tests/test_rules_fixtures.py::test_ctl_selfie_class_t_plus_one",
    "tests/test_rules_fixtures.py::test_ctl_selfie_class_t_plus_two_ambiguous",
)


# --------------------------------------------------------------------------- #
# The three photo-fixture breaks (50 §1.3 CTL-FACES-*): each swaps one fixture
# path for another so the real YuNetDetector reports the wrong count. The seam is
# `tests.controls.faces_fixtures.photo_path`; the swap reads only which file the
# test resolves, never the image bytes.
# --------------------------------------------------------------------------- #

def _swap_photo(victim: str, replacement: str) -> Patch:
    def patch(mp: object) -> None:
        from tests.controls import faces_fixtures

        original = faces_fixtures.photo_path

        def swapped(name: str):
            return original(replacement if name == victim else name)

        mp.setattr(faces_fixtures, "photo_path", swapped)  # type: ignore[attr-defined]

    return patch


def _patch_rig_noreact(mp: object) -> None:
    """CTL-RIG-NOREACT: `sync` skips reaction convergence (50 §7.2 "step 7")
    unconditionally, so no status emoji is ever placed or removed.

    The rig's read-back reaction assertions (R1's `counted`, R5's veto removal) then
    turn red, proving they inspect the reactions actually on the message rather than
    restating the ledger. `sync._converge_reactions` is the one seam that adds and
    removes the bot's reactions; stubbing it to a no-op (nothing added, nothing
    removed) is exactly "step 7 skipped". The paired positive control drives this
    offline against `FakeSlack`, where the same skip leaves the counted emoji off a
    plainly counted snipe.
    """
    from snipebot import sync

    mp.setattr(sync, "_converge_reactions",
               lambda *a, **k: (0, 0))  # type: ignore[attr-defined]


_RIG_NOREACT_RED = (
    # canonical: L6-RIG-reactions (the live rig's R1/R5 reaction read-back). Driven
    # offline here so the positive control runs without a workspace: sync skips the
    # convergence, so the counted emoji never lands on a counted snipe.
    "tests/rig/test_rig_offline.py::test_ctl_rig_noreact_positive_control",
)


def _patch_sync_onemiss(mp: object) -> None:
    """CTL-SYNC-ONEMISS: the merge marks a row `deleted` on the FIRST miss instead of
    the second. `sync._crosses_delete_threshold` is the one seam that holds the
    two-miss rule (`old_missing == 1 and new_missing == 2`); widening it to any miss
    at all (`new_missing >= 1`) moves the deletion one fetch earlier.
    """
    from snipebot import sync

    mp.setattr(sync, "_crosses_delete_threshold", lambda old, new: new >= 1)  # type: ignore[attr-defined]


_SYNC_ONEMISS_RED = (
    # canonical: L3-FA-vanish, L2-PR-delete-safety (two-miss rule)
    "tests/test_sync.py::test_two_complete_misses_mark_deleted",
)


def _patch_render_overflow(mp: object) -> None:
    """CTL-RENDER-OVERFLOW: the leaderboard renderer omits the "+K others tied" collapse
    line for a tied tail, so a reader can no longer tell the table was truncated.
    """
    from snipebot import report

    mp.setattr(report, "_others_tied_line", lambda collapsed, cutoff_str: "")  # type: ignore[attr-defined]


_RENDER_OVERFLOW_RED = (
    # canonical: L7-GO-tie_overflow
    "tests/test_report_golden.py::test_golden_blocks[tie_overflow]",
    "tests/test_report_golden.py::test_tie_overflow_line_present",
)


def _patch_redteam_live(mp: object) -> None:
    """CTL-REDTEAM-LIVE: re-introduce the accepted wave1 red-team defect in
    `parse._INLINE_RE` -- the un-fixed pattern does not allow a code span to cross a
    newline, so an inline code span straddling a line break no longer strips the
    ping it carries.
    """
    import re

    from snipebot import parse

    mp.setattr(parse, "_INLINE_RE", re.compile(r"`[^`]*`"))  # type: ignore[attr-defined]


_REDTEAM_LIVE_RED = (
    "tests/red_team/wave1/test_parse_r3.py::"
    "test_inline_code_span_stripped_across_newline_drops_pinged_mention",
)


def _patch_doctor_scope(mp: object) -> None:
    """CTL-DOCTOR-SCOPE: drop `reactions:read` from the fixture that stands in
    for a fully-scoped bot token (`tests/controls/slack_fixtures.py`), leaving
    `doctor`'s own required set -- read from the real, untouched
    `slack-app-manifest.yaml` -- with a scope the (patched) granted set no
    longer carries. DOC-SCOPES then correctly FAILs where the "full manifest
    scopes -> PASS" test (`L8-PF-DOC-SCOPES`) expected a PASS.
    """
    from tests.controls import slack_fixtures

    original = slack_fixtures.full_granted_scopes

    def patched() -> tuple[str, ...]:
        return tuple(s for s in original() if s != "reactions:read")

    mp.setattr(slack_fixtures, "full_granted_scopes", patched)  # type: ignore[attr-defined]


_DOCTOR_SCOPE_RED = (
    "tests/test_doctor.py::test_doc_scopes_full_manifest_grants_pass",
)


_FACES_2FACE_RED = (
    "tests/test_faces.py::test_real_detector_counts[selfie_two_faces.jpg]",
)
_FACES_1FACE_RED = (
    "tests/test_faces.py::test_real_detector_counts[portrait_one_face.jpg]",
)
_FACES_0FACE_RED = (
    "tests/test_faces.py::test_real_detector_counts[landscape_no_face.jpg]",
)


CONTROLS: dict[str, Control] = {
    "CTL-PARSE-MIME": Control("L1", _patch_parse_mime, _MIME_RED),
    "CTL-RULES-BOUNDARY": Control("L2", _patch_rules_boundary, _BOUNDARY_RED),
    "CTL-FACES-OFFBYONE": Control("L2", _patch_faces_offbyone, _SELFIE_RED),
    # sync wave: merge marks `deleted` at missing_runs >= 1 instead of >= 2
    "CTL-SYNC-ONEMISS": Control("L3", _patch_sync_onemiss, _SYNC_ONEMISS_RED),
    # --- breaks homed in later waves; patch wired up there (50 section 1.3) ---
    # mutation wave: the mutation itself is the broken build (section 6)
    "CTL-MUT-NOOP": Control("L4", None, ("mutmut rules.py survivors == 0",)),
    # red-team wave: re-introduce the last accepted red-team defect (wave1 parse r3:
    # inline code span no longer strips a ping that crosses a newline)
    "CTL-REDTEAM-LIVE": Control("L5", _patch_redteam_live, _REDTEAM_LIVE_RED),
    # rig wave: sync skips reaction convergence (50 §7.2 "step 7") unconditionally
    "CTL-RIG-NOREACT": Control("L6", _patch_rig_noreact, _RIG_NOREACT_RED),
    # outputs wave: renderer omits the "+K others tied" line
    "CTL-RENDER-OVERFLOW": Control("L7", _patch_render_overflow, _RENDER_OVERFLOW_RED),
    # io+manifest wave: drop `reactions:read` from the manifest under test
    "CTL-DOCTOR-SCOPE": Control("L8", _patch_doctor_scope, _DOCTOR_SCOPE_RED),
    # faces wave: swap selfie_two_faces.jpg for landscape_no_face.jpg (returns 0)
    "CTL-FACES-2FACE": Control(
        "L1", _swap_photo("selfie_two_faces.jpg", "landscape_no_face.jpg"),
        _FACES_2FACE_RED,
    ),
    # faces wave: swap portrait_one_face.jpg for landscape_no_face.jpg (returns 0)
    "CTL-FACES-1FACE": Control(
        "L1", _swap_photo("portrait_one_face.jpg", "landscape_no_face.jpg"),
        _FACES_1FACE_RED,
    ),
    # faces wave: swap landscape_no_face.jpg for selfie_two_faces.jpg (returns 2)
    "CTL-FACES-0FACE": Control(
        "L1", _swap_photo("landscape_no_face.jpg", "selfie_two_faces.jpg"),
        _FACES_0FACE_RED,
    ),
}
