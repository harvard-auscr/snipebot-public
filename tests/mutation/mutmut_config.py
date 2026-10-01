"""Mutation-testing configuration record (50-test-matrix.md section 6.1).

mutmut 2.5.1 imports a module literally named `mutmut_config` off the current
working directory (for the `pre_mutation`/`pre_mutation_ast` hooks only) and
reads `paths_to_mutate`/`runner` from `[tool.mutmut]` in pyproject.toml (or
`[mutmut]` in setup.cfg) via its own `config_from_file` loader. It does not
read those two settings from a nested `tests/mutation/mutmut_config.py`, so
the values actually in force live in pyproject.toml; this module holds the
same values for reference and so this file matches what the spec names.

Mutated: the four pure modules the mutation workstream owns.
Excluded: sync.py, slack_io.py, report.py rendering, config.py, ledger.py,
faces.py (covered by their own suites; see the module docstring rationale
below for faces.py specifically).
"""

from __future__ import annotations

paths_to_mutate = [
    "snipebot/ts.py",
    "snipebot/parse.py",
    "snipebot/rules.py",
    "snipebot/aggregate.py",
]

# The fast rules-scoped subset: pure-core unit/fixture tests, oracle equivalence
# and property tests, the report-table builder tests, and this package's own
# backstop tests. Never the full suite per mutant (sync/slack/rig/crash-matrix
# tests are excluded: they add wall time without added kill power over the
# oracle, per the DECISION in 50-test-matrix.md section 6.1).
runner = (
    r".venv\Scripts\python.exe -m pytest -q -x --continue-on-collection-errors "
    "tests/mutation/test_mutation_backstop.py tests/test_ts.py tests/test_parse.py "
    "tests/test_rules_fixtures.py tests/test_aggregate.py tests/test_report_tables.py "
    "tests/oracle/test_oracle_equiv.py tests/oracle/test_properties.py"
)
# Backslashes in the venv interpreter path are load-bearing: mutmut spawns the
# runner with shell=True, which on Windows is cmd.exe, and cmd.exe fails to
# resolve a leading ".venv/Scripts/..." (forward slashes) as an executable.
#
# --continue-on-collection-errors is also load-bearing: mutmut 2.5.1's
# tests_pass() treats any pytest exit code other than exactly 1 as "survived"
# (`return returncode != 1`). A mutation that breaks a module-level call (e.g.
# test_properties.py's `SEASON = Semester(..., parse_ts(...))` at import time)
# makes pytest exit 2 (collection error), which mutmut misreads as SURVIVED
# even though the mutation is catastrophically broken. This flag makes pytest
# still run whatever DID collect and fold the collection error into the normal
# failure count (exit 1), so mutmut classifies these correctly as KILLED.
#
# Order is a speed optimization over the spec's literal listing order, not a
# change of which tests run: test_mutation_backstop.py goes FIRST because it
# is the cheapest test (a handful of dict-equality assertions, no fixtures,
# sub-second) and is exactly the file that kills mutants nothing else in this
# list reaches (enum-literal mutations invisible to identity comparisons,
# section 6.3's "backstop" case) -- running it last under `-x` meant every
# such mutant paid the full ~50s cost of the fixture + oracle suites before
# failing on the one assertion that actually catches it. The two Hypothesis-
# heavy oracle files move to the end for the same reason, in reverse: they are
# the slowest and, since 00-data/oracle already agree with rules.py on every
# branch a property strategy reaches, they are also the ones least likely to
# be the mutant's first failure.

# faces.py holds no rules -- it fetches, decodes and counts; the selfie
# classification lives in rules.py and is mutated there (zero survivors), so
# mutating faces.py would only test the detector adapter, not a game rule.

# tests/oracle/test_properties.py and tests/oracle/test_oracle_equiv.py read
# their Hypothesis example count from SNIPEBOT_HYPOTHESIS_EXAMPLES (module
# constant _EXAMPLES in each file), falling back to that file's original
# literal (120 and 200 respectively) when the variable is unset. A mutation
# run sets it low in the shell before invoking mutmut so child pytest runs
# inherit it, without changing either file's default for normal test runs.
# tests/oracle/test_schedule_replay.py carries no @settings and is unaffected.
