"""One-time helper: map a legacy WHOOP v1 activity id to a v2 UUID via GET /v1/activity-mapping/{id}.

Requires a valid WHOOP OAuth access token (Bearer) with scopes that allow developer API access.

Usage:
  export WHOOP_ACCESS_TOKEN=...
  python scripts/whoop_map_v1_activity_id.py 12345678
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python scripts/whoop_map_v1_activity_id.py <activity_v1_id_integer>", file=sys.stderr)
        return 2
    try:
        v1_id = int(sys.argv[1])
    except ValueError:
        print("activity_v1_id must be an integer", file=sys.stderr)
        return 2
    token = (os.environ.get("WHOOP_ACCESS_TOKEN") or "").strip()
    if not token:
        print("Set WHOOP_ACCESS_TOKEN to a valid Bearer token.", file=sys.stderr)
        return 2
    try:
        import httpx
    except ImportError:
        print("Install httpx (pip install httpx)", file=sys.stderr)
        return 2
    url = f"https://api.prod.whoop.com/developer/v1/activity-mapping/{v1_id}"
    r = httpx.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if r.status_code == 404:
        print("404: no mapping for this v1 id")
        return 1
    if r.status_code >= 400:
        print(f"{r.status_code}: {r.text[:500]}")
        return 1
    data = r.json()
    v2 = data.get("v2_activity_id")
    print(v2 if v2 is not None else data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
