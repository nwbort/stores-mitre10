#!/bin/bash
# This script scrapes store details from the Mitre 10 website.

# Exit immediately if a command exits with a non-zero status.
set -e

# The URL of the stores page.
URL='https://www.mitre10.com.au/stores'

echo "Scraping store data from $URL"

# JavaScript to extract the markers array from the page's Magento init script tags.
# Uses double-quotes only so this is safe inside a bash single-quoted string.
JS_EXTRACT='
(function() {
  var allScripts = document.querySelectorAll("script");
  for (var i = 0; i < allScripts.length; i++) {
    var s = allScripts[i];
    if (s.getAttribute("type") !== "text/x-magento-init") continue;
    try {
      var data = JSON.parse(s.textContent);
      var components = data
        && data["*"]
        && data["*"]["Magento_Ui/js/core/app"]
        && data["*"]["Magento_Ui/js/core/app"].components;
      if (components
          && components["store-locator-search"]
          && components["store-locator-search"].markers) {
        return components["store-locator-search"].markers;
      }
    } catch(e) {}
  }
  return null;
})()
'

# Use shot-scraper to run a real browser (bypasses bot-protection challenge pages)
# and evaluate the extraction JS after the page finishes loading.
JSON_BLOB=$(shot-scraper javascript "$URL" "$JS_EXTRACT")

# Check if extraction succeeded
if [ -z "$JSON_BLOB" ] || [ "$JSON_BLOB" = "null" ]; then
    echo "Error: Could not find the JSON data blob in the page."
    exit 1
fi

# The markers array is returned directly; strip the schedule.calendar noise.
echo "$JSON_BLOB" | jq 'map(del(.schedule.calendar))' > stores.json

# Check if the file was created and is not empty or just "null".
if [ ! -s stores.json ] || [ "$(cat stores.json)" = "null" ]; then
    echo "Error: Failed to extract store data. The resulting stores.json is empty or null."
    exit 1
fi

echo "Successfully extracted and cleaned store data, saved to stores.json"
