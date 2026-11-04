from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from battle.agents import AgentError, Job  # noqa: E402
from tests.fake_team import FakeTeamAgent  # noqa: E402


def main() -> int:
    job = Job(os.environ["BATTLE_ROLE"], sys.stdin.read(), Path.cwd(), writes=True)
    try:
        print(FakeTeamAgent().run(job))
    except AgentError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
