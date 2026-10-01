"""The L6 self-driving rig (50-test-matrix.md section 7, PLAN.md section 9 L6).

A script that posts the ordered scenario as a real human on a throwaway workspace
(user token), runs the real `snipebot sync` against the real API, and reads
everything back through the bot token to assert ledger rows, on-message reactions,
the posted digests and their metadata, the `post_to` second channel, and API-vs-
phone candidate parity.

Everything in this package that talks to Slack refuses cleanly when the five rig
env vars are absent (`rig_scenario.missing_env`): there are no tokens in CI, so
the live entry (`test_rig.py`) skips and only the offline parts -- `plan()`,
`render_config()`, and the `assertions` helpers over canned read-back dicts --
run. Logs and stored files carry user IDs only; tokens come from the environment
and are never printed.
"""
