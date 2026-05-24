# ── Stage 1: find latest Playwright Python image tag ─────────────────────────
# We use a small Alpine helper to query the Microsoft Container Registry
# and pick the latest jammy (Ubuntu 22.04) tag automatically.
# If the query fails, it falls back to a known-good version.

FROM python:3.11-slim AS version-finder

RUN pip install --no-cache-dir requests 2>/dev/null || true

RUN python3 -c "
import urllib.request, json, sys

try:
    url = 'https://mcr.microsoft.com/v2/playwright/python/tags/list'
    req = urllib.request.Request(url, headers={'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    tags = data.get('tags', [])

    # Filter: jammy tags with version numbers like v1.XX.X-jammy
    import re
    pattern = re.compile(r'^v(\d+)\.(\d+)\.(\d+)-jammy$')
    versioned = []
    for t in tags:
        m = pattern.match(t)
        if m:
            versioned.append((int(m.group(1)), int(m.group(2)), int(m.group(3)), t))

    if versioned:
        versioned.sort(reverse=True)
        best = versioned[0][3]
        print(best)
    else:
        print('v1.60.0-jammy')   # fallback
except Exception as e:
    print('v1.60.0-jammy', file=sys.stderr)
    print('v1.60.0-jammy')
" > /tmp/pw_tag.txt

# ── Stage 2: actual image ─────────────────────────────────────────────────────
# Read the tag found above and use it.
# Docker doesn't support dynamic FROM, so we use a build-arg approach.
# The build script (build.sh) passes the tag in.

ARG PLAYWRIGHT_TAG=v1.60.0-jammy
FROM mcr.microsoft.com/playwright/python:${PLAYWRIGHT_TAG}

WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY . .

EXPOSE 10000

CMD ["python", "server.py"]