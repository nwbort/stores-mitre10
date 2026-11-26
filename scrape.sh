#!/bin/bash
# This script scrapes store details from the Mitre 10 website.

set -e

echo "Scraping store data from https://www.mitre10.com.au/stores"

# Cloudflare protects this site with a managed challenge, so drive a real
# Chrome via nodriver to clear it (see scrape.py). xvfb-run gives Chrome a
# display so it runs headful, which is far less likely to be flagged.
# Output is a JSON array of store objects from /storelocatorstate/store/offers.
xvfb-run -a python3 scrape.py /tmp/stores_raw.json

# Remove verbose schedule.calendar noise if present (no-op if key is absent).
jq 'map(if .schedule then .schedule |= del(.calendar) else . end)' /tmp/stores_raw.json > stores.json

# Sanity check
if [ ! -s stores.json ] || [ "$(cat stores.json)" = "null" ] || [ "$(cat stores.json)" = "[]" ]; then
    echo "Error: stores.json is empty or null after extraction."
    exit 1
fi

COUNT=$(jq 'length' stores.json)
echo "Successfully extracted $COUNT stores, saved to stores.json"
