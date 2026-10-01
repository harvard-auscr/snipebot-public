"""L2-OR-equiv: the production sweep and the independent oracle must agree, element
for element, on every generated timeline -- status, reason, blocked_by and selfie per
pair, plus the message-level selfie class and message status/reason.
"""
from __future__ import annotations

import os

from hypothesis import HealthCheck, given, settings

from snipebot.rules import evaluate as production_evaluate
from tests.oracle import oracle
from tests.oracle.strategies import scenarios

# Overridable Hypothesis example count (default matches this file's original
# literal). A mutation-testing run sets SNIPEBOT_HYPOTHESIS_EXAMPLES lower so
# this equivalence test still kills mutants without the default per-test cost.
_EXAMPLES = int(os.environ.get("SNIPEBOT_HYPOTHESIS_EXAMPLES", "200"))


def _diff(a, b) -> str:
    if len(a) != len(b):
        return f"length {len(a)} != {len(b)}"
    for x, y in zip(a, b):
        if x != y:
            return (
                f"ts={x.ts}\n"
                f"  oracle: status={x.status.value} reason={x.reason.value} "
                f"selfie={x.selfie.value} "
                f"pairs={[(p.target, p.status.value, p.reason.value, p.blocked_by, p.selfie) for p in x.pairs]}\n"
                f"  prod:   status={y.status.value} reason={y.reason.value} "
                f"selfie={y.selfie.value} "
                f"pairs={[(p.target, p.status.value, p.reason.value, p.blocked_by, p.selfie) for p in y.pairs]}"
            )
    return "equal"


@settings(max_examples=_EXAMPLES, deadline=None,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(sc=scenarios())
def test_production_equals_oracle(sc):
    got = production_evaluate(*sc.eval_args())
    want = oracle.evaluate(*sc.eval_args())
    assert got == want, _diff(want, got)
