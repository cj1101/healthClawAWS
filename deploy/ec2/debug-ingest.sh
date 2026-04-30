#!/usr/bin/env bash
PW='Sunf1reCar!!'
BASE="http://127.0.0.1:8000"
COOKIE=/tmp/nc_cookies2.txt
rm -f "$COOKIE"

echo "=== login ==="
curl -s -c "$COOKIE" -b "$COOKIE" -X POST "${BASE}/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"password\":\"${PW}\"}"

echo ""
echo "=== data ingest verbose ==="
curl -s -c "$COOKIE" -b "$COOKIE" -X POST "${BASE}/v1/data/ingest" \
  -H "Content-Type: application/json" \
  -d '{"domain":"smoke_test_domain","payload":{"value":42},"source":"smoke_test","confidence":0.9}'

echo ""
echo "=== journal last 20 lines ==="
journalctl -u nemoclaw-health -n 20 --no-pager
