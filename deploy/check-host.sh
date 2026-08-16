#!/usr/bin/env bash
#
# Run this on a candidate host BEFORE installing anything.
#
#   bash check-host.sh
#
# It answers the one question that cannot be answered from a laptop: whether
# this machine's IP address is allowed to read the pages at all. Essex sits
# behind a bot check that judges the network as well as the browser, and cloud
# ranges are treated more harshly than home connections. A real Chrome on a
# blocked IP still gets nothing.
#
# Takes about a minute. Changes nothing except installing chromium and curl.
set -uo pipefail

echo "==> where am I"
curl -s --max-time 15 https://ipinfo.io/json 2>/dev/null \
  | tr ',' '\n' | grep -E '"(ip|city|org)"' | sed 's/^/    /' || echo "    (could not determine)"

echo
echo "==> installing chromium and curl if needed"
if ! command -v chromium >/dev/null && ! command -v chromium-browser >/dev/null \
   && ! command -v google-chrome >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  sudo apt-get update -qq && sudo apt-get install -y -qq chromium curl >/dev/null 2>&1 \
    || sudo apt-get install -y -qq chromium-browser curl >/dev/null 2>&1
fi
BROWSER=$(command -v chromium || command -v chromium-browser || command -v google-chrome)
echo "    using: ${BROWSER:-NONE FOUND}"

echo
echo "==> Avalon (plain HTTP, no browser needed)"
AV=$(curl -s --compressed --max-time 40 -A "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36" \
  "https://www.avaloncommunities.com/washington/bellevue-apartments/avalon-meydenbauer/" | grep -c '"moveInDate"')
if [ "$AV" -gt 0 ]; then
  echo "    PASS — $AV unit price blocks found"
else
  echo "    FAIL — no unit data. Avalon may be blocking this IP."
fi

echo
echo "==> Essex (needs the browser, and needs this IP to be trusted)"
if [ -z "${BROWSER:-}" ]; then
  echo "    SKIP — no browser installed"
else
  OUT=$("$BROWSER" --headless --disable-gpu --no-sandbox --disable-dev-shm-usage \
        --blink-settings=imagesEnabled=false --virtual-time-budget=30000 --dump-dom \
        "https://www.essexapartmenthomes.com/apartments/bellevue/belcarra/floor-plans-and-pricing" 2>/dev/null)
  if echo "$OUT" | grep -q "Security Checkpoint"; then
    echo "    FAIL — bot check blocked this IP. Chrome is fine; the address is not."
    echo "           Try another region, or keep Essex on a home connection."
  elif echo "$OUT" | grep -q "available_units_count"; then
    N=$(echo "$OUT" | grep -o "available_units_count" | wc -l | tr -d ' ')
    echo "    PASS — page read, $N floorplan records present"
  else
    echo "    UNCLEAR — no bot check, but no availability data either."
    echo "           The page may have changed shape; send me the output."
  fi
fi

echo
echo "==> memory available"
free -m 2>/dev/null | sed 's/^/    /' || echo "    (free not available)"
echo
echo "If both say PASS, run install.sh. If Essex says FAIL, tell me and we will"
echo "split it: Avalon in the cloud, Essex somewhere with a home IP."
