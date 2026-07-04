from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from flask_cors import CORS
from werkzeug.exceptions import HTTPException
import os
import re
import json
import logging
import signal
import sys
import math
import socket
import ipaddress
import requests
from urllib.parse import urljoin, urlparse, quote, unquote

# Configure logging based on environment variable
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s %(levelname)s %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

# Graceful shutdown handler
shutdown_requested = False
def shutdown_handler(signum, frame):
    global shutdown_requested
    shutdown_requested = True
    logger.info(f'Received signal {signum}, shutting down gracefully...')
    sys.exit(0)

signal.signal(signal.SIGTERM, shutdown_handler)
signal.signal(signal.SIGINT, shutdown_handler)

# Base directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
TEMPLATE_DIR = os.path.join(BASE_DIR, 'ui', 'templates')
STATIC_DIR = os.path.join(BASE_DIR, 'ui', 'static')

app = Flask(__name__, template_folder=TEMPLATE_DIR, static_folder=STATIC_DIR)

# Log each incoming request
@app.before_request
def log_request():
    logger.info(f"{request.remote_addr} {request.method} {request.path}")
CORS(app)

M3U8_PATH = os.path.join(PROJECT_ROOT, 'network', 'channels.m3u8')
FAV_PATH = os.path.join(PROJECT_ROOT, 'favorites.json')

# Ensure favorites.json exists with default structure
if not os.path.exists(FAV_PATH):
    with open(FAV_PATH, 'w', encoding='utf-8') as f:
        json.dump([], f, indent=2)

def parse_m3u8(filepath):
    """Parse M3U8 playlist and return list of channel dicts.
    Each dict contains at least: name, url, tvg-id, tvg-logo, group-title.
    """
    if not os.path.exists(filepath):
        return []
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
    return parse_m3u8_file(content)

def parse_tvg_filename(filename):
    """Extract country info from tvg-id using domain pattern."""
    if not filename:
        return None
    # Extract from tvg-id if present
    tvg_domain = re.search(r'@([^:]+)', filename)
    if tvg_domain:
        domain = tvg_domain.group(1).split('.')
        if len(domain) >= 2:
            return domain[1].upper()  # Get country code from TLD
    # Fallback to group-title extraction
    return None

# Merge and dedupe new channels with existing ones
# Enhanced version: handles missing attributes gracefully
def merge_channels(new_data, existing_channels):
    """Merge new channels into existing ones, avoiding duplicates by URL.
    Uses safe attribute access with defaults for missing fields.
    """
    merged = []
    seen_urls = {c['url'] for c in existing_channels}
    for channel in new_data:
        channel['url'] = channel['url'].strip()
        if channel['url'] in seen_urls:
            continue
        # Ensure safe attribute access with defaults
        safe_channel = {
            'name': channel.get('name', 'Unknown'),
            'url': channel['url'],
            'tvg-id': channel.get('tvg-id', ''),
            'tvg-logo': channel.get('tvg-logo', ''),
            'group-title': channel.get('group-title', 'Other')
        }
        merged.append(safe_channel)
    return existing_channels + merged

# File parse methods
def parse_m3u8_file(file_content):
    """Parse M3U8 content from string."""
    channels = []
    lines = file_content.strip().split('\n')
    current = None
    for line in lines:
        line = line.strip()
        if line.startswith('#EXTINF:'):
            current = {}
            # duration may be followed directly by "," (no attrs) or by
            # whitespace-separated key="value" attrs before the trailing ",name"
            match = re.match(r'#EXTINF:-?\d+\s*(.*)', line)
            if match:
                attrs_str = match.group(1)
                # key="value" pairs (keys may contain hyphens, e.g. tvg-id)
                for key, val in re.findall(r'([\w-]+)="([^"]*)"', attrs_str):
                    current[key.lower()] = val
                # name after last comma
                name_match = re.search(r',\s*(.+)$', attrs_str)
                current['name'] = name_match.group(1).strip() if name_match else 'Unknown'
        elif line and not line.startswith('#') and current is not None:
            current['url'] = line
            channels.append(current)
            current = None
    return channels

def load_favorites():
    if not os.path.exists(FAV_PATH):
        return []
    try:
        with open(FAV_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []

def save_favorites(favs):
    with open(FAV_PATH, 'w', encoding='utf-8') as f:
        json.dump(favs, f, indent=2)

def write_m3u8(filepath, channels):
    """Write a list of channel dicts back out as an M3U8 playlist file."""
    lines = ['#EXTM3U']
    for ch in channels:
        attrs = ''
        for key in ('tvg-id', 'tvg-logo', 'group-title'):
            val = ch.get(key, '')
            if val:
                attrs += f' {key}="{val}"'
        name = ch.get('name', 'Unknown')
        lines.append(f'#EXTINF:-1{attrs},{name}')
        lines.append(ch['url'])
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/channels')
def api_channels():
    # Pagination via query parameters
    try:
        page = int(request.args.get('page', 1))
    except ValueError:
        page = 1
    try:
        size = int(request.args.get('size', 50))
    except ValueError:
        size = 50
    if page < 1:
        page = 1
    if size < 1:
        size = 50
    channels = parse_m3u8(M3U8_PATH)
    total = len(channels)
    max_page = math.ceil(total / size)
    offset = (page - 1) * size
    result = channels[offset:offset + size]

    data = {
        'total': total,
        'page': page,
        'size': size,
        'total_pages': max_page,
        'channels': result
    }
    return jsonify(data)

# ... (rest of the code remains unchanged)

@app.route('/api/favorites', methods=['GET', 'POST', 'DELETE'])
def api_favorites():
    if request.method == 'GET':
        return jsonify(load_favorites())
    data = request.get_json(silent=True) or {}
    if request.method == 'POST':
        favs = load_favorites()
        if not any(f.get('url') == data.get('url') for f in favs):
            favs.append(data)
            save_favorites(favs)
        return jsonify(favs)
    if request.method == 'DELETE':
        favs = load_favorites()
        favs = [f for f in favs if f.get('url') != data.get('url')]
        save_favorites(favs)
        return jsonify(favs)

@app.route('/api/import', methods=['POST'])
def import_channels():
    # Pagination via query parameters (applies to GET-like behavior as well)
    try:
        page = int(request.args.get('page', 1))
    except ValueError:
        page = 1
    try:
        size = int(request.args.get('size', 50))
    except ValueError:
        size = 50
    if page < 1:
        page = 1
    if size < 1:
        size = 50

    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'Empty file'}), 400
    try:
        content = file.read().decode('utf-8')
    except UnicodeDecodeError:
        return jsonify({'error': 'File encoding not UTF-8'}), 400
    new_channels = parse_m3u8_file(content)
    existing = parse_m3u8(M3U8_PATH)
    merged = merge_channels(new_channels, existing)
    write_m3u8(M3U8_PATH, merged)

    # Apply pagination to the merged list
    offset = (page - 1) * size
    result = merged[offset:offset + size]

    data = {
        'message': 'Channels imported successfully',
        'total_channels_imported': len(merged),
        'page': page,
        'size': size,
        'channels': result
    }
    return jsonify(data)

@app.route('/api/import_url', methods=['POST'])
def import_channels_from_url():
    """Fetch an M3U8 playlist from a remote URL and merge it into the local
    channel list. Lets users pull in curated playlists (e.g. country- or
    language-specific ones) without manually downloading and re-uploading."""
    data = request.get_json(silent=True) or {}
    url = data.get('url')
    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    if not _is_safe_upstream_url(url):
        return jsonify({'error': 'URL not allowed'}), 400
    try:
        resp = requests.get(url, timeout=15, headers={'User-Agent': PROXY_USER_AGENT})
        resp.raise_for_status()
        content = resp.text
    except requests.RequestException as exc:
        logger.warning(f'Import-from-URL failed for {url}: {exc}')
        return jsonify({'error': 'Failed to fetch playlist'}), 502

    new_channels = parse_m3u8_file(content)
    existing = parse_m3u8(M3U8_PATH)
    merged = merge_channels(new_channels, existing)
    write_m3u8(M3U8_PATH, merged)

    return jsonify({
        'message': 'Channels imported successfully',
        'source_channels_found': len(new_channels),
        'total_channels': len(merged)
    })

@app.route('/api/play')
def play_channel():
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    return jsonify({'url': url})

# --- Streaming proxy -------------------------------------------------------
# Many public IPTV stream sources don't send CORS headers permitting browser
# XHR from an arbitrary origin (or restrict Access-Control-Allow-Origin to
# their own site), so hls.js running client-side can't fetch them directly.
# This proxy fetches streams server-side (not subject to CORS) and re-serves
# them from our own origin, rewriting playlist URIs to route back through
# the proxy so nested playlists/segments/keys are proxied too.

PROXY_USER_AGENT = 'Mozilla/5.0 (compatible; InternetTVProxy/1.0)'
_URI_ATTR_RE = re.compile(r'URI="([^"]+)"')

def _is_safe_upstream_url(url):
    """Basic SSRF guard: only allow http(s) URLs that don't resolve to
    private/loopback/link-local/reserved addresses."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            return False
        for info in socket.getaddrinfo(parsed.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except (ValueError, OSError):
        return False

def _proxied(url):
    return '/api/stream?url=' + quote(url, safe='')

def _rewrite_playlist(text, base_url):
    """Rewrite every URI reference in an M3U8 playlist (plain lines and
    quoted URI="..." attributes) to an absolute URL routed through our proxy."""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
        elif stripped.startswith('#'):
            out.append(_URI_ATTR_RE.sub(
                lambda m: f'URI="{_proxied(urljoin(base_url, m.group(1)))}"', line
            ))
        else:
            out.append(_proxied(urljoin(base_url, stripped)))
    return '\n'.join(out) + '\n'

@app.route('/api/stream')
def stream_proxy():
    target = request.args.get('url')
    if not target:
        return jsonify({'error': 'No URL provided'}), 400
    target = unquote(target)
    if not _is_safe_upstream_url(target):
        return jsonify({'error': 'URL not allowed'}), 400

    try:
        upstream = requests.get(
            target, stream=True, timeout=10,
            headers={'User-Agent': PROXY_USER_AGENT}
        )
    except requests.RequestException as exc:
        logger.warning(f'Proxy fetch failed for {target}: {exc}')
        return jsonify({'error': 'Failed to fetch stream'}), 502

    content_type = upstream.headers.get('Content-Type', '')
    is_playlist = target.split('?')[0].lower().endswith('.m3u8') or 'mpegurl' in content_type.lower()

    if is_playlist:
        try:
            text = upstream.text
        except Exception:
            upstream.close()
            return jsonify({'error': 'Failed to read playlist'}), 502
        upstream.close()
        return Response(_rewrite_playlist(text, upstream.url), content_type='application/vnd.apple.mpegurl')

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    headers = {}
    if content_type:
        headers['Content-Type'] = content_type
    if 'Content-Length' in upstream.headers:
        headers['Content-Length'] = upstream.headers['Content-Length']
    return Response(stream_with_context(generate()), headers=headers, status=upstream.status_code)

@app.errorhandler(Exception)
def handle_exception(e):
    # Let normal HTTP errors (404, 405, etc.) pass through unchanged instead
    # of masking them as a 500.
    if isinstance(e, HTTPException):
        return e
    logger.exception("Unhandled exception")
    return jsonify({"error": "Internal server error"}), 500

@app.after_request
def log_response(response):
    logger.info(f'Response: {response.status} for {request.path}')
    return response

if __name__ == '__main__':
    # When running directly (e.g., during development), we honour graceful shutdown.
    try:
        logger.info('Starting Internet TV Flask development server')
        app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=False)
    except KeyboardInterrupt:
        logger.info('KeyboardInterrupt received, shutting down')
    finally:
        logger.info('Internet TV application stopped')