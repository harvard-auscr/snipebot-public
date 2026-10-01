"""Round 1 red-team tests: spec-conformance breaks against snipebot/rules.py
and the dataclasses it consumes (snipebot/config.py, snipebot/parse.py).

Scope of this file: the verdict layer of 00-data.md section 4 and section 5, and
PLAN.md section 1 / section 1a. One test function per proven finding, each failing
on the current code.

Result of Round 1: no provable spec violation was found in the rules layer. The
`evaluate` sweep was checked clause-by-clause against 00-data.md section 4/section 5
and PLAN.md section 1/1a (per-message gate order and the REPOST gate; per-target
gate order; the cooldown `>=` boundary in integer microseconds; anchor
creation/consumption and `blocked_by` under both `rejected_attempts_reset` values;
`admit` daily-cap ordering; `multi_tag` single-fold vs per-target independence;
the `late_tag` evidence rule and grace inclusivity; sib-tagged/T derivation; selfie
classification T / T+1 / mix / missing / vacuous-empty and the durable override;
the intra-group `PairVerdict.selfie` mirror; message-level status/reason selection),
and the consumed dataclass methods (`Candidate.deleted`, `Candidate.is_top_level`,
`DatedRules.in_force_at`, `Roster.is_member_at`/`is_bot`/`group_of`,
`Semester.contains`).

The production sweep was additionally confirmed to equal the independent
from-scratch oracle (tests/oracle/oracle.py) on full-field `MessageVerdict`
equality, both across the existing 200-example fuzzing space and across the two
combinations that space deliberately holds constant (a `cooldown.scope` change and
a `rejected_attempts_reset` change across dated rule entries).

Per the Round 1 charter ("A test that passes is not a finding; delete it"), no
test functions are emitted here. The genuine gap that surfaced is filed under
spec_issues, not as a code finding.
"""

from __future__ import annotations

# No findings: no test functions. See the module docstring.
