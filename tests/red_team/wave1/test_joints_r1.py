"""Round 1 — joints / cross-cutting spec conformance (red team).

Scope: the seams between modules rather than any one module's internal logic
(the per-module files in this directory cover that). Checked against the spec:

  - shared-name homes (00-data section 10): every shared type/enum/constant is
    defined once at its home and imported elsewhere;
  - dataclass field names, ORDER and defaults verbatim (Candidate, Veto,
    SelfieOverride, TargetEdit, Digest, DigestMetadata, PairVerdict,
    MessageVerdict, ResolvedRule, CooldownRule, DatedRules, RosterEntry, Roster,
    Semester, FeedbackReactions, ReviewFlag, SyncSettings, ConsentConfig,
    FacesConfig, ReportSpec, Config, EligibleSnipe, RejectedAttempt, Eligibility,
    DuePeriod, DigestRender);
  - enum values equal the spec strings (Status, Reason, SelfieClass, Scope,
    MultiTag, Cadence, Section, Weekday, VetoActor, Persistence, VetoSource);
  - the SlackError taxonomy (10 section 3) — every class and its base;
  - signatures of evaluate, eligible_snipes, render_digest, most_recent_due,
    NameResolver, FaceDetector, SlackIO methods;
  - no float() applied to a ts; logs/warnings carry IDs only;
  - no hardcoded bot / workspace name;
  - every production module imports in a fresh subprocess (no import cycles);
  - no mention of models/assistants/agents/prompts/sessions in snipebot/ or tests/;
  - the oracle's structural independence from rules.py (50 section 3);
  - config.example.yaml loads and its keys match 40 section 1;
  - requirements.txt / requirements-dev.txt / pyproject match 40 section 6.

Result: no joint-level spec violation survived verification.

Two differential harnesses were run to probe the seams that unit assertions miss:
  1. snipebot.rules.evaluate vs the independent tests.oracle.oracle.evaluate over
     20,000 randomly generated scenarios (varying scope, multi_tag,
     rejected_attempts_reset, caps, grace, selfie_bonus, allow_*, faces, overrides,
     vetoes, edits, semesters, tz) -> 0 divergences on the full MessageVerdict lists;
  2. snipebot.aggregate points vs tests.oracle.oracle.people_points over 20,000
     scenarios -> 0 divergences.

The oracle sharing the private helper NAMES `_late_tag` and `_classify_selfie`
with rules.py was considered and rejected as a finding: spec 50 section 3.1
forbids the oracle reading rules.py source or copying the *algorithm*, and
section 3 explicitly has it re-implement the spec-named "late_tag evidence rule"
and "selfie classification"; the names are shared spec terminology, the bodies
are independent (O(n^2) rescan vs. carried anchor state), and the 20,000-case
agreement is the intended non-trivial evidence that the independence holds.

No test functions are defined here because a passing "break" is not a finding.
"""
