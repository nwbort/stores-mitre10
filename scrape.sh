#!/bin/bash
# This script scrapes store details from the Mitre 10 website.

# Exit immediately if a command exits with a non-zero status.
set -e

# The URL of the stores page.
URL='https://www.mitre10.com.au/stores'

echo "Scraping store data from $URL"

# Use awk to handle the multi-line script tag and isolate the JSON content.
# 1. Set the Record Separator (RS) to "</script>", treating each script block as a single record.
# 2. Find the unique record that contains both "store-locator-search" and "x-magento-init".
# 3. Use the sub() function to remove the opening <script> tag and everything before it.
# 4. Print the remaining text, which is the clean JSON blob.
JSON_BLOB=$(curl -sL "$URL" | awk 'BEGIN{RS="</script>"} /"store-locator-search"/ && /x-magento-init/ { sub(/.*<script type="text\/x-magento-init">/,""); print }')

# Check if awk successfully extracted the JSON blob
if [ -z "$JSON_BLOB" ]; then
    echo "Error: Could not find the JSON data blob in the HTML source."
    exit 1
fi

# Pipe the extracted JSON blob into jq.
# The first part navigates to the 'markers' array.
# The second part (| map(...)) iterates over each store object in the array
# and deletes the 'calendar' key from within the 'schedule' object.
echo "$JSON_BLOB" | jq '.[ "*"]["Magento_Ui/js/core/app"].components["store-locator-search"].markers | map(del(.schedule.calendar))' > stores.json

# Check if the file was created and is not empty or just "null".
if [ ! -s stores.json ] || [ "$(cat stores.json)" = "null" ]; then
    echo "Error: Failed to extract store data. The resulting stores.json is empty or null."
    exit 1
fi

echo "Successfully extracted and cleaned store data, saved to stores.json"
