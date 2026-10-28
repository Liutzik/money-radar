"""Collect big bets on Stake Engine games, classify them and keep the ones that count."""
import re
import time
from datetime import datetime

import classify
import sc
import store

CATALOG_EVERY = 3600
AFFILIATES_EVERY = 24 * 3600
RELEASES_EVERY = 5 * 60
INFO_RETRY = 30 * 60
UNKNOWN_RETRY = 10 * 60      # a slug whose demo page didn't answer yet (brand-new games)
PENDING_FOR = 3 * 3600       # how long bets on such a slug wait to be resolved
MODES_RETRY = 7 * 24 * 3600

_pending = {}  # betKey -> bet, for bets on slugs not yet known to be engine or not


def iso_ts(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def due(db, key, every, now):
    return now - (store.meta_get(db, key) or 0) >= every


def refresh_catalog(db, now, force=False):
    """Every Stake Engine game on stake.com, so the feed's casino bets can be told apart."""
    missing_ids = db.execute("SELECT 1 FROM games WHERE catalog_slug IS NOT NULL AND engine_id IS NULL LIMIT 1").fetchone()
    if not force and not missing_ids and not due(db, "catalog_at", CATALOG_EVERY, now):
        return 0
    games = sc.engine_games()
    for g in games:
        if g.get("stake_com_slug"):
            store.upsert_game(db, g["stake_com_slug"], catalog_slug=g["slug"], engine_id=g["id"], name=g["name"], studio=g["team_name"])
    db.commit()
    store.meta_set(db, "catalog_at", now)
    return len(games)


def refresh_affiliates(db, now, force=False):
    """Partnered streamers play on Stake's money; remember their accounts only as hashes."""
    if not force and not due(db, "affiliates_at", AFFILIATES_EVERY, now):
        return None
    hashes = sorted({store.account_hash(s["site"], s["username"]) for s in sc.streamers() if s.get("username")})
    store.meta_set(db, "affiliates", hashes)
    store.meta_set(db, "affiliates_at", now)
    return hashes


def refresh_releases(db, now, force=False):
    """Newest engine releases (roughly the last day) with their exact Stake release time."""
    if not force and not due(db, "releases_at", RELEASES_EVERY, now):
        return 0
    seen = set()
    for page in (1, 2):  # the endpoint only has two pages; later ones repeat page 2
        for it in sc.new_releases(page):
            store.upsert_game(
                db, it["slug"], name=it.get("name"), studio=it.get("studioName"), engine_slug=it.get("engineSlug"),
                game_id=it.get("gameId"), released_at=iso_ts(it.get("stakeReleasedAt")), info_at=now, not_engine=0,
            )
            seen.add(it["slug"])
    db.commit()
    store.meta_set(db, "releases_at", now)
    return len(seen)


def scan_release_dates(db, cfg, now, budget):
    """Look up release times newest-first through the engine catalog until games get old.

    Newer catalog ids were listed later, so once 40 lookups in a row are older than twice
    the new-release window the rest of the catalog is older too and the scan stops.
    """
    floor = store.meta_get(db, "scan_floor")
    if floor is not None and floor <= 0:
        return 0
    cutoff = now - 2 * cfg["new_release_days"] * 86400
    rows = db.execute(
        "SELECT * FROM games WHERE info_at IS NULL AND engine_id IS NOT NULL AND engine_id < ? ORDER BY engine_id DESC LIMIT ?",
        (floor if floor is not None else 1 << 40, budget),
    ).fetchall()
    old_run = store.meta_get(db, "scan_old_run", 0)
    done = 0
    for r in rows:
        ensure_game(db, dict(r, modes=None, modes_at=now), now)  # release info only; modes wait for a big bet
        g = store.game(db, r["slug"])
        old_run = old_run + 1 if g["released_at"] and g["released_at"] < cutoff else 0
        done += 1
        if old_run >= 40:
            store.meta_set(db, "scan_floor", 0)
            break
    store.meta_set(db, "scan_old_run", old_run)
    return done


def ensure_game(db, g, now):
    """Fill in release time and mode prices the first time a game gets a big bet."""
    slug = g["slug"]
    if not g["released_at"] and now - (g["info_at"] or 0) >= INFO_RETRY:
        info = sc.game_info(slug) or {}
        if info.get("provider") and info["provider"] != "stake-engine":
            # the engine catalog maps a few casino slugs wrongly ("dice" -> an engine "Plinko Dice")
            store.upsert_game(db, slug, not_engine=1, info_at=now)
        else:
            store.upsert_game(
                db, slug, engine_slug=info.get("engineSlug") or g["engine_slug"], game_id=info.get("gameId") or g["game_id"],
                released_at=iso_ts(info.get("stakeReleasedAt")), studio=info.get("studioName") or g["studio"], info_at=now,
            )
        g = store.game(db, slug)
    if not g["modes"] and now - (g["modes_at"] or 0) >= MODES_RETRY:
        modes = None
        for candidate in (g["engine_slug"], g["catalog_slug"]):
            if candidate:
                modes = sc.game_modes(candidate)
                if modes:
                    break
        store.upsert_game(db, slug, modes=modes, modes_at=now)
        g = store.game(db, slug)
    db.commit()
    return g


def norm(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def excluded_studio(cfg, studio):
    """Stake's internal studios (config.json exclude_studios); matches 'Paperclip Gaming' and 'paperclip-gaming'."""
    return norm(studio) in {norm(s) for s in cfg.get("exclude_studios", [])}


def same_price_blank_spin(db, slug, amount, created, window=15 * 60):
    """A blank spin at the same price on the same game just before means this one is a spin too."""
    return db.execute(
        "SELECT 1 FROM bets WHERE slug=? AND kind='spin' AND payout_usd=0 AND ABS(amount_usd-?)<=?*0.005 AND created_at BETWEEN ? AND ?",
        (slug, amount, amount, created - window, created),
    ).fetchone() is not None


def unresolved(g):
    """Seen in the feed, but stakecruncher hasn't said yet whether it's an engine game."""
    return g is not None and g["not_engine"] is None and not g["catalog_slug"] and not g["released_at"] and not g["engine_slug"]


def discover(db, slug, now):
    """A slug missing from the engine catalog (EL CARTEL was): ask its demo page.

    Only a definite answer naming another provider marks it "not engine". No answer (a game
    released minutes ago has no demo page yet, or a hiccup) leaves it unresolved; it is asked
    again every UNKNOWN_RETRY while its bets wait in _pending.
    """
    if not slug:
        return None
    info = sc.game_info(slug)
    if info and info.get("provider") == "stake-engine":
        store.upsert_game(
            db, slug, name=info.get("name"), studio=info.get("studioName") or info.get("studio"),
            engine_slug=info.get("engineSlug"), game_id=info.get("gameId"),
            released_at=iso_ts(info.get("stakeReleasedAt")), info_at=now, not_engine=0,
        )
    elif info and info.get("provider"):
        store.upsert_game(db, slug, name=info.get("name"), not_engine=1, info_at=now)
    else:
        store.upsert_game(db, slug, info_at=now)
    db.commit()
    return store.game(db, slug)


def ingest(db, bet, cfg, affiliates, now):
    """Store one feed bet if it's on an engine game and clears the counting rules."""
    tiers = cfg["tiers"]
    amount = bet.get("amountUsd") or 0
    if amount < min(tiers["spin_high_usd"], tiers["buy_high_usd"]) or store.has_bet(db, bet["betKey"]):
        return None
    if cfg.get("exclude_affiliates", True) and bet.get("username") and store.account_hash(bet["site"], bet["username"]) in affiliates:
        return None
    if bet.get("gameKind") == "original":
        return None  # Stake Originals; the engine catalog wrongly maps "dice" to an engine game
    slug = bet.get("gameSlug") or ""
    g = store.game(db, slug)
    if not g or (unresolved(g) and now - (g["info_at"] or 0) >= UNKNOWN_RETRY):
        g = discover(db, slug, now)
    if unresolved(g):
        _pending.setdefault(bet["betKey"], bet)
        return None
    if not g or g["not_engine"]:
        return None  # not a Stake Engine game
    if excluded_studio(cfg, g["studio"]):
        return None
    g = ensure_game(db, g, now)
    if excluded_studio(cfg, g["studio"]):
        return None
    kind, mode, cost = classify.classify(amount, bet.get("payoutUsd"), bet.get("currency"), g["modes"])
    created = int(bet.get("createdAt") or now)
    if kind == "unclear" and same_price_blank_spin(db, g["slug"], amount, created):
        kind, mode, cost = "spin", None, 1.0
    level = classify.tier(amount, kind, tiers)
    if not level:
        return None
    row = {
        "bet_key": bet["betKey"], "site": bet.get("site"), "slug": g["slug"], "amount_usd": amount,
        "payout_usd": bet.get("payoutUsd") or 0, "currency": bet.get("currency"), "created_at": created,
        "hidden": 0 if bet.get("username") else 1, "kind": kind, "mode": mode, "cost": cost, "tier": level,
        "age_days": (created - g["released_at"]) / 86400 if g["released_at"] else None, "image": bet.get("imageUrl"),
    }
    if not store.add_bet(db, row):
        return None
    db.commit()
    _pending.pop(bet["betKey"], None)
    return dict(row, name=g["name"], studio=g["studio"])


def backfill(db, cfg, now, limit=150):
    """Pull the last 100 big bets on each recent release once, so patterns start with history."""
    done = set(store.meta_get(db, "backfilled", []))
    window = now - 2 * cfg["new_release_days"] * 86400
    rows = [r for r in db.execute(
        "SELECT slug, game_id, studio FROM games WHERE game_id IS NOT NULL AND released_at >= ? ORDER BY released_at DESC", (window,)
    ).fetchall() if not excluded_studio(cfg, r["studio"])]
    affiliates = set(store.meta_get(db, "affiliates", []))
    added = []  # returned so the caller can alert on any that are still fresh
    for r in rows:
        if r["game_id"] in done:
            continue
        if limit <= 0:
            break
        limit -= 1
        for bet in sorted(sc.feed(cfg["tiers"]["spin_high_usd"], game_id=r["game_id"]), key=lambda b: b.get("createdAt") or 0):
            row = ingest(db, bet, cfg, affiliates, now)
            if row:
                added.append(row)
        done.add(r["game_id"])
        store.meta_set(db, "backfilled", sorted(done))
    return added


def poll(db, cfg, now):
    """New qualifying bets from the live big-bet feeds, plus held bets whose game got resolved.

    The $1k feed holds only the latest 100 bets of $1,000+ on all of Stake (about two minutes,
    less at peak), so the $10k and $50k feeds are read too: they reach back much further, and an
    extreme bet can't be pushed out by a burst of small ones. An overflow of the $1k feed (its
    oldest bet newer than the previous poll) is counted in meta "overflows".
    """
    affiliates = set(store.meta_get(db, "affiliates", []))
    tiers = cfg["tiers"]
    bets = {}
    last = store.meta_get(db, "last_poll_at")
    for min_usd in sorted({tiers["spin_high_usd"], tiers["buy_high_usd"], tiers["extreme_usd"]}):
        feed = sc.feed(min_usd)
        if min_usd == tiers["spin_high_usd"] and feed and last and min(b.get("createdAt") or now for b in feed) > last:
            store.meta_set(db, "overflows", (store.meta_get(db, "overflows", 0) or 0) + 1)
        for b in feed:
            bets[b["betKey"]] = b
    store.meta_set(db, "last_poll_at", now)
    out = []
    for bet in sorted(bets.values(), key=lambda b: b.get("createdAt") or 0):
        row = ingest(db, bet, cfg, affiliates, now)
        if row:
            out.append(row)
    for key, bet in list(_pending.items()):
        if now - (bet.get("createdAt") or now) > PENDING_FOR:
            del _pending[key]
        elif key not in bets:  # ones still in the feed were tried above
            row = ingest(db, bet, cfg, affiliates, now)
            if row:
                out.append(dict(row, late=True))
    return out


def refresh_all(db, now, force=False):
    refresh_catalog(db, now, force)
    refresh_affiliates(db, now, force)
    refresh_releases(db, now, force)
    return time.time()
