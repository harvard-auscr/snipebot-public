# Mutation testing: survivors and named-mutant pins

Scope, runner and termination rule: `spec/50-test-matrix.md` section 6 (6.1-6.3),
cross-referenced from section 2.4. Config lives in `pyproject.toml` `[tool.mutmut]`
(mutmut 2.5.1 reads `paths_to_mutate`/`runner` from there; see the note in
`tests/mutation/mutmut_config.py` for why the settings are not read from that
nested module directly). Runner: `.venv\Scripts\python.exe -m pytest -q -x
tests/mutation/test_mutation_backstop.py tests/test_ts.py tests/test_parse.py
tests/test_rules_fixtures.py tests/test_aggregate.py tests/test_report_tables.py
tests/oracle/test_oracle_equiv.py tests/oracle/test_properties.py` — the
pure-core + oracle + backstop subset, never the full suite, per the DECISION in
section 6.1 (order is a speed optimization over the spec's own listing; see
"Decisions" below).

Environment: Windows, mutmut 2.5.1, no fork (subprocess runner via `cmd.exe`,
`shell=True`). `--simple-output` used throughout (mutmut's default emoji legend
does not encode on the `cp1252` Windows console).

Hypothesis example count: `tests/oracle/test_properties.py` and
`tests/oracle/test_oracle_equiv.py` each read a module constant `_EXAMPLES =
int(os.environ.get("SNIPEBOT_HYPOTHESIS_EXAMPLES", "<file's original
default>"))` (120 for `test_properties.py`, 200 for `test_oracle_equiv.py`)
and pass it to every `@settings(max_examples=...)` in the file. Unset, both
files behave exactly as before (verified by a full `tests/oracle` run before
any mutmut invocation: 27 passed, unchanged). A mutation run sets
`SNIPEBOT_HYPOTHESIS_EXAMPLES=10` in the shell before invoking mutmut so
every child pytest run inherits it, cutting Hypothesis's per-mutant example
budget without touching either file's assertions or its default for normal
(non-mutation) test runs. `tests/oracle/test_schedule_replay.py` carries no
`@settings` and needed no change.

## Summary (all four files, termination rule met)

Per spec 6.2/6.3: `rules.py` has zero survivors; `ts.py`/`parse.py`/`aggregate.py`
have survivors only on this justified allowlist. All four files are complete —
nothing was left untested.

| File | Real mutants | Killed | Survived (justified equivalent) | Timeout/Suspicious/Untested |
|---|---|---|---|---|
| `rules.py` | 224 (id 112 is a cache-numbering gap) | 219 | 5 | 0 |
| `ts.py` | 21 | 20 | 1 | 0 |
| `parse.py` | 193 (ids 263-264 are gaps) | 187 | 6 | 0 |
| `aggregate.py` | 213 (12 ids were gaps: 528-538, 540) | 206 | 7 | 0 |
| **Total** | **651** | **632** | **19** | **0** |

## Named mutants (matrix section 2.4, plan section 9 L4)

Pinned per the mapping the spec table already gives; test IDs resolved to their
actual pytest node ids below. `rules.py`/`parse.py` mutants are inside the
mutmut-mutated scope (`ts.py`/`parse.py`/`rules.py`/`aggregate.py`) and are
additionally confirmed killed by the real mutmut run recorded below.
`config.py` is excluded from the mutmut scope (section 6.1: "covered by their
own suites"), so `L4-MU-join-date` and `L4-MU-semester-end` are pinned to their
killer test by inspection of the spec table, not by a mutmut mutation of
`config.py` itself.

| Mutant | Module | Mutation | Killer test | Status |
|---|---|---|---|---|
| `L4-MU-ge-gt` | rules.py | `>=`→`>` at the cooldown boundary (`admit`, the `ts_us - a < cd.microseconds` check) | `tests/oracle/test_properties.py::test_cooldown_boundary` | in mutmut scope |
| `L4-MU-pair-target` | rules.py | scope key `(sender,target)`→`(target,)` (`admit`'s `key = ...` line) | `tests/oracle/test_properties.py::test_cooldown_mingap_and_blocker` | in mutmut scope |
| `L4-MU-parent-thread` | parse.py | `is_top_level` drops the `thread_ts == ts` clause | `tests/test_parse.py::test_L1_RF_reply_added` | in mutmut scope |
| `L4-MU-self-snipe` | rules.py | SELF_SNIPE gate removed (`t == f.sender and not rule.allow_self`) | `tests/oracle/test_oracle_equiv.py` (self-target case) | in mutmut scope |
| `L4-MU-sender-roster` | rules.py | SENDER_OFF_ROSTER gate removed (`not roster.is_member_at(f.sender, ts_us)`) | `tests/oracle/test_properties.py::test_optout_and_roster_closure` | in mutmut scope |
| `L4-MU-join-date` | config.py | `is_member_at` ignores `join_us` | `tests/oracle/test_properties.py::test_optout_and_roster_closure` | out of mutmut scope (config.py has its own suite; see decisions) |
| `L4-MU-tombstone-live` | parse.py | tombstoned file counted as live | `tests/test_parse.py::test_L1_RF_file_deleted` | in mutmut scope |
| `L4-MU-optout-filter` | rules.py | opt-out filter removed (`f.sender in opted_out` / `t in opted_out`) | `tests/oracle/test_properties.py::test_optout_and_roster_closure` | in mutmut scope |
| `L4-MU-semester-end` | config.py | `Semester.contains` inclusive end `<=`→`<` (off-by-one) | `tests/test_aggregate.py::test_semester_filter_keeps_only_requested_semester` | out of mutmut scope (config.py has its own suite; see decisions) |
| `L4-MU-backstop-*` | any | config-branch mutants the oracle strategies do not reach | `tests/mutation/test_mutation_backstop.py` | see table below |

## Run: `snipebot/rules.py` — COMPLETE (zero unjustified survivors)

**Run 1** (before the `SNIPEBOT_HYPOTHESIS_EXAMPLES` override existed):
225 mutants generated, 74 tested before the run was stopped for time; all 74
closed (58 killed outright, 2 suspicious-but-killed, 14 survivors closed with
backstop tests — see git history for that table). ids 75-225 were untested.

**Run 2** (with `SNIPEBOT_HYPOTHESIS_EXAMPLES=10` set for every
mutmut invocation): the cache was invalidated by the `test_properties.py` /
`test_oracle_equiv.py` edits (step 1) and by new tests added to
`test_mutation_backstop.py`, so mutmut re-mutated and re-tested from id 1.
mutmut reports "225 mutants generated", but id 112 was never populated in
the result cache (`mutmut show 112` raises `Obtained null mutant for pk:
112` — an internal mutmut numbering gap, not a testing gap); **224 real,
distinct mutants exist and all 224 are classified.**

Final status (`.mutmut-cache`, queried directly via sqlite since `mutmut
results`'s emoji legend requires `PYTHONUTF8=1`/`PYTHONIOENCODING=utf-8` on
this console):

| Status | Count | Ids |
|---|---|---|
| `KILLED` | 219 | every id 1-225 except 78, 82, 154, 199, 208, and the nonexistent 112 |
| `SURVIVED` | 5 | 78, 82, 154, 199, 208 — all justified equivalent, see Allowlist below |
| `TIMEOUT` / `SUSPICIOUS` / `UNTESTED` | 0 | — |

**Zero unjustified survivors: the spec 6.2/6.3 requirement for `rules.py` is met.**

### How the first full pass broke down (before the 24 new backstop tests)

The full `mutmut run --paths-to-mutate snipebot/rules.py` (baseline 2.15s
with `SNIPEBOT_HYPOTHESIS_EXAMPLES=10`) took **1h10m23s** and classified:
187 killed (live counter; see cache-reconciliation note below), 15 survived,
17 timeout (mutmut's hard per-mutant timeout is a fixed `baseline*10`,
~21.5s here — not the `-m`/`-b` CLI flags, which only gate the separate
`SUSPICIOUS` threshold), 6 suspicious, 3 untested/skipped (a `tested_against_hash`
cache-reconciliation artifact from ids tallied live but not persisted as
`ok_killed`; re-running them individually resolved this). All 41 non-killed
ids were closed with 24 new deterministic backstop tests in
`test_mutation_backstop.py` (see the pin table below), then every one of the
41 was re-run individually with `mutmut run <id>` (fast: ~4-5s each,
including the per-invocation baseline check) and reconfirmed: 36 now
`KILLED`, 5 provably equivalent (`SURVIVED`, justified, see Allowlist).

### Backstop pin table (Run 2 closures)

| Ids | Mutation | Backstop test |
|---|---|---|
| 79 | `_local_date_str`'s `strftime("%Y-%m-%d")` -> `strftime("XX%Y-%m-%dXX")` | `test_local_date_str_backstop_format` |
| 81 | `_late_tag`: early `return False` (present at first sight) -> `return True` | `test_late_tag_backstop_present_at_first_sight` |
| 83, 88 | `_late_tag`: `edit_ts` init `None`->`""` (crashes `parse_ts("")`); final `return False`->`return True` | `test_late_tag_backstop_no_edit_record` |
| 86 | `_late_tag`: `break`->`continue` scanning `target_edited_in` | `test_late_tag_backstop_first_match_wins` |
| 117 | `admit`'s cooldown key: `cd.scope is Scope.PAIR` -> `is not Scope.PAIR` | `test_cooldown_key_backstop_scope_pair_vs_target` |
| 125, 126 | `admit`'s rejection-reset: `anchor[key]=ts_us`->`None`; `anchor_ts[key]=ts`->`None` | `test_cooldown_backstop_rejected_attempts_reset_extends_and_reports_blocker` |
| 127 | `admit`: `day = _local_date_str(ts_us, tz)` -> `day = None` (day cap becomes lifetime, not per-day) | `test_day_cap_backstop_resets_next_day` |
| 128, 135, 136, 137, 138 | `admit`'s day-cap bookkeeping: `cap`->`None`; `day_counted` default `0`->`1`; increment `+1`->`-1`/`+2`/direct `None` | `test_day_cap_backstop_same_day_boundary` |
| 149, 151 | `live_count`: video term `+`->`-`; linked-image term `+`->`-` | `test_live_count_backstop_video_and_linked_terms_add` |
| 93, 146, 147 | three different `and`->`or` loosenings of the sib_tagged / selfie-gate chain | `test_selfie_backstop_sib_tag_requires_group_match` |
| 98 | `_classify_selfie`'s SNIPE branch: `is not None`->`is None` (makes an exact match impossible) | `test_selfie_backstop_snipe_requires_face_match` |
| 195, 197 | bot gate: `not rule.allow_bots`->`rule.allow_bots`; `pre_reason[t]=TARGET_IS_BOT`->`None` | `test_target_gate_backstop_bot_reason` |
| 108 | `_remember_hashes`: `sem is None or not row.rendition_hash`->`... and ...` (crashes `sem.name` on `None`) | `test_remember_hashes_backstop_out_of_season_with_hash` |
| 162 | message gate: `gate = Reason.OUT_OF_SEASON` -> `gate = None` | `test_out_of_season_backstop_gate_applies` |
| 156 | message gate: `gate = Reason.DELETED` -> `gate = None` | `test_deleted_backstop_gate_applies` |
| 171 | `len(f.targets) > rule.max_targets_per_message` -> `>=` (boundary off-by-one) | `test_max_targets_backstop_boundary_not_gated` |
| 173 | message gate: `gate = Reason.TOO_MANY_TARGETS` -> `gate = None` | `test_max_targets_backstop_over_limit_gated` |
| 175 | `len(f.vetoes) > 0` -> `> 1` (boundary off-by-one) | `test_vetoes_backstop_single_veto_gates` |
| 184 | untagged-message branch: `continue` -> `break` (aborts the whole chronological sweep) | `test_untagged_backstop_does_not_abort_remaining_facts` |
| 188 | self-snipe gate: `not rule.allow_self` -> `rule.allow_self` | `test_self_snipe_backstop_gated_when_disallowed` |
| 194 | pre-target gate: `pre_reason[t]=TARGET_OFF_ROSTER` -> `None` | `test_target_gate_backstop_off_roster_reason` |
| 202 | `multi_tag=SINGLE`: `chosen = eligible[0]` -> `eligible[1]` | `test_multi_tag_single_backstop_picks_first_eligible` |
| 77, 217, 218 | `_TARGET_REASON_RANK`->`None`; its lookup `key=lambda r: _TARGET_REASON_RANK[r]`->`lambda r: None`; `m_reason = min(...)`->`None` | `test_dominant_reason_backstop_ranked_not_mention_order` |
| 224 | per-pair `selfie` flag: first `and`->`or` (message-level SELFIE alone sets every pair's flag) | `test_selfie_flag_backstop_requires_pair_group_match` |

Root cause for the whole batch: every one of these mutations either (a) is
only reachable through a *specific* scenario shape the Hypothesis property
strategies under-sample at `max_examples=10` (the reason mutation
runs reduce examples for speed), or (b) is a config/branch combination
(bot roster entries, day-boundary timestamps, `rejected_attempts_reset`,
`multi_tag=SINGLE`, an out-of-season message carrying a repost hash) that the
scenario generator can produce but rarely enough to miss at a reduced budget.
Each is exactly the section 6.3 "config branch the oracle strategy does not
exercise" case, closed with a direct, deterministic unit test instead of
relying on Hypothesis to eventually sample it. Because
`test_mutation_backstop.py` runs FIRST in the runner order, every one of
these mutants now fails in well under a second instead of hitting mutmut's
~21.5s hard timeout waiting for `test_properties.py`'s later Hypothesis
tests to happen to shrink into the failing case.

### Investigation note: `admit`'s `day = None` is a mutation, not a shipped bug

While inspecting a slow-running mutant, reading `snipebot/rules.py` on
disk transiently showed `day = None` in `admit` instead of `day =
_local_date_str(ts_us, tz)`. This was mutmut's own mutant caught mid-flight
(the file is genuinely mutated on disk for the ~2s a mutant's test run takes),
**not** a defect in the committed source — confirmed via `git show
HEAD:snipebot/rules.py`, which has the correct `_local_date_str(ts_us, tz)`
call. Recorded here because it
looked alarming for a moment. (This exact line, mutated for real by mutmut as
id 127, is now covered by `test_day_cap_backstop_resets_next_day` above.)

## Run: `snipebot/ts.py` — COMPLETE (zero unjustified survivors)

21 mutants generated, all classified in 17.4s (post `--continue-on-collection-errors`
fix; see "Runner fix" below). 20 killed, 1 justified equivalent.

| Mutant | Mutation | Disposition |
|---|---|---|
| 227-230, 232-235, 239-242, 244-246 (12) | assorted regex/operator/arithmetic mutations in `parse_ts`/`format_ts` | killed by the existing `tests/test_ts.py` suite |
| 231 | `_TS_RE` pattern wrapped in "XX...XX" (never matches anything) | killed — see "Runner fix" below (was a false SURVIVED before the fix) |
| 236 | `TsFormatError` message wrapped in "XX...XX" | killed by `test_parse_ts_backstop_error_message` |
| 243 | `ValueError` message (in `format_ts`) wrapped in "XX...XX" | killed by `test_format_ts_backstop_error_message` |
| **226** | `Ts = int` -> `Ts = None` | **SURVIVED, justified equivalent** — see Allowlist |

## Run: `snipebot/parse.py` — COMPLETE (zero unjustified survivors)

195 mutants generated; ids 263-264 never populated in the cache (same
numbering-gap phenomenon as `rules.py`'s id 112 — `mutmut show` raises
"Obtained null mutant for pk" for them), so **193 real, distinct mutants
exist.** First full pass (before `--continue-on-collection-errors`, see
below): 6m18s, 108 killed, 86 (not 87 — the live progress counter's final
tally double-counted one id relative to the persisted cache, same
reconciliation quirk seen in `rules.py`'s run) survived. All 86 were closed
with 21 new backstop tests (pin table below); after adding them, every
survivor was re-run individually (`mutmut run <id>`) and **187 killed, 6
justified equivalent** (ids 333, 337, 388, 390, 415, 423 — see Allowlist).

### Backstop pin table (`parse.py`)

| Ids | Mutation | Backstop test |
|---|---|---|
| 247, 248, 249, 250 | `VetoSource.REACTION`/`.CLI` literal values wrapped/nulled | `test_veto_source_backstop_values` |
| 251, 253, 255, 257, 285, 287 | `@dataclass(frozen=True)` -> `frozen=False` on `Veto`/`SelfieOverride`/`TargetEdit`/`Candidate`/`Digest`/`DigestMetadata` | `test_parse_dataclasses_backstop_are_frozen` |
| 259, 261, 262, 265, 268 | `Candidate`'s `face_counts`/`detect_attempts`/`has_file_object`/`selfie_override` field defaults tampered | `test_candidate_backstop_field_defaults` |
| 266 | `has_file_object` field's `compare=False`->`True` | `test_candidate_backstop_has_file_object_not_compared` |
| 267 | `has_file_object` field's `repr=False`->`True` | `test_candidate_backstop_has_file_object_not_in_repr` |
| 276-282 | `file_sig`'s width/height `""`/`str(...)` branches (both directions) and the payload f-string | `test_file_sig_backstop_exact_hash` |
| 289-297 | `DigestMetadata.to_wire`'s wire-dict key/value literals | `test_digest_metadata_backstop_to_wire_exact_shape` |
| 298 | `DigestMetadata.from_wire`'s `@staticmethod` removed | `test_digest_metadata_backstop_from_wire_is_static` |
| 314, 315, 316 | `_RENDITION_KEYS` tuple entries wrapped | `test_rendition_keys_backstop_values` |
| 325, 327 | `_is_present`'s `"mode"`/`"hidden_by_limit"` literals | `test_is_present_backstop_hidden_by_limit` |
| 340, 342 | `_strip_code`'s fenced/inline `.sub("", ...)` replacement text | `test_strip_code_backstop_removes_not_replaces` |
| 350-355, 357-359 | `_block_mentions`'s per-node gate (dict keys, `==`/`!=`, `in`/`not in`, `and`/`or`) and `walk(value)`/`walk(item)`/`walk(blocks)` each replaced with `walk(None)` | `test_block_mentions_backstop_walk_and_gate` |
| 368-372, 373-375 | digest bot-warning condition (dict keys/operators) and its message text | `test_parse_backstop_digest_bot_warning` |
| 376, 378, 380-383 | digest channel-mismatch warning condition and message text | `test_parse_backstop_digest_channel_mismatch_warning` |
| 386, 388\*, 390\*, 391, 393 | human-test bot_id/subtype/user filter (dict keys, value, `and`/`or` precedence) | `test_parse_backstop_human_test_gate` (\*388/390 remain equivalent regardless — see Allowlist) |
| 412-414, 416-421 | `parse()`'s `file_sigs` filter: dict keys, mimetype prefix, `is_tombstoned` check (3 flips), `and`->`or`, whole expression nulled | `test_parse_backstop_file_sigs_filter` |
| 430-432 | mention text/blocks-disagree warning message text | `test_parse_backstop_mention_disagreement_warning` |
| 439 | `Candidate`'s `missing_runs=0` default (in `parse()`'s return) | `test_parse_backstop_missing_runs_default` |

### Runner fix: `--continue-on-collection-errors` (applies to all four files)

Discovered while investigating parse.py's `ts.py` mutant 231 (`_TS_RE` wrapped
so it never matches anything real): mutmut 2.5.1's `tests_pass()` treats
**any pytest exit code other than exactly 1** as "tests pass" (`return
returncode != 1`, `mutmut/__init__.py`). A mutation that breaks a
**module-level** call — e.g. `test_properties.py`'s `SEASON =
Semester(..., parse_ts(...))`, evaluated at import time — makes pytest exit
**2** (collection error), not 1, so mutmut misreads a catastrophically
broken mutant as SURVIVED. Verified by hand: applying mutant 231 and running
the runner command directly gave `ERROR collecting
tests/oracle/test_properties.py` and exit code 2; adding
`--continue-on-collection-errors` made pytest still execute whatever DID
collect and fold the collection error into the normal failure count, giving
exit code **1** — which mutmut correctly reads as KILLED. The flag was added
to the runner in both `pyproject.toml` `[tool.mutmut]` and
`tests/mutation/mutmut_config.py` (kept in sync, matching the file's own
convention). Verified harmless on the happy path: the full runner still
passes unchanged (160 -> 180 -> now more, growing with each file's backstop
additions) in ~2-3s. This fix is why `ts.py`'s mutant 231 (and likely some
`aggregate.py`/further `rules.py` mutants that would only manifest as
collection errors, though none of `rules.py`'s were — see that section)
now classifies correctly.

## Run: `snipebot/aggregate.py` — COMPLETE (zero unjustified survivors)

226-228 mutants generated depending on the pass (cache-reconciliation
variance, same phenomenon noted throughout this doc); the final, individually
re-verified state is authoritative: **206 killed, 7 justified equivalent.**
12 ids across the run (528-538, 540) were phantom cache rows with no real
mutation behind them — same `mutmut show`/`run` "Obtained null mutant for
pk" numbering gap as `rules.py`'s id 112 and `parse.py`'s 263-264 — and are
not counted as survivors.

First full pass: 7m6s wall time, 65 recorded as SURVIVED (12 phantom + 53
real). All 53 real survivors were closed with 19 new backstop tests (pin
table below); re-running each individually afterward gave 46 killed and 7
provably equivalent (454, 464, 470, 511, 513, 604, 606 — see Allowlist).

### Backstop pin table (`aggregate.py`)

| Ids | Mutation | Backstop test |
|---|---|---|
| 442, 443 | `UNGROUPED` literal wrapped/nulled | `test_ungrouped_backstop_value` |
| 444, 445, 446 | `_NO_SNIPE_US = 1 << 62` tampered three ways (`2<<62`, `1>>62`=0, `1<<63`) | `test_no_snipe_us_backstop_value` |
| 448, 450, 452, 487, 491, 507, 526, 598, 630, 659 | `@dataclass(frozen=True)` -> `frozen=False` on all ten of this module's dataclasses | `test_aggregate_dataclasses_backstop_are_frozen` |
| 543, 544, 545, 546 | `_per_member`'s zero-members sentinel (`Fraction(-1)` tampered two ways, plus two conditions that turn the guard into a `ZeroDivisionError`) | `test_per_member_backstop_zero_members_sentinel` |
| 524 | `build_people_table`'s row sort: `-r.times_sniped` -> `+r.times_sniped` | `test_people_table_backstop_times_sniped_tiebreak` |
| 574 | `into_from_other`'s cross-group filter: `!=`->`==` | `test_groups_table_backstop_cross_group_only` |
| 583, 584, 585, 588 | `build_groups_table`'s `touches` gate (`>0`->`>=0`/`>1`, first `or`->`and`) and `continue`->`break` | `test_build_groups_table_backstop_touches_gate` |
| 590, 591, 592 | `build_groups_table`'s own internal `sort_key` (three sign flips, distinct from `rank_groups`'s copy of the same logic) | `test_build_groups_table_backstop_sort_key` |
| 602, 605, 608, 609 | `_top_sniper_of`: target filter inverted, ranking-increment inverted, `earliest[...]` nulled (crashes comparing `None<None` on a tie), ranking sign inverted | `test_top_sniper_of_backstop_picks_highest_count` |
| 615 | `build_most_sniped_table`'s final tie-break: `kv[0]`->`kv[1]` (an unorderable list) | `test_most_sniped_table_backstop_final_tiebreak` |
| 647 | `rank_pairs`: `-r.count`->`+r.count` | `test_rank_pairs_backstop_order` |
| 649, 650, 651, 652, 653, 654, 655, 656, 657, 658 | `rank_groups`: the UNGROUPED filter flipped twice, all four sort-key term signs, `key`->`None`, `real`->`None`, and `+`->`-` combining the two result tuples | `test_rank_groups_backstop_full_order` |
| 661, 662 | `top_n_cutoff`: the no-overflow boundary (`<=`->`<`) and the cutoff row index (`top_n-1`->`top_n+1`) | `test_top_n_cutoff_backstop_boundary` |

Root cause, as with the other three files: these are config/data-shape
combinations (zero-member groups, opted-out sole members, participation
points decoupled from "made" counts, exact ties on multiple sort keys at
once) that the oracle/property strategies either cannot reach at all (they
target `rules.py`'s `evaluate()`, not `aggregate.py`'s pure table builders,
which have no oracle counterpart) or would need many more Hypothesis
examples than the reduced mutation-run budget affords. Every one is closed
with a direct, deterministic unit test on the relevant pure function, per
spec 6.3.

## Allowlist (equivalent mutants)

**Run 1** (ids 56, 58, 60-66, 69, 71-74): none needed there — every survivor
was closed with a backstop test rather than argued equivalent.

**Run 2**, `rules.py`, 5 ids — all provably equivalent, none killable by any
test (arguments below), per spec 6.3's "equivalent mutant" disposition:

- **78** — `_local_date_str`: `ts_us // US_PER_SECOND` -> `ts_us / US_PER_SECOND`
  (floor division to true division) inside `datetime.fromtimestamp(...,
  tz=utc).astimezone(tz).strftime("%Y-%m-%d")`. Both `ts_us` and
  `US_PER_SECOND` are non-negative, so true division only ever ADDS the
  discarded sub-second fraction (`0 <= frac < 1.0`) on top of the floor
  value; the resulting instant never crosses a second boundary (let alone a
  day boundary), so `%Y-%m-%d` is byte-identical either way for every
  timestamp in this codebase's realistic range. Verified both by proof (the
  addition is strictly less than one second) and empirically: 200,000
  random `ts_us` samples across `[0, 4e15]` microseconds (the `_SEM`
  semester's own range, ~year 1970-2096) produced zero mismatches.
- **82** — `_late_tag`: local variable annotation `edit_ts: str | None = None`
  -> `edit_ts: str & None = None`. `rules.py` has `from __future__ import
  annotations` (PEP 563): ALL annotations, including local variable
  annotations inside a function body, are stored as unevaluated strings and
  never executed at runtime. Confirmed empirically: `str & None` (which
  would raise `TypeError: unsupported operand type(s) for &` if evaluated,
  since neither `str` nor `type` defines `__and__`) runs with no error under
  `from __future__ import annotations`. No test — mutation or otherwise —
  can observe a difference, because the annotation expression is never
  reached.
- **154, 199, 208** — the same class of mutation (`|`->`&` in a local
  variable annotation: `gate: Reason | None`, `sweep: dict[str, tuple[...,
  str | None]]`, `pair_sr: list[tuple[..., str | None]]`), same proof as 82.

No new allowlist entries were needed for `rules.py` beyond these 5; every
other survivor/timeout/suspicious/untested id was closed with a backstop
test (pin table above).

**`ts.py`, 1 id:**

- **226** — `Ts = int` -> `Ts = None`. `Ts` is a module-level type alias used
  ONLY in annotation positions (`def parse_ts(s: str) -> Ts:`, `def
  format_ts(t: Ts) -> str:`); `ts.py` also carries `from __future__ import
  annotations`, so those annotations are never evaluated, and `Ts` itself is
  never imported or dereferenced as a runtime value anywhere in `snipebot/`
  or `tests/` (checked by grep). Rebinding it to `None` is unobservable by
  any test.

**`parse.py`, 6 ids:**

- **333, 337** — `_is_live`/`_is_live_video`: `f.get("mimetype", "")` ->
  `f.get("mimetype", "XXXX")`. The default only applies when the `"mimetype"`
  key is absent, and the sole use of the result is
  `.startswith("image/")`/`.startswith("video/")`; `""` and `"XXXX"` both
  fail that check identically, so no input (key present or absent) can ever
  distinguish the two defaults.
- **415** — the same default-value pattern inside `parse()`'s `file_sigs`
  filter (`f.get("mimetype", "")` -> `f.get("mimetype", "XXXX")`), same
  argument: only matters when the key is absent, and both defaults fail
  `.startswith("image/")` identically.
- **423** — `_text_mentions(message.get("text", ""))` -> `message.get("text",
  "XXXX")`. Same shape again: the default only matters when `"text"` is
  absent, and `_text_mentions` finds zero `<@U...>` mentions in `""` and in
  `"XXXX"` alike.
- **388, 390** — the human-message filter's `message.get("subtype") ==
  "bot_message"` clause (key mutated in 388, value mutated in 390). This
  clause is redundant with the unconditional, unmutated post-shape gate a
  few lines later (`if subtype not in _POST_SHAPE_SUBTYPES:` where
  `_POST_SHAPE_SUBTYPES = frozenset({None, "file_share",
  "thread_broadcast"})`): **any** message whose real `subtype` is
  `"bot_message"` is rejected by the post-shape gate regardless of whether
  the human-test clause fires, and any message whose subtype is not
  `"bot_message"` fails the human-test clause identically under the
  original and the mutant. Verified directly: constructing a message with
  `subtype="bot_message"` and no `bot_id` returns `None` under both the
  unmutated code and mutants 388/390 alike (confirmed via individual
  `mutmut run <id>` after the backstop test targeting this clause still
  came back SURVIVED) — the only place a difference COULD show is the
  human-test's own return path, but the post-shape gate downstream
  independently produces the same `None` outcome first.

**`aggregate.py`, 7 ids:**

- **454** — `_local_parts`: the identical floor-vs-true-division mutation as
  rules.py's id 78, on the SAME `datetime.fromtimestamp(...)` pattern, here
  also feeding a `"%H:%M:%S"` format. Same proof: adding the discarded
  sub-second fraction never crosses a whole-second boundary, so both the
  date and the time string are byte-identical either way.
- **464** — `eligible_snipes`: `continue`->`break` in `for pair in
  mv.pairs: if not semester.contains(ts_us): continue`. Every `PairVerdict`
  in one message's `mv.pairs` shares the SAME message `ts` (hence the same
  `ts_us`), so `semester.contains(ts_us)` evaluates identically for every
  pair of that message — if it fails on one pair it fails on all of them.
  `break`ing out of the remaining pairs of that message therefore skips
  nothing that `continue` would not also have skipped; no input can make
  the two behave differently.
- **470** — `RejectedAttempt`'s `blocked_by=pair.blocked_by or ""` ->
  `or "XXXX"`. Every code path in `rules.py`'s `admit()` that sets
  `anchor[key]` also sets `anchor_ts[key]` in the same block, so
  `anchor_ts.get(key)` (the source of `pair.blocked_by`) is never `None`
  for a genuine `Status.COOLDOWN` pair — the `or` fallback is unreachable
  dead code via any real `evaluate()`-produced verdict, so its value can
  never be observed.
- **511, 513** — `_best_day`'s `per_day[s.date] = per_day.get(s.date, 0) +
  1` tampered to `get(s.date, 1) + 1` (511) and `+ 2` (513). Traced by
  induction: 511's wrong default only affects the FIRST occurrence of each
  date, but every later occurrence reads the (already one-too-high) real
  stored value and adds the correct `+1`, so after N occurrences the count
  is uniformly `N+1` for EVERY date — a constant additive shift that
  preserves both the argmax and every tie (`_best_day` only ever compares
  dates' counts to each other, never against an absolute threshold). 513's
  `+2` similarly makes every count `2N` — a uniform positive scaling, which
  likewise preserves every relative ordering and tie. Neither is
  observable through `_best_day`'s return value.
- **604, 606** — the identical pattern one function over, in
  `_top_sniper_of`'s `counts[s.sniper] = counts.get(s.sniper, 0) + 1`:
  604's wrong default (`1`) yields uniform `N+1` per sniper; 606's `+2`
  yields uniform `2N`. Same proof as 511/513 — `_top_sniper_of` only ever
  ranks snipers relative to each other via `min(counts, key=...)`, never
  against an absolute value, so both are unobservable. (Contrast with 605
  and 609, which DO change the ranking outcome and are killed by
  `test_top_sniper_of_backstop_picks_highest_count`.)

## Decisions

- Functional mutmut config (`paths_to_mutate`, `runner`) lives in
  `pyproject.toml` `[tool.mutmut]`, not only in `tests/mutation/mutmut_config.py`:
  mutmut imports a plain top-level `mutmut_config` module off the current
  working directory (for the `pre_mutation`/`pre_mutation_ast` hooks only) and
  reads `paths_to_mutate`/`runner` via its own `config_from_file` loader against
  `pyproject.toml`/`setup.cfg`, never against a nested module path. Both files
  carry the same values; the nested module is kept because it is where a reader
  following the test matrix looks first.
- The runner's venv interpreter path uses backslashes
  (`.venv\Scripts\python.exe`), not forward slashes: mutmut spawns the runner
  with `shell=True`, which on Windows is `cmd.exe`, and `cmd.exe` fails to
  resolve a leading `.venv/Scripts/...` as an executable.
- `--simple-output` is passed on every mutmut invocation: the default emoji
  legend raises `UnicodeEncodeError` on the Windows `cp1252` console.
- `L4-MU-join-date` and `L4-MU-semester-end` mutate `config.py`, which section
  6.1 excludes from the mutmut scope ("covered by their own suites"). They are
  still pinned here per the matrix table 2.4, using the killer test the table
  names, since the point is to record the pin, not to force those two mutants
  into a scope the spec deliberately keeps them out of.
- The runner order in both `pyproject.toml` and `mutmut_config.py` is
  `test_mutation_backstop.py` first, then the fixture/unit files, then the two
  Hypothesis-heavy oracle files last — a reorder from the spec's own listing
  order, not a change of scope (same test set). Rationale and measured effect
  are in `mutmut_config.py`'s comment; in short, it turns a ~50s-per-mutant
  cost for anything only the backstop file catches into <1s, which is most of
  why 74 mutants could be tested at all in the first run.
- `mutmut run` is invoked via the installed `mutmut.exe` console-script, not
  `python -m mutmut`: both work, but `-m mutmut`'s multiprocessing "spawn"
  bootstrap on Windows re-executes the full CLI (`-m mutmut run ...`) as an
  intermediate step before reaching the real worker, which is harmless but
  adds a visible extra process per mutant; the console-script entry point
  does the same re-exec against the script path instead, which is no faster
  but was easier to reason about while debugging the process tree.
- mutmut mutates `snipebot/rules.py` **on disk**, keeping the original at
  `snipebot/rules.py.bak` only for the duration of one mutant's test run
  (`finally: move(.bak, rules.py)` restores it, including on a caught
  interrupt — but not on a hard kill, which skips the `finally`). Never
  force-kill a `mutmut run` without first checking for
  a stray `<path>.bak` next to the mutated file and restoring it if present;
  a run hit that exact failure mode once (recovered by diffing the
  restored line against `git show HEAD`) before switching to
  checking for `.bak` before any kill.
