#!/bin/bash
# This script scrapes store details from the Mitre 10 website.

# Exit immediately if a command exits with a non-zero status.
set -e

# The URL of the stores page.
URL='https://www.mitre10.com.au/stores'

echo "Scraping store data from $URL"

# 1. Download the HTML page content using curl.
# 2. Use grep to find the specific script tag line containing the store locator JSON data.
# 3. Use sed to strip the opening and closing <script> tags, leaving only the raw JSON.
# 4. Use jq to parse the JSON and extract the 'markers' array, which contains the list of stores.
# 5. Save the pretty-printed JSON array output to stores.json.
curl -sL "$URL" \
  | grep '<script type="text/x-magento-init">.*"store-locator-search".*</script>' \
  | sed -e 's|<script type="text/x-magento-init">||' -e 's|</script>||' \
  | jq '."*".`"Magento_Ui/js/core/app"`.markers' > stores.json

# Check if the file was created and is not empty or just "null".
if [ ! -s stores.json ] || [ "$(cat stores.json)" = "null" ]; then
    echo "Error: Failed to extract store data. The resulting stores.json is empty or null."
    exit 1
fi

echo "Successfully extracted store data and saved to stores.json"
