#!/usr/bin/env bash
set -euo pipefail

BASE="http://127.0.0.1:8000"
COOKIE=/tmp/nc_cookies.txt
rm -f "$COOKIE"

pass() { echo "  PASS: $1"; }
fail() { echo "  FAIL: $1"; exit 1; }

echo ""
echo "=== 1. healthz ==="
R=$(curl -sf "${BASE}/healthz") && echo "$R" && pass "healthz" || fail "healthz"

echo ""
echo "=== 2. dashboard HTML ==="
CODE=$(curl -s -o /dev/null -w "%{http_code}" "${BASE}/")
[ "$CODE" = "200" ] && pass "dashboard index ($CODE)" || fail "dashboard index ($CODE)"

echo ""
echo "=== 3. static assets ==="
CODE=$(curl -s -o /dev/null -w "%{http_code}" "${BASE}/assets/style.css")
[ "$CODE" = "200" ] && pass "style.css" || fail "style.css ($CODE)"
CODE=$(curl -s -o /dev/null -w "%{http_code}" "${BASE}/assets/app.js")
[ "$CODE" = "200" ] && pass "app.js" || fail "app.js ($CODE)"

echo ""
echo "=== 4. auth login ==="
PW='Sunf1reCar!!'
R=$(curl -sf -c "$COOKIE" -b "$COOKIE" -X POST "${BASE}/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"password\":\"${PW}\"}") && echo "$R" && pass "login" || fail "login"

echo ""
echo "=== 5. profile ==="
R=$(curl -sf -c "$COOKIE" -b "$COOKIE" "${BASE}/v1/profile") && echo "$R" && pass "profile" || fail "profile"

echo ""
echo "=== 6. storage summary ==="
R=$(curl -sf -c "$COOKIE" -b "$COOKIE" "${BASE}/v1/storage/summary") && echo "$R" && pass "storage summary" || fail "storage summary"

echo ""
echo "=== 7. WHOOP status ==="
R=$(curl -sf -c "$COOKIE" -b "$COOKIE" "${BASE}/v1/connectors/whoop/status") && echo "$R" && pass "whoop status" || fail "whoop status"

echo ""
echo "=== 8. Apple Health status ==="
R=$(curl -sf -c "$COOKIE" -b "$COOKIE" "${BASE}/v1/connectors/apple-health/status") && echo "$R" && pass "apple status" || fail "apple status"

echo ""
echo "=== 9. timeline ==="
R=$(curl -sf -c "$COOKIE" -b "$COOKIE" "${BASE}/v1/timeline?limit=5") && echo "$R" && pass "timeline" || fail "timeline"

echo ""
echo "=== 10. data domain register ==="
R=$(curl -sf -c "$COOKIE" -b "$COOKIE" -X POST "${BASE}/v1/data/domain" \
  -H "Content-Type: application/json" \
  -d '{"display_name":"smoke_test_domain","schema_hint":["value"]}') && echo "$R" && pass "domain register" || fail "domain register"

echo ""
echo "=== 11. data ingest ==="
R=$(curl -sf -c "$COOKIE" -b "$COOKIE" -X POST "${BASE}/v1/data/ingest" \
  -H "Content-Type: application/json" \
  -d '{"domain":"smoke_test_domain","payload":{"value":42},"source":"manual","confidence":0.9}') \
  && echo "$R" && pass "data ingest" || fail "data ingest"

echo ""
echo "=== 12. debug sessions ==="
R=$(curl -sf -c "$COOKIE" -b "$COOKIE" "${BASE}/v1/debug/sessions") && echo "$R" && pass "debug sessions" || fail "debug sessions"

echo ""
echo "=== 13. debug analyze (env) ==="
R=$(curl -sf -c "$COOKIE" -b "$COOKIE" -X POST "${BASE}/v1/debug/analyze" \
  -H "Content-Type: application/json" \
  -d '{}') && echo "$R" && pass "debug analyze" || fail "debug analyze"

echo ""
echo "=== 14. chat (popeye) ==="
R=$(curl -sf -c "$COOKIE" -b "$COOKIE" -X POST "${BASE}/v1/chat" \
  -H "Content-Type: application/json" \
  -d '{"message":"Hello, what can you help me with?"}') && echo "$R" && pass "chat" || fail "chat"

echo ""
echo "============================"
echo "ALL SMOKE TESTS PASSED"
echo "============================"
