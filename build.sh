#!/usr/bin/env bash
set -e

echo "==> Querying latest Playwright Python image tag..."

# Query MCR for latest jammy tag
PLAYWRIGHT_TAG=$(python3 -c "
import urllib.request, json, re, sys

try:
    url = 'https://mcr.microsoft.com/v2/playwright/python/tags/list'
    req = urllib.request.Request(url, headers={'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    tags = data.get('tags', [])
    pattern = re.compile(r'^v(\d+)\.(\d+)\.(\d+)-jammy$')
    versioned = []
    for t in tags:
        m = pattern.match(t)
        if m:
            versioned.append((int(m.group(1)), int(m.group(2)), int(m.group(3)), t))
    if versioned:
        versioned.sort(reverse=True)
        print(versioned[0][3])
    else:
        print('v1.60.0-jammy')
except Exception:
    print('v1.60.0-jammy')
" 2>/dev/null || echo "v1.60.0-jammy")

echo "==> Using Playwright tag: ${PLAYWRIGHT_TAG}"

echo "==> Installing Python dependencies..."
pip install -r requirements.txt

echo "==> Installing Playwright Chromium browser..."
playwright install chromium

echo "==> Installing Playwright system dependencies..."
playwright install-deps chromium

echo "==> Build complete! (Playwright: ${PLAYWRIGHT_TAG})"