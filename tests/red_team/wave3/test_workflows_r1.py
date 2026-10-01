"""Wave-3 red-team, Round 1 (SPEC CONFORMANCE) — infra files under attack:

    .github/workflows/sync.yml
    .github/workflows/admin.yml
    slack-app-manifest.yaml
    requirements.txt / requirements-dev.txt
    pyproject.toml
    README.md
    .gitignore

Result: NO provable break was found. The three YAML files are byte-identical to the
spec's embedded blocks (spec/40-config-cli.md §7.1/§7.2/§7.3) apart from comment
wording; requirements*.txt reproduce §6.2/§6.3 verbatim; pyproject dependencies and
[tool.mutmut] mirror §6.2 and 50-test-matrix.md §6.1; the manifest carries exactly the
seven bot scopes of plan §5 (no user scopes) and doctor reads that same file as ground
truth for DOC-SCOPES; the README carries the face-count disclosure sentence; and no
code path hardcodes "Shoutout". Every angle in the brief was exercised and passed, so
by the rule "a test that passes is not a finding" this file declares no findings.

The one genuine issue is a spec/plan CONTRADICTION, reported under spec_issues, not as a
finding: sync.yml's cron is hourly ("0 * * * *"), matching spec §7.1, whose comment cites
"owner default, plan §13" — but plan §13 records no cadence decision, and plan §8 states
the *accepted* v1 trade is "roughly a 30-minute cadence" (`*/30 * * * *`). The workflow
faithfully follows its build contract (the spec); the disagreement lives between the two
design documents, so no failing test is written against spec-conformant code.

No test functions: none of the ten-plus angles produced a rule the shipped files break.
"""
