from flask import Flask, jsonify, send_from_directory, abort, Response
from flask_cors import CORS
import json, os, pickle, threading, time
import secrets
from functools import wraps

app = Flask(__name__, static_folder='.')
CORS(app, origins=['http://localhost:10000', 'https://diary.onrender.com'])

_SERVER_SECRET = secrets.token_hex(32)
_rate_store: dict = {}
_rate_lock = threading.Lock()

def rate_limit(max_calls=60, window=60):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            from flask import request
            ip  = request.remote_addr
            now = time.time()
            with _rate_lock:
                ts = _rate_store.get(ip, [])
                ts = [t for t in ts if now - t < window]
                if len(ts) >= max_calls:
                    return jsonify({'error': 'rate limited'}), 429
                ts.append(now)
                _rate_store[ip] = ts
            return fn(*a, **kw)
        return wrapper
    return decorator

def _safe_path(path):
    safe      = os.path.abspath('.')
    requested = os.path.abspath(os.path.join('.', path))
    return requested.startswith(safe)

LB_PKL     = 'letterboxd_cache.pkl'
SZ_PKL     = 'serializd_cache.pkl'
LB_JSON    = 'letterboxd_cache.json'
SZ_JSON    = 'serializd_cache.json'
DIARY_META = 'diary_meta.json'

# Only these static files are served
ALLOWED_STATIC = {'index.html', 'favicon.ico'}

_refresh_lock  = threading.Lock()
_is_refreshing = False

def read_pkl(path):
    if os.path.exists(path):
        try:
            with open(path, 'rb') as f:
                data = pickle.load(f)
            if isinstance(data, dict):
                return list(data.values())
        except Exception:
            pass
    return []

def read_json(path):
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return []

def read_entries(pkl_path, json_path):
    entries = read_pkl(pkl_path)
    if entries:
        return entries
    return read_json(json_path)

def read_meta():
    if os.path.exists(DIARY_META):
        try:
            with open(DIARY_META, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def write_meta(d):
    with open(DIARY_META, 'w', encoding='utf-8') as f:
        json.dump(d, f)

def combined_diary():
    lb    = read_entries(LB_PKL, LB_JSON)
    sz    = read_entries(SZ_PKL, SZ_JSON)
    all_e = lb + sz
    seen, merged = set(), []
    for e in all_e:
        k = (
            f"{(e.get('title') or '').strip().lower()}"
            f"|{e.get('watched_date') or ''}"
            f"|{e.get('source') or ''}"
        )
        if k not in seen:
            seen.add(k)
            merged.append(e)
    merged.sort(key=lambda e: e.get('watched_date') or '', reverse=True)
    return merged

def _run_fetchers():
    import importlib.util
    def load_mod(path):
        spec = importlib.util.spec_from_file_location('_fetcher', path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    print('  Running fetch_letterboxd…')
    try:
        load_mod('fetch_letterboxd.py').fetch_letterboxd()
    except Exception as e:
        print(f'  fetch_letterboxd error: {e}')

    print('  Running fetch_serializd…')
    try:
        load_mod('fetch_serializd.py').fetch_serializd()
    except Exception as e:
        print(f'  fetch_serializd error: {e}')

def run_refresh():
    global _is_refreshing
    with _refresh_lock:
        if _is_refreshing:
            return
        _is_refreshing = True
    try:
        print('\nBackground refresh starting…')
        _run_fetchers()
        m = read_meta()
        m['last_refresh'] = time.time()
        write_meta(m)
        print('Background refresh complete\n')
    except Exception as e:
        print(f'Refresh error: {e}')
    finally:
        with _refresh_lock:
            _is_refreshing = False

def trigger_bg_refresh():
    t = threading.Thread(target=run_refresh, daemon=True)
    t.start()

# ── Security headers ──────────────────────────────────────────────────────────
@app.after_request
def set_security_headers(response):
    response.headers['X-Content-Type-Options']  = 'nosniff'
    response.headers['X-Frame-Options']         = 'DENY'
    response.headers['X-XSS-Protection']        = '1; mode=block'
    response.headers['Referrer-Policy']         = 'strict-origin-when-cross-origin'

    # ── CSP: allow everything the app actually needs ──────────────────────────
    # plausible.io was being blocked — removed it, we don't use it
    # TMDB images + Letterboxd images both need to be in img-src
    # Google Fonts needs style-src + font-src
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' https://image.tmdb.org https://a.ltrbxd.com "
        "https://img.youtube.com data: blob:; "
        "script-src 'self' 'unsafe-inline'; "
        "script-src-elem 'self' 'unsafe-inline'; "   # ← explicit so no fallback issues
        "connect-src 'self'; "
        "frame-src 'none'; "
        "object-src 'none'; "
        "base-uri 'self';"
    )

    if 'application/json' in response.content_type:
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        response.headers['Pragma']        = 'no-cache'
    return response

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/favicon.ico')
def favicon():
    """
    Return a minimal inline SVG favicon so browser stops 404-ing.
    No file needed on disk.
    """
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<rect width="32" height="32" rx="6" fill="#0d0d0d"/>'
        '<text x="16" y="23" font-size="20" text-anchor="middle" '
        'font-family="Georgia,serif" fill="#c8a96e" font-style="italic">T</text>'
        '</svg>'
    )
    return Response(svg, mimetype='image/svg+xml',
                    headers={'Cache-Control': 'public, max-age=86400'})

@app.route('/api/diary')
@rate_limit(max_calls=30, window=60)
def diary():
    entries = combined_diary()
    lb_c    = len(read_entries(LB_PKL, LB_JSON))
    sz_c    = len(read_entries(SZ_PKL, SZ_JSON))
    trigger_bg_refresh()
    return jsonify({
        'entries':    entries,
        'count':      len(entries),
        'refreshing': _is_refreshing,
        'lb_count':   lb_c,
        'sz_count':   sz_c,
    })

@app.route('/api/status')
@rate_limit(max_calls=60, window=60)
def status():
    meta = read_meta()
    lb_c = len(read_entries(LB_PKL, LB_JSON))
    sz_c = len(read_entries(SZ_PKL, SZ_JSON))
    return jsonify({
        'refreshing':   _is_refreshing,
        'last_refresh': meta.get('last_refresh', 0),
        'lb_count':     lb_c,
        'sz_count':     sz_c,
        'total':        lb_c + sz_c,
    })

@app.route('/<path:path>')
def static_files(path):
    if path not in ALLOWED_STATIC:
        abort(404)
    if not _safe_path(path):
        abort(403)
    return send_from_directory('.', path)

@app.errorhandler(404)
def not_found(e):    return jsonify({'error': 'not found'}), 404
@app.errorhandler(403)
def forbidden(e):    return jsonify({'error': 'forbidden'}), 403
@app.errorhandler(429)
def too_many(e):     return jsonify({'error': 'rate limited'}), 429
@app.errorhandler(500)
def server_error(e): return jsonify({'error': 'internal server error'}), 500

# ── Startup ───────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    has_lb = os.path.exists(LB_PKL) or os.path.exists(LB_JSON)
    has_sz = os.path.exists(SZ_PKL) or os.path.exists(SZ_JSON)

    if not has_lb and not has_sz:
        print('No cache found.')
        print('Run these first:')
        print('  py fetch_letterboxd.py')
        print('  py fetch_serializd.py')
        print('  py server.py')
        import sys; sys.exit(0)
    else:
        print('Cache found — starting server')
        trigger_bg_refresh()

    port = int(os.environ.get('PORT', 10000))
    print(f'Running on http://localhost:{port}')
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)