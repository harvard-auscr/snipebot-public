"""Crash-matrix subprocess entry point (10-slack-io.md section 7, verbatim
shape). The harness launches `python -m tests._fake_entry` and kills it at a
step boundary; this is how the real `run_sync` gets pointed at a file-backed
fake Slack world that survives the kill. Test code only -- never shipped,
never imported by production snipebot modules.

`snipebot.sync` does not exist yet (a different workstream), so this module
cannot be executed end-to-end until it lands; the import and the call shape
below are written to match spec section 7 exactly so nothing here needs to
change once `run_sync` exists.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from snipebot.config import load_config
from snipebot.faces import FakeFaceDetector
from snipebot.sync import run_sync

from tests.fake_slack import FileBackedFakeSlack


def main() -> int:
    slack = FileBackedFakeSlack(path=os.environ["SNIPEBOT_FAKE_SLACK"])
    config = load_config(os.environ["SNIPEBOT_CONFIG"])          # 40 section 2.1: resolved Config
    detector = FakeFaceDetector(                                  # 10 section 9: sha256 hex -> face count
        json.loads(os.environ.get("SNIPEBOT_FAKE_FACES", "{}")),
    )
    # Step 9 (digest posting) is stubbed in this build, so the crash harness runs with
    # SNIPEBOT_NO_POST=1; --no-react is exposed the same way for completeness. Both default
    # off, so the verbatim spec-section-7 shape (no flags) is unchanged when they are unset.
    return run_sync(
        slack,
        config=config,
        detector=detector,                                       # 20 section 1.3: required, no default
        ledger_path=Path(os.environ["SNIPEBOT_LEDGER"]),
        state_path=Path(os.environ["SNIPEBOT_STATE"]),
        now_us=int(os.environ["SNIPEBOT_NOW_US"]),          # injected simulated clock, integer microseconds
        no_post=os.environ.get("SNIPEBOT_NO_POST") == "1",
        no_react=os.environ.get("SNIPEBOT_NO_REACT") == "1",
    ).exit_code


if __name__ == "__main__":
    raise SystemExit(main())
