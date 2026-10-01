"""Wave 2, Round 1 (spec conformance) — adversarial attack on snipebot/sync.py steps 0-4:
fingerprint guard, fetch range, parse+keep filter, merge, missing->deleted, delete breaker,
dropping deleted rows and blocked_by integrity.

RESULT: no provable spec violation found in the attacked surface. Every rule, boundary and
ordering named in 20-sync-ledger.md sections 2-4 and 9, and 00-data.md sections 2-3, was
exercised against the current code and held. A red-team test may only be committed here if
it FAILS on the current code (a passing test is not a finding and is deleted); none of the
probes below produced a failing assertion, so this file intentionally contains no tests.

Angles exercised and confirmed correct (probes run, not committed):
- fetch_oldest_us: normal window; backfill override ignores all terms; watermark gap-recovery
  pulls oldest back only when < scan_floor; pending-miss term clamped up to the horizon floor;
  message exactly at scan_floor is fetched (inclusive) and stored.
- fingerprint guard: refuses (exit 3) on an undated rules change over rows with ts <= H;
  allows a future-dated (effective_from > H) rule/roster addition; --reevaluate bypasses it.
- parse + keep filter: a new fileless (text-only) candidate is dropped; an existing row edited
  to text-only has its media wholesale-replaced to 0 and stays in the ledger; a text/blocks
  mention-disagreement row is stored AND audited under text_blocks_disagree.
- merge fact replacement: TargetEdit appended once for an added tag, never duplicated on a
  third run, edit_ts pinned to the sync that first saw the target; first_seen_targets stable;
  a caption-only edit appends no spurious TargetEdit; a tombstoned file drops from
  live_image_ids while its face_counts entry is carried untouched.
- missing -> deleted: two complete misses cross 1->2 (newly_deleted counted); a single miss
  then reappearance resets missing_runs to 0 and undeletes; a full delete then reappearance
  resets; a pending row aged past history_horizon_days never increments and is never deleted.
- delete breaker: trips (exit 6) only when newly_deleted > max_deletes_per_run (exactly at the
  cap proceeds); accept_deletes == newly_deleted releases once as a large movement; a stale
  accept_deletes count aborts with exit 7 carrying the fresh figure.
- dropping: a deleted row is dropped iff missing_runs >= 2 AND ts < scan_floor; a deleted row
  still inside the window is kept; a dropped/deleted cooldown anchor is re-evaluated so the
  dependent re-anchors (blocked_by clears), and the end-of-sync integrity check passes.
- dry-run leaves ledger/state/verdicts untouched and fetches no image; kill switch (enabled
  false + SYNC/RUN) returns exit 0 with no side effects while admin commands proceed.

See spec_issues in the wave return for the one ambiguity noted (20 section 3, step-3 row): the
sentence lists "a malformed digest" ParseAnomaly beside the text/blocks disagreement and says
step 3 "adds it to the L8 audit list under text_blocks_disagree", but the audit category is
named for text/blocks and a digest anomaly stores no row, so it is unclear (and the code does
not) whether a digest-channel-mismatch ParseAnomaly should be audited. This is a spec-clarity
gap, not a code defect, so it is reported as a spec_issue rather than a finding.
"""
