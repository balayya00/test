import asyncio
import base64
import codecs
import json
import os
import pickle
import re
import time
import requests
import nest_asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from playwright.async_api import async_playwright

nest_asyncio.apply()

# ── Config ────────────────────────────────────────────────────────────────────
SZ_USER      = 'tharun123'
PKL_FILE     = 'serializd_cache.pkl'
JSON_FILE    = 'serializd_cache.json'
CURRENT_YEAR = str(datetime.now().year)

TMDB_BASE = 'https://api.themoviedb.org/3'
TMDB_IMG  = 'https://image.tmdb.org/t/p/w342'

DEBUG = True  # ← set False to silence logs

def dbg(*args):
    if DEBUG:
        print('[DBG]', *args)

# ── TMDB key ──────────────────────────────────────────────────────────────────
_RAW_T = 'LzIzAwWxZ2EwMQV0ZwL2AJAyA2Z1BQZ0LzRmATR5AQp='
def _tmdb_key():
    return base64.b64decode(codecs.decode(_RAW_T, 'rot_13')).decode()

# ── Shared session ────────────────────────────────────────────────────────────
_session = requests.Session()
_session.headers.update({
    'User-Agent': 'DiaryApp/2.0',
    'Accept':     'application/json',
})

# ── Safe GET ──────────────────────────────────────────────────────────────────
def safe_get(url, params=None, retries=3):
    for attempt in range(retries):
        try:
            r = _session.get(url, params=params, timeout=10)
            dbg(f'  {r.status_code} {url.split("?")[0]}')
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                wait = 2 ** attempt
                for i in range(wait, 0, -1):
                    print(f'\r  ⚠ Rate-limited — retry in {i}s  ',
                          end='', flush=True)
                    time.sleep(1)
                print()
        except Exception as ex:
            dbg(f'  attempt {attempt+1} failed: {ex}')
            time.sleep(2 ** attempt)
    return None

# ── Year helper ───────────────────────────────────────────────────────────────
def fmt_year(start, end=None, status=None):
    def extract(s):
        if not s:
            return None
        m = re.search(r'((?:19|20)\d{2})', str(s))
        return m.group(1) if m else None

    s = extract(start)
    if not s:
        return None
    e = extract(end) if end else None
    running = status and str(status).lower() in (
        'running', 'returning series', 'in production', 'planned'
    )
    if running:
        return f'{s}–{CURRENT_YEAR}'
    if not e or e == s:
        return s
    return f'{s}–{e}'

# ── TMDB TV fetch by ID ───────────────────────────────────────────────────────
def tmdb_tv(show_id, show_name=None):
    """
    Fetch TV show details by TMDB ID.
    Uses /tv/{id} for details + /tv/{id}/credits for cast/crew.
    Falls back to name search if no ID.
    Returns (year_str, details_dict).
    """
    key = _tmdb_key()

    # ── Search by name if no ID ──
    if not show_id:
        if not show_name:
            return None, {}
        dbg(f'  [TMDB-TV] No ID — searching "{show_name}"')
        search = safe_get(
            f'{TMDB_BASE}/search/tv',
            params={'api_key': key, 'query': show_name, 'page': 1},
        )
        if not isinstance(search, dict) or not search.get('results'):
            return None, {}
        # Prefer exact match
        name_lower = show_name.strip().lower()
        best = None
        for r in search['results'][:5]:
            if (r.get('name') or '').strip().lower() == name_lower:
                best = r
                break
        if not best:
            best = search['results'][0]
        show_id = best.get('id')
        if not show_id:
            return None, {}

    dbg(f'  [TMDB-TV] Details id={show_id}')

    # ── Details ──
    details = safe_get(
        f'{TMDB_BASE}/tv/{show_id}',
        params={'api_key': key},
    )
    if not isinstance(details, dict):
        return None, {}

    # ── Credits (separate call — more complete) ──
    credits = safe_get(
        f'{TMDB_BASE}/tv/{show_id}/credits',
        params={'api_key': key},
    ) or {}

    # Year
    year = fmt_year(
        details.get('first_air_date'),
        details.get('last_air_date'),
        details.get('status', ''),
    )
    dbg(f'  year={year}')

    # Poster
    pp     = details.get('poster_path')
    poster = f'{TMDB_IMG}{pp}' if pp else None

    # Synopsis
    synopsis = (details.get('overview') or '').strip() or None

    # Genres
    genres = [g['name'] for g in details.get('genres', []) if g.get('name')]

    # Created by
    created_by = [
        p['name'] for p in details.get('created_by', []) if p.get('name')
    ]

    # Cast top 3
    cast = [
        c['name'] for c in credits.get('cast', [])[:3] if c.get('name')
    ]

    # Directors (unique, max 3)
    directors = list({
        c['name'] for c in credits.get('crew', [])
        if c.get('job') == 'Director' and c.get('name')
    })[:3]

    # Music
    music = next(
        (c['name'] for c in credits.get('crew', [])
         if c.get('job') in ('Original Music Composer', 'Composer', 'Music')
         and c.get('name')),
        None
    )

    dbg(f'  created_by={created_by} directors={directors} '
        f'cast={cast} music={music} '
        f'synopsis={"YES" if synopsis else "NO"}')

    enriched = {
        'poster':     poster,
        'synopsis':   synopsis,
        'genres':     genres,
        'created_by': created_by,
        'director':   directors,
        'cast':       cast,
        'music':      music,
        'runtime':    None,
    }
    return year, enriched

# ── Cache helpers ─────────────────────────────────────────────────────────────
def entry_key(e):
    return (
        (e.get('title') or '').strip().lower(),
        e.get('watched_date') or '',
        e.get('source') or '',
    )

def load_pkl():
    if os.path.exists(PKL_FILE):
        try:
            with open(PKL_FILE, 'rb') as f:
                data = pickle.load(f)
            if isinstance(data, dict):
                dbg(f'Loaded pkl: {len(data)} entries')
                return data
        except Exception as ex:
            print(f'  pkl load error: {ex}')
    return {}

def save_pkl(d):
    with open(PKL_FILE, 'wb') as f:
        pickle.dump(d, f)
    dbg(f'Saved pkl: {len(d)} entries')

def save_json(entries):
    with open(JSON_FILE, 'w', encoding='utf-8') as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    dbg(f'Saved json: {len(entries)} entries')

def load_json():
    if os.path.exists(JSON_FILE):
        try:
            with open(JSON_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return []

# ── Playwright scraper ────────────────────────────────────────────────────────
async def scrape_serializd():
    url      = f'https://www.serializd.com/user/{SZ_USER}/diary'
    captured = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox', '--disable-dev-shm-usage',
                '--disable-gpu', '--disable-extensions',
                '--disable-setuid-sandbox', '--single-process',
                '--no-zygote',
            ],
        )
        ctx  = await browser.new_context(user_agent=(
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        ))
        page = await ctx.new_page()

        # Block heavy assets
        await page.route(
            '**/*.{png,jpg,jpeg,gif,svg,webp,woff,woff2,ttf,eot,mp4,mp3}',
            lambda r: r.abort(),
        )
        for pat in ('**/analytics**', '**/hotjar**',
                    '**/googletagmanager**', '**/ads**'):
            await page.route(pat, lambda r: r.abort())

        async def on_response(response):
            u = response.url.lower()
            if response.status == 200 and (
                'diary' in u or 'review' in u or 'log' in u
            ):
                try:
                    j = await response.json()
                    if j:
                        captured.append(j)
                        dbg(f'  captured: {response.url}')
                        if DEBUG:
                            dbg(f'    preview: {str(j)[:200]}')
                except Exception as ex:
                    dbg(f'  json error {response.url}: {ex}')

        page.on('response', on_response)

        try:
            dbg(f'  navigating → {url}')
            await page.goto(url, timeout=60000, wait_until='domcontentloaded')
            await page.wait_for_timeout(5000)
            for i in range(10):
                await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                await page.wait_for_timeout(1500)
                dbg(f'  scroll {i+1}/10')
        except Exception as e:
            print(f'  page error: {e}')
        finally:
            await browser.close()

    dbg(f'  total captured: {len(captured)}')
    return captured

# ── Parse Playwright responses ────────────────────────────────────────────────
def parse_serializd(raw_data):
    clean = []
    seen  = set()

    for bi, batch in enumerate(raw_data):
        dbg(f'  batch {bi+1} keys={list(batch.keys())[:6]}')
        reviews = (
            batch.get('reviews')
            or (batch.get('diary') or {}).get('reviews', [])
            or batch.get('entries')
            or batch.get('items')
            or batch.get('data')
            or []
        )
        if isinstance(reviews, dict):
            reviews = reviews.get('items') or reviews.get('results') or []
        if not isinstance(reviews, list):
            continue

        dbg(f'    {len(reviews)} reviews')
        for item in reviews:
            item_id = item.get('id')
            if item_id is None or item_id in seen:
                continue
            seen.add(item_id)

            rating_raw = item.get('rating')
            rating     = round(float(rating_raw) / 2, 1) if rating_raw else None

            watched_date = None
            for k in ('backdate', 'watchedDate', 'watched_date',
                      'createdAt', 'created_at', 'loggedDate'):
                v = item.get(k)
                if v:
                    watched_date = str(v)[:10]
                    break

            title = ''
            for k in ('showName', 'show_name', 'name', 'title', 'seriesName'):
                v = item.get(k)
                if v:
                    title = str(v).strip()
                    break
            if not title:
                continue

            # TMDB show ID if Serializd exposes it
            show_id = item.get('showId') or item.get('show_id')
            tmdb_id = item.get('tmdbId') or item.get('tmdb_id') or None

            dbg(f'    "{title}" id={item_id} date={watched_date} '
                f'rating={rating} tmdb_id={tmdb_id}')

            clean.append({
                'id':           str(item_id),
                'title':        title,
                'year':         '',
                'rating':       rating,
                'watched_date': watched_date,
                'type':         'tv',
                'source':       'serializd',
                'show_id':      show_id,
                'tmdb_id':      tmdb_id,
                'details':      None,
            })

    dbg(f'  total parsed: {len(clean)}')
    return clean

# ── Smart update ──────────────────────────────────────────────────────────────
def smart_update(fresh_entries):
    """
    Diff fresh scrape vs cache:
    - New   → parallel TMDB fetch + add
    - Removed → delete
    - Existing → sync rating only
    """
    cache        = load_pkl()
    fresh_keys   = {entry_key(e): e for e in fresh_entries}
    added_keys   = set(fresh_keys) - set(cache)
    removed_keys = set(cache)      - set(fresh_keys)
    added        = [fresh_keys[k] for k in added_keys]

    dbg(f'Diff: +{len(added_keys)} -{len(removed_keys)} '
        f'~{len(fresh_keys)-len(added_keys)} unchanged')

    # Title → year/details from cache
    title_year:    dict = {}
    title_details: dict = {}
    for ce in cache.values():
        t = (ce.get('title') or '').strip()
        if t:
            if ce.get('year'):
                title_year[t] = ce['year']
            if ce.get('details'):
                title_details[t] = ce['details']

    # New unique titles
    seen:     set  = set()
    to_fetch: list = []
    for e in added:
        t = (e.get('title') or '').strip()
        if t and t not in title_year and t not in seen:
            to_fetch.append(e)
            seen.add(t)

    if not to_fetch:
        print('  No new shows')
    else:
        print(f'  ⚡ Fetching {len(to_fetch)} new show(s) in parallel…')

        def worker(e):
            t       = (e.get('title') or '').strip()
            tmdb_id = e.get('tmdb_id')
            show_id = e.get('show_id')
            use_id  = tmdb_id or show_id   # use whichever is available
            print(f'    → {t} (ID: {use_id})')
            year, details = tmdb_tv(use_id, show_name=t)
            return t, year, details

        with ThreadPoolExecutor(max_workers=5) as ex:
            futures = {ex.submit(worker, e): e for e in to_fetch}
            for f in as_completed(futures):
                try:
                    t, year, details = f.result()
                    title_year[t]    = year or ''
                    title_details[t] = details
                except Exception as err:
                    e = futures[f]
                    t = (e.get('title') or '').strip()
                    dbg(f'  worker error "{t}": {err}')
                    title_year[t]    = ''
                    title_details[t] = {}

    for e in added:
        t         = (e.get('title') or '').strip()
        e['year']    = title_year.get(t, '')
        e['details'] = title_details.get(t, {})

    new_cache = {k: v for k, v in cache.items() if k not in removed_keys}
    for e in added:
        new_cache[entry_key(e)] = e

    added_set = {entry_key(e) for e in added}
    for k, fe in fresh_keys.items():
        if k in new_cache and k not in added_set:
            ce           = new_cache[k]
            ce['rating'] = fe.get('rating', ce.get('rating'))
            new_cache[k] = ce

    save_pkl(new_cache)
    final = list(new_cache.values())
    return final, {
        'added':   len(added),
        'removed': len(removed_keys),
        'total':   len(final),
    }

# ── Entry point ───────────────────────────────────────────────────────────────
def fetch_serializd():
    print('Scraping Serializd…')
    try:
        raw = asyncio.run(scrape_serializd())
        print(f'  Got {len(raw)} response batches')

        fresh = parse_serializd(raw)
        print(f'  Parsed {len(fresh)} entries')

        if not fresh:
            print('  Nothing scraped — serving from cache')
            cache   = load_pkl()
            entries = list(cache.values()) if cache else load_json()
            save_json(entries)
            return entries

        entries, summary = smart_update(fresh)
        print(f'Done: {summary["total"]} total '
              f'(+{summary["added"]} new, -{summary["removed"]} removed)')
        save_json(entries)
        return entries

    except Exception as e:
        print(f'Serializd failed: {e}')
        cache   = load_pkl()
        entries = list(cache.values()) if cache else load_json()
        save_json(entries)
        return entries


if __name__ == '__main__':
    fetch_serializd()