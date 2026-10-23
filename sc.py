"""Read-only client for stakecruncher.com's public tracker API.

The API only answers requests that look like they come from its own pages, so Origin and
Referer are set the same way as math-sdk/games/*/fetch_reference.mjs. Every request waits
MIN_GAP seconds after the previous one so the radar never hammers the site.
"""
import gzip
import json
import time
import urllib.error
import urllib.parse
import urllib.request

SITE = "https://stakecruncher.com"
HEADERS = {
    "Origin": SITE,
    "Referer": SITE + "/casino",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
}
MIN_GAP = 1.5  # seconds between requests
_last = [0.0]


def get(path, tries=3):
    """GET a tracker-api path and return parsed JSON, or None for 404 / non-JSON answers."""
    for attempt in range(tries):
        wait = _last[0] + MIN_GAP - time.time()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        req = urllib.request.Request(SITE + "/tracker-api" + path, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(10 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < tries - 1:
                time.sleep(10 * (attempt + 1))
                continue
            raise
        try:
            return json.loads(data)
        except ValueError:
            return None  # the site answers unknown paths with an HTML error page
    return None


def q(**params):
    return urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})


def engine_games():
    """Every Stake Engine game: casino slug, studio and name (about 6 requests)."""
    out, offset = [], 0
    while True:
        page = get(f"/engine/games?{q(sort='turnover_30d', limit=1000, offset=offset)}") or {}
        items = page.get("items") or []
        out += items
        offset += len(items)
        if not items or offset >= (page.get("total") or 0):
            return out


def new_releases(page):
    """One page of the newest Stake Engine releases (with release time and gameId)."""
    data = get(f"/casino/demos/new?{q(days=30, kind='engine', page=page)}") or {}
    return data.get("items") or []


def game_info(slug):
    """Release time, gameId, studio and engine slug for one casino slug."""
    return get(f"/casino/demos/game/{urllib.parse.quote(slug)}")


def game_modes(engine_slug):
    """Active modes with their cost multipliers, or None if the game isn't in the verifier."""
    stats = get(f"/engine/stats/games/{urllib.parse.quote(engine_slug)}")
    if not stats or not stats.get("modes"):
        return None
    version = stats.get("activeVersion")
    modes = [m for m in stats["modes"] if m.get("version") == version and m.get("active") is not False]
    return [{"name": m["name"], "cost": float(m["costMultiplier"])} for m in modes if m.get("costMultiplier")] or None


def feed(min_usd, game_id=None, limit=100):
    """The latest big bets across stake.com and stake.us (at most 100)."""
    data = get(f"/casino/feed?{q(limit=limit, minUsd=min_usd, gameId=game_id)}") or {}
    return data.get("bets") or []


def streamers():
    """Partnered streamer accounts (Stake affiliates), used only to leave their bets out."""
    data = get("/casino/streamers") or {}
    return data.get("streamers") or []
