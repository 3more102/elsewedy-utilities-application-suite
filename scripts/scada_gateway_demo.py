#!/usr/bin/env python3
"""Send a small authenticated telemetry batch to EUAS using an integration API key.

Environment variables:
  EUAS_BASE_URL         default: http://127.0.0.1:8000
  EUAS_INTEGRATION_KEY  required; create one in Utility Command Center as admin
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE = os.getenv('EUAS_BASE_URL', 'http://127.0.0.1:8000').rstrip('/')
KEY = os.getenv('EUAS_INTEGRATION_KEY', '').strip()


def main() -> int:
    if not KEY:
        print('ERROR: set EUAS_INTEGRATION_KEY to a telemetry:write integration key.', file=sys.stderr)
        return 2
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    nonce = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    payload = {
        'source_system': 'SCADA-Gateway-Demo',
        'idempotency_key': f'demo-{nonce}',
        'readings': [
            {'channel_code': 'TEL-TR001-OIL-TEMP', 'value': 82.4, 'quality': 'Good', 'external_id': f'{nonce}-oil-temp', 'captured_at': stamp},
            {'channel_code': 'TEL-TR001-LOAD', 'value': 73.5, 'quality': 'Good', 'external_id': f'{nonce}-load', 'captured_at': stamp},
            {'channel_code': 'TEL-PMP301-VIB', 'value': 3.4, 'quality': 'Good', 'external_id': f'{nonce}-vib', 'captured_at': stamp},
        ],
    }
    req = urllib.request.Request(
        BASE + '/api/telemetry/ingest',
        data=json.dumps(payload).encode(),
        method='POST',
        headers={'Content-Type': 'application/json', 'X-EUAS-Integration-Key': KEY, 'User-Agent': 'EUAS-SCADA-Demo/4.0'},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        print(f'HTTP {exc.code}: {exc.read().decode(errors="replace")}', file=sys.stderr)
        return 1
    print(json.dumps(data, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
