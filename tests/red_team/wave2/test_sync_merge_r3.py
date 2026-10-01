"""Wave 2, Round 3 (INVARIANTS ACROSS RUNS) — adversarial attack on snipebot/sync.py
steps 0-4: fingerprint guard, fetch range, parse+keep filter, merge, missing->deleted,
delete breaker, dropping deleted rows and blocked_by integrity.

Attack theme: idempotence of a second sync, order-independence, convergence after a single
fault, byte-identical ledgers across schedules, verdict/ledger agreement, and conservation
between what was reacted and what was persisted.

RESULT: no provable spec violation found in the attacked surface. Every across-runs invariant
named in 20-sync-ledger.md sections 2-4 and 9 and 00-data.md sections 2-3 was exercised
against the current code with the FakeSlack authoring API (posts/edits/deletes plus the
`vanish`, `fetch_429` and `fail_mid_page` faults) and held. A red-team test may only be
committed here if it FAILS on the current code (a passing test is not a finding and is
deleted); none of the probes below produced a failing assertion, so this file intentionally
contains no tests.

Angles exercised and confirmed correct (probes run, not committed):

- Idempotence. A second sync at the same `now` over a stable channel (present messages, plus
  a counted sib-tagged face image under selfie_bonus) reproduces byte-identical
  ledger.jsonl / state.json / verdicts.jsonl. No field (missing_runs, watermark,
  fingerprints, face_counts, detect_attempts, target_edited_in) drifts on the no-op re-run.

- Fingerprint guard across runs. A forward-dated rule (and, separately, a forward-dated
  player `from:`) already present in config does NOT self-refuse (exit 3) when `H` advances
  past its effective_from/join between runs, because the guard keys off the on-disk ledger's
  `H` (which lags the just-fetched message by one run) and the persist step rewrites the
  fingerprints the run before the stored `H` reaches the new floor (20 §2.4 "a pre-authorised
  forward-dated bump never later self-refuses").

- Missing -> deleted -> reappear. A message that `vanish`es for one fetch increments
  missing_runs to 1; on the next fetch it reappears and missing_runs resets to 0 with
  first_seen_targets and targets intact (20 §4.2). A message edited to add a tag WHILE it is
  vanished appends exactly one TargetEdit for that tag on reappearance and never a duplicate
  on the third run; first_seen_targets stays the creation-time set (20 §4.1).

- Partial fetch is not a miss. A `fail_mid_page` fault raises a SlackError, so step 2 aborts
  with exit 5 and writes nothing; the would-be second absence is never persisted, so two
  misses require two COMPLETE fetches (20 §3 "every run that reaches merge has a complete
  fetch").

- Delete breaker across runs. Because an aborted breaker run (exit 6) and an accept-deletes
  mismatch (exit 7) both write nothing, the pending rows stay at missing_runs 1 and the same
  newly_deleted recomputes on the retry; the released run lands the rows at deleted and the
  breaker does not re-trip on the following run (newly_deleted counts only the 1->2 crossing).

- blocked_by integrity, deleted-in-window. A COUNTED anchor A with a COOLDOWN dependent B
  (blocked_by A), both inside a widened scan window, is deleted (two misses) but kept because
  its ts is still in-window; step 6 re-evaluates B to COUNTED with blocked_by cleared before
  persist, so no surviving blocked_by points at a deleted row and the step-8 integrity check
  passes (20 §4.4).

- Convergence after a single tolerated fault. A per-image `fetch_429` on run A leaves the
  face image uncounted (attempts incremented, the documented carve-out); a clean run B counts
  it, and face_counts / rendition_hash converge to exactly what a never-faulted single run
  produces (20 §5.2.2 fault table; §2.2 faces carve-out).

See spec_issues in the wave return for two gaps noted while sweeping this surface (the §3
watermark "only after a gap" claim vs. a monotonic watermark stuck on a deleted newest
message, and the §9.2 fetch-log example's `pages=` field that the sync layer cannot compute
because SlackIO.history returns a flat list). Both are spec-clarity gaps, not code defects, so
they are reported as spec_issues rather than findings.
"""
