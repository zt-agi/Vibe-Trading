"""Isolated PIT query process for the MCP adapter."""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import date, datetime

import server


def decode(item):
    kind = item["kind"]
    value = item["value"]
    if kind == "datetime":
        return datetime.fromisoformat(value)
    if kind == "date":
        return date.fromisoformat(value)
    if kind == "plain":
        return value
    raise ValueError("invalid argument encoding")


def main() -> int:
    payload = json.load(sys.stdin)
    allowed = {
        "c8dda43174cb3af7c737a5983e0c05654bf7b9277cbd22bee1e60506846ea432",
        "11c77d197d7b9142ff7177d1119f47093c25bc54dde3bda4e8594fa3a07728d6",
        "eb225370024711667e312d2745dfc720c8f4a42ced99dc9178708ae5bc6da841",
        # server.UNIVERSE_SQL: dim_security identities, read only to seal blind packets
        "3384a8dc06573cd1decb3e1f69daf3fad14c3e2c29d3bd299467268777ad3df3",
        # server.ISSUER_FACTS_SQL: an issuer's FY SEC XBRL facts through obs_asof (zt_style)
        "4b3d04432b6509becb4fc2b0268b1d77886e189728ff7b08c7b3a823bf7aa09d",
    }
    if hashlib.sha256(payload["sql"].encode("utf-8")).hexdigest() not in allowed:
        raise ValueError("query is not an approved PIT macro or security lookup")
    rows = server.query(payload["sql"], [decode(item) for item in payload["args"]])
    sys.stdout.write(json.dumps(rows, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())