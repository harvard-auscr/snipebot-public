"""Round-2 adversarial sweep of ``snipebot.aggregate`` (hostile / malformed input).

This module is the landing place for round-2 red-team findings against
``spec/30-aggregate-report.md`` sections 1-3 (the single boundary, the six table
builders, ranking and the ``top_n`` cutoff). Each finding would appear here as one
``test_<slug>`` that FAILS on the current code, with a docstring quoting the spec
sentence it rests on.

No such test is present: the round-2 sweep did not find a reproducible spec
violation in ``aggregate.py``. The territory was probed both by hand-built minimal
ledgers driven through ``eligible_snipes`` and by two randomized fuzzers (8000
ledgers total, both ``count_intra_group`` modes, override-selfies, pair- and
target-scope cooldowns, retroactive opt-outs, 1-3 target selfies producing
partial-cooldown selfie messages). Every probe asserted the spec's invariants and
all held:

- both conservation identities, in both units, on adversarial ledgers
  (selfie with 3+ sibs, selfie with a non-sib also tagged, a SELFIE message with a
  pair in cooldown): ``N`` across ``daily``/``people``/``pairs``; the group
  ``made``/``sniped`` form under each ``count_intra_group`` mode
  (``sum(made) == N`` / ``N - I``, ``intra + cross == made`` / ``cross == made``);
  the points identity ``Σ people.points == Σ groups.points == P + F + Q ==
  Σ pairs.points + F == Σ daily.points``; and ``pairs.points == count`` per row;
- ``most_sniped`` standard competition ranking (the 1,2,2,4 pattern) and
  ``top_sniper_of_them`` tie order (count desc, earliest pair-snipe asc, sniper ID
  asc), including full ties resolved by ID;
- ``groups`` row-sort order per 30 section 2.4 (points_per_member, made_per_member,
  points, made, group asc; ``members == 0`` sorting as if -1; ``(ungrouped)`` last),
  and the drop of a real group that has neither a member nor a counted snipe (the
  round-1 finding, now fixed);
- ``best_day`` peak with the earliest-date tiebreak; ``daily`` bucketing on a
  DST-transition local day; the inclusive semester filter at both edges;
- ``top_n_cutoff`` with fewer rows than ``n``, exactly ``n``, and an all-tied
  boundary; and full determinism of every table across shuffled input order.

The one spec-level issue surfaced by this sweep (the ``groups`` ranking
earliest-member-snipe tiebreak: 30 section 3 lists it as ranking key 2 for the
``groups`` context and its DECISION says keys 1-2/4 are computed name-free in
``aggregate.py``, while 30 section 2.4 writes the ``groups`` key out explicitly as
ending in ``group`` asc with no earliest-snipe key and calls it "the same key the
ranked digest groups section uses") is reported as a spec issue, not a code
finding, because it is a contradiction between two spec sections rather than a
place the code disagrees with an unambiguous requirement.
"""

from __future__ import annotations

# No round-2 findings against aggregate.py. See the module docstring for the sweep
# that was run and the single spec contradiction reported separately.
