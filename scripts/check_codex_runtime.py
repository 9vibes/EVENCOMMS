"""No credentials or live inference: verify pinned Codex using loopback fixtures."""

import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_bridge.probe import run_probe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", default="/usr/local/bin/codex")
    parser.add_argument("--temp-parent", default="/tmp/opencode")
    args = parser.parse_args()
    report = asyncio.run(run_probe(args.binary, temp_parent=args.temp_parent))
    print(json.dumps(report, indent=2))
    return 0 if report["generation_enabled"] else 1


if __name__ == "__main__":
    sys.exit(main())
