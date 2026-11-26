#!/bin/bash
# This script scrapes store details from the Mitre 10 website.

set -e

echo "Scraping store data from https://www.mitre10.com.au/stores"

# Cloudflare protects this site with a managed challenge, so drive a real
# Chrome via nodriver to clear it (see scrape.py). xvfb-run gives Chrome a
# display so it runs headful, which is far less likely to be flagged.
# Output is the JSON array of store-locator markers from the /stores page.
xvfb-run -a python3 scrape.py /tmp/stores_raw.json

# Keep the same fields stores.json has always had, so diffs stay meaningful.
# (The old /storelocatorstate/store/offers API also gave "postcodes"; the
# markers don't include them.)
jq 'map({id: (.id | tonumber), name, address, latitude, longitude, phone}
        | with_entries(select(.value != null and .value != "")))
    | sort_by(.id)' /tmp/stores_raw.json > stores.json

# Sanity check
if [ ! -s stores.json ] || [ "$(cat stores.json)" = "null" ] || [ "$(cat stores.json)" = "[]" ]; then
    echo "Error: stores.json is empty or null after extraction."
    exit 1
fi

COUNT=$(jq 'length' stores.json)
echo "Successfully extracted $COUNT stores, saved to stores.json"
