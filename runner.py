"""GitHub Actions entry point for sms_grist_sync.

Reads SYNC_CID from the environment (default "all"), runs the engine,
prints the returned artifact JSON to stdout. Exits 0; per-sender failures
are reported inside the artifact, not via the exit code.
"""
import json
import os
import sys

import sms_grist_sync


class _Ctx:
    def __init__(self, cid):
        self.input = {"cid": cid}


def main():
    cid = os.environ.get("SYNC_CID", "all")
    result = sms_grist_sync.run(_Ctx(cid))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
