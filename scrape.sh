#!/bin/bash
# This script scrapes store details from the Mitre 10 website.

set -e

echo "Scraping store data from https://www.mitre10.com.au/stores"

# Use Playwright (via scrape.py) to load the page in a real browser,
# intercept the dynamic AJAX response that contains store markers, and
# output the raw JSON array.
python3 scrape.py > /tmp/stores_raw.json

# Strip schedule.calendar noise and write final output.
jq 'map(del(.schedule.calendar))' /tmp/stores_raw.json > stores.json

# Sanity check
if [ ! -s stores.json ] || [ "$(cat stores.json)" = "null" ] || [ "$(cat stores.json)" = "[]" ]; then
    echo "Error: stores.json is empty or null after extraction."
    exit 1
fi

COUNT=$(jq 'length' stores.json)
echo "Successfully extracted $COUNT stores, saved to stores.json"
