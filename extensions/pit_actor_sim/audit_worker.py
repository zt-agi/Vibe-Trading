"""Fixed read-only audit entry point; never bootstrap or hydrate the lake."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    from pitdb import config as config
    from pitdb.audit import run_audit
    from pitdb.db import connect

    expected = Path(sys.argv[1]).resolve(strict=True)
    lake = Path(sys.argv[2]).resolve(strict=True)
    if config.DB_PATH is None or config.DB_PATH.resolve(strict=True) != expected:
        raise RuntimeError("Audit child index path mismatch")
    if config.LAKE_ROOT.resolve(strict=True) != lake:
        raise RuntimeError("Audit child lake path mismatch")
    if os.name == "nt" and expected.drive.upper() != "E:":
        raise RuntimeError("Audit index must be on E:")
    con = connect(read_only=True, wait_minutes=0)
    try:
        return 0 if run_audit(con, verbose=True) else 1
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
