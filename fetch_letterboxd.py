import requests
import re
import json
import os
import pickle
import base64
import codecs
import time
from xml.etree import ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Config ────────────────────────────────────────────────────────────────────
LB_USER   = 'a__tharun'
LB_RSS    = f'https://letterboxd.com/{LB_USER}/rss/'
PKL_FILE  = 'letterboxd_cache.pkl'
JSON_FILE = 'letterboxd_cache.json'

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

RSS_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept': 'application/rss+xml, application/xml, text/xml, */*',
}

PROXIES = [
    lambda u: f'https://api.allorigins.win/raw?url={requests.utils.quote(u)}',
    lambda u: f'https://corsproxy.io/?{requests.utils.quote(u)}',
    lambda u: f'https://api.codetabs.com/v1/proxy?quest={requests.utils.quote(u)}',
]

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

# ── TMDB fetch by ID ──────────────────────────────────────────────────────────
def tmdb_movie(tmdb_id, rss_poster=None):
    """
    Fetch movie details by TMDB ID.
    Uses RSS poster if available — no extra CDN call needed.
    Returns details dict or empty dict.
    """
    if not tmdb_id:
        return {}

    data = safe_get(
        f'{TMDB_BASE}/movie/{tmdb_id}',
        params={'api_key': _tmdb_key(), 'append_to_response': 'credits'},
    )
    if not data:
        return {}

    # Poster — prefer RSS URL
    if rss_poster:
        poster = rss_poster
    else:
        pp     = data.get('poster_path')
        poster = f'{TMDB_IMG}{pp}' if pp else None

    # Runtime
    rt = data.get('runtime')
    runtime = f'{rt//60}h {rt%60}m' if rt and rt > 0 else None

    # Genres
    genres = [g['name'] for g in data.get('genres', []) if g.get('name')]

    credits = data.get('credits') or {}

    # Director
    director = next(
        (c['name'] for c in credits.get('crew', [])
         if c.get('job') == 'Director'),
        None
    )

    # Music
    music = next(
        (c['name'] for c in credits.get('crew', [])
         if c.get('job') in ('Original Music Composer', 'Composer', 'Music')),
        None
    )

    # Cast top 3
    cast = [c['name'] for c in credits.get('cast', [])[:3] if c.get('name')]

    # Synopsis
    synopsis = (data.get('overview') or '').strip() or None

    dbg(f'  title={data.get("title")} runtime={runtime} '
        f'director={director} cast={cast} synopsis={"YES" if synopsis else "NO"}')

    return {
        'poster':     poster,
        'synopsis':   synopsis,
        'genres':     genres,
        'director':   director,
        'cast':       cast,
        'music':      music,
        'runtime':    runtime,
        'created_by': None,
    }

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

# ── RSS helpers ───────────────────────────────────────────────────────────────
def fetch_rss_text():
    try:
        r = _session.get(LB_RSS, headers=RSS_HEADERS, timeout=12)
        if r.ok and '<item>' in r.text:
            print('  RSS: direct OK')
            return r.text
    except Exception as e:
        print(f'  RSS direct failed: {e}')

    for i, fn in enumerate(PROXIES):
        try:
            r = _session.get(fn(LB_RSS), headers=RSS_HEADERS, timeout=12)
            if r.ok and '<item>' in r.text:
                print(f'  RSS: proxy {i+1} OK')
                return r.text
        except Exception as e:
            print(f'  RSS proxy {i+1} failed: {e}')
    return None

def ns_text(el, local_name):
    for child in el.iter():
        local = child.tag.split('}')[-1] if '}' in child.tag else child.tag
        if local == local_name:
            t = (child.text or '').strip()
            if t:
                return t
    return ''

def extract_rss_poster(desc_html):
    if not desc_html:
        return None
    m = re.search(r'<img\s+src=["\']([^"\']+)["\']', desc_html)
    return m.group(1) if m else None

def parse_rss(xml_text):
    xml_text = re.sub(r'<\?xml[^>]+\?>', '', xml_text).strip()
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        xml_text = re.sub(r'<!\[CDATA\[|\]\]>', '', xml_text)
        try:
            root = ET.fromstring(xml_text)
        except Exception as e:
            print(f'  XML error: {e}')
            return []

    items = root.findall('.//item')
    print(f'  Found {len(items)} RSS items')

    entries = []
    for i, item in enumerate(items):
        title    = ns_text(item, 'filmTitle')
        year     = ns_text(item, 'filmYear')
        rating_s = ns_text(item, 'memberRating')
        date     = ns_text(item, 'watchedDate')
        rewatch  = ns_text(item, 'rewatch').lower() == 'yes'
        tmdb_raw = ns_text(item, 'movieId')
        tmdb_id  = int(tmdb_raw) if tmdb_raw.isdigit() else None
        poster   = extract_rss_poster(item.findtext('description') or '')

        if not title:
            continue

        # Date fallback from pubDate
        if not date:
            pub = (item.findtext('pubDate') or '').strip()
            if pub:
                try:
                    from email.utils import parsedate_to_datetime
                    date = parsedate_to_datetime(pub).strftime('%Y-%m-%d')
                except Exception:
                    date = pub[:10]

        rating = float(rating_s) if rating_s else None

        dbg(f'  [{i+1}] "{title}" ({year}) tmdb={tmdb_id} '
            f'date={date} rating={rating} rewatch={rewatch} '
            f'poster={"YES" if poster else "NO"}')

        entries.append({
            'id':           item.findtext('link') or f'lb-{i}',
            'title':        title,
            'year':         year,
            'rating':       rating,
            'watched_date': date,
            'rewatch':      rewatch,
            'type':         'film',
            'source':       'letterboxd',
            'tmdb_id':      tmdb_id,
            'rss_poster':   poster,
            'details':      None,
        })
    return entries

# ── Parallel enrichment ───────────────────────────────────────────────────────
def enrich_new_entries(added, cache):
    """
    Parallel TMDB fetch for new entries only.
    Reuses details for same title watched multiple times.
    """
    # Title→details from cache (avoid re-fetching same film)
    title_details: dict = {}
    for ce in cache.values():
        t = (ce.get('title') or '').strip()
        d = ce.get('details')
        if t and d:
            title_details[t] = d

    # Unique new titles
    seen:     set  = set()
    to_fetch: list = []
    for e in added:
        t = (e.get('title') or '').strip()
        if t and t not in title_details and t not in seen:
            to_fetch.append(e)
            seen.add(t)

    if not to_fetch:
        print('  No new movies to enrich')
    else:
        print(f'  ⚡ Fetching {len(to_fetch)} new movie(s) in parallel…')

        def worker(e):
            t = (e.get('title') or '').strip()
            print(f'    → {t} (ID: {e.get("tmdb_id")})')
            return t, tmdb_movie(e.get('tmdb_id'), e.get('rss_poster'))

        with ThreadPoolExecutor(max_workers=5) as ex:
            futures = {ex.submit(worker, e): e for e in to_fetch}
            for f in as_completed(futures):
                try:
                    title, details = f.result()
                    title_details[title] = details
                except Exception as err:
                    e = futures[f]
                    t = (e.get('title') or '').strip()
                    dbg(f'  worker error "{t}": {err}')
                    title_details[t] = {}

    # Apply to all added entries
    for e in added:
        t = (e.get('title') or '').strip()
        e['details'] = title_details.get(t) or {}

    return added

# ── Smart update ──────────────────────────────────────────────────────────────
def smart_update(fresh_entries):
    """
    Compare fresh RSS entries with cache:
    - New     → enrich + add
    - Removed → delete
    - Existing → sync rating / rewatch / year
    """
    cache        = load_pkl()
    fresh_keys   = {entry_key(e): e for e in fresh_entries}
    added_keys   = set(fresh_keys) - set(cache)
    removed_keys = set(cache)      - set(fresh_keys)
    added        = [fresh_keys[k] for k in added_keys]

    dbg(f'Diff: +{len(added_keys)} new  -{len(removed_keys)} removed  '
        f'~{len(fresh_keys) - len(added_keys)} unchanged')

    if added:
        enrich_new_entries(added, cache)

    # Build new cache
    new_cache = {k: v for k, v in cache.items() if k not in removed_keys}
    for e in added:
        new_cache[entry_key(e)] = e

    # Sync mutable fields on existing entries
    added_set = {entry_key(e) for e in added}
    for k, fe in fresh_keys.items():
        if k in new_cache and k not in added_set:
            ce            = new_cache[k]
            ce['rating']  = fe.get('rating',  ce.get('rating'))
            ce['rewatch'] = fe.get('rewatch', ce.get('rewatch'))
            ce['year']    = fe.get('year',    ce.get('year'))
            new_cache[k]  = ce

    save_pkl(new_cache)
    final = list(new_cache.values())
    return final, {
        'added':   len(added),
        'removed': len(removed_keys),
        'total':   len(final),
    }

# ── Entry point ───────────────────────────────────────────────────────────────
def fetch_letterboxd():
    print('Fetching Letterboxd RSS…')
    xml = fetch_rss_text()

    if not xml:
        print('RSS failed — serving from cache')
        cached  = load_pkl()
        entries = list(cached.values()) if cached else load_json()
        save_json(entries)
        return entries

    fresh = parse_rss(xml)
    if not fresh:
        print('Nothing parsed — serving from cache')
        cached  = load_pkl()
        entries = list(cached.values()) if cached else load_json()
        save_json(entries)
        return entries

    entries, summary = smart_update(fresh)
    print(f'Done: {summary["total"]} total '
          f'(+{summary["added"]} new, -{summary["removed"]} removed)')
    save_json(entries)
    return entries


if __name__ == '__main__':
    fetch_letterboxd()