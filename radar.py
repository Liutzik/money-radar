"""Money Radar: big money on new Stake Engine releases.

  python radar.py watch      run forever: poll the big-bet feed, send Discord alerts, rebuild the dashboard
  python radar.py watch --for 340   the same, but stop cleanly after 340 minutes (GitHub Actions)
  python radar.py collect    one pass: refresh games, backfill recent releases, poll once, rebuild the dashboard
  python radar.py report     rebuild dashboard.html from what's stored
  python radar.py selftest   offline check of counting, alerts and the dashboard (no network)
"""
import json
import os
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import alerts  # noqa: E402
import collect  # noqa: E402
import predict  # noqa: E402
import store  # noqa: E402

DB = HERE / "data" / "radar.db"
PAGE = HERE / "dashboard.html"
REPORT = HERE / "data" / "report.json"
REPORT_EVERY = 15 * 60


def config():
    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    for channel in ("whales", "bets"):
        key = f"discord_{channel}_webhook"
        cfg[key] = os.environ.get(f"MONEY_RADAR_{channel.upper()}_WEBHOOK") or cfg.get(key) or ""
    return cfg


def single_instance():
    """Hold a local port so a second copy (login task + double-click) exits instead of double-alerting."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 47391))
    except OSError:
        return None
    return s


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    if sys.stdout:  # None under pythonw (the login task)
        print(line, flush=True)
    with open(HERE / "data" / "radar.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def cmd_collect(cfg, db):
    now = int(time.time())
    log(f"catalog: {collect.refresh_catalog(db, now)} engine games")
    collect.refresh_affiliates(db, now)
    log(f"releases: {collect.refresh_releases(db, now, force=True)} recent releases read")
    log(f"release dates: {collect.scan_release_dates(db, cfg, now, budget=4000)} games looked up")
    log(f"backfill: {len(collect.backfill(db, cfg, now))} counted bets from recent releases")
    log(f"poll: {len(collect.poll(db, cfg, now))} new counted bets")
    predict.write(db, cfg, now, PAGE, REPORT)
    log(f"dashboard: {PAGE}")


def bets_export(db, cfg, now, days=14):
    """Counted bets of the last `days` (no names) plus health numbers, for checking the radar
    against stakecruncher. Published next to the dashboard as bets.json."""
    rows = db.execute(
        "SELECT b.bet_key, b.site, b.slug, g.name, g.studio, b.amount_usd, b.payout_usd, b.kind, b.tier, b.age_days, b.hidden, b.created_at "
        "FROM bets b JOIN games g ON g.slug=b.slug WHERE b.created_at>=? ORDER BY b.created_at DESC", (now - days * 86400,)
    ).fetchall()
    return {
        "generated_at": now,
        "overflows": store.meta_get(db, "overflows", 0),
        "waiting_on_new_games": len(collect._pending),
        "bets": [dict(r) for r in rows if not collect.excluded_studio(cfg, r["studio"])],
    }


def publish(db, cfg):
    """On GitHub Actions: force-push the dashboard (index.html) and bets.json to a one-commit
    branch (MONEY_RADAR_PUBLISH_BRANCH, gh-pages) that GitHub Pages serves."""
    import shutil
    import subprocess
    import tempfile

    branch = os.environ.get("MONEY_RADAR_PUBLISH_BRANCH")
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not (branch and token and repo):
        return
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy(PAGE, Path(tmp) / "index.html")
        (Path(tmp) / "bets.json").write_text(json.dumps(bets_export(db, cfg, int(time.time()))), encoding="utf-8")
        git = ["git", "-C", tmp, "-c", "user.name=radar-bot", "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com"]
        for args in (["init", "-q", "-b", branch], ["add", "index.html", "bets.json"], ["commit", "-qm", "Money Radar dashboard"],
                     ["push", "-qf", f"https://x-access-token:{token}@github.com/{repo}.git", f"{branch}:{branch}"]):
            r = subprocess.run(git + args, capture_output=True, text=True)
            if r.returncode:
                log(f"publish failed at git {args[0]}: {r.stderr.strip()[:200].replace(token, '***')}")
                return


def cmd_watch(cfg, db, minutes=None):
    """Poll forever, or for `minutes` and then exit cleanly (each GitHub Actions run hands over)."""
    stop_at = time.time() + minutes * 60 if minutes else None
    lock = single_instance()
    if not lock:
        log("already running; this copy exits")
        return
    # Before 10-28, a slug whose demo page didn't answer was marked "not engine" for good (no name
    # saved with it). Give those another look; a real other-provider answer always carried a name.
    reopened = db.execute("UPDATE games SET not_engine=NULL, info_at=NULL WHERE not_engine=1 AND name IS NULL").rowcount
    db.commit()
    if reopened:
        log(f"re-checking {reopened} games that an unanswered lookup had marked 'not engine'")
    # "The Originals" catalog entries are Stake's own games mapped onto engine slugs ("dice")
    db.execute("UPDATE games SET not_engine=1 WHERE studio='The Originals'")
    junk = db.execute("DELETE FROM bets WHERE slug IN (SELECT slug FROM games WHERE not_engine=1)").rowcount
    db.commit()
    if junk:
        log(f"dropped {junk} stored bets on games that aren't Stake Engine")
    hooks = ", ".join(f"{c} {'on' if cfg[f'discord_{c}_webhook'] else 'OFF'}" for c in ("whales", "bets"))
    log(f"watching every {cfg['poll_seconds']}s; Discord {hooks}")
    last_report = 0
    while not stop_at or time.time() < stop_at:
        now = int(time.time())
        try:
            collect.refresh_all(db, now)
            rows = collect.poll(db, cfg, now)  # alerts first, slow catch-up work after
            if rows:
                log(f"{len(rows)} new counted bets")
                alerts.dispatch(db, rows, cfg, now, say=log)
            collect.scan_release_dates(db, cfg, now, budget=3)
            caught_up = collect.backfill(db, cfg, now, limit=1)
            if caught_up:  # a new release's first bets: any still fresh alert like live ones
                alerts.dispatch(db, caught_up, cfg, now, say=log)
            if now - last_report >= REPORT_EVERY:
                predict.write(db, cfg, now, PAGE, REPORT)
                publish(db, cfg)
                log(f"health: $1k feed overflowed {store.meta_get(db, 'overflows', 0)} times so far; "
                    f"{len(collect._pending)} bets waiting on brand-new games")
                last_report = now
        except KeyboardInterrupt:
            raise
        except Exception:
            log("error: " + traceback.format_exc(limit=3).strip().replace("\n", " | "))
            time.sleep(60)
        time.sleep(cfg["poll_seconds"])
    predict.write(db, cfg, int(time.time()), PAGE, REPORT)
    publish(db, cfg)
    log("time limit reached; handing over to the next run")


def selftest():
    """Offline: fake catalog and feed, check counting, alerts and the dashboard."""
    import tempfile

    import classify
    import sc

    tiers = {"spin_high_usd": 1000, "buy_high_usd": 10000, "extreme_usd": 50000}
    modes = [{"name": "BASE", "cost": 1}, {"name": "ANTE", "cost": 1.25}, {"name": "BONUS", "cost": 100}, {"name": "MYSTERY", "cost": 500}]
    cases = [
        # amount, payout, currency, expected kind, expected tier
        (1000, 0, "usdt", "spin", "high"),                 # $1k normal spin counts
        (999, 0, "usdt", "spin", None),
        (5000, 12000, "usdt", "unclear", None),            # $5k spin or $50 bonus: can't tell, buy bar
        (20000, 30000, "usdc", "unclear", "high"),         # $20k spin or $200 bonus: high either way
        (1730.5, 2500, "usdt", "spin", "high"),            # $17.305 can't be a bonus stake
        (12345.67, 4000, "btc", "unclear", "high"),        # crypto, paid: unclear -> buy bar, still over $10k
        (4321.98, 900, "eth", "unclear", None),            # crypto, unclear, under the buy bar
        (4321.98, 0, "eth", "spin", "high"),               # paid nothing -> spin
        (60000, 0, "usdt", "spin", "extreme"),
        (2000, 0, "usdt", "spin", "high"),
    ]
    bad = 0
    for amount, payout, cur, want_kind, want_tier in cases:
        kind, mode, cost = classify.classify(amount, payout, cur, modes)
        got_tier = classify.tier(amount, kind, tiers)
        ok = kind == want_kind and got_tier == want_tier
        bad += not ok
        print(f"{'ok ' if ok else 'BAD'} ${amount:>10,.2f} {cur:<5} paid {payout:>6} -> {kind:<8} {mode or '-':<8} {got_tier}")

    now = int(time.time())
    day = 86400
    games = [
        {"slug": "fresh-studio-new-game", "catalog_slug": "fresh-studio-new-game", "name": "New Game", "studio": "Fresh Studio", "released_at": now - 2 * day, "game_id": 1},
        {"slug": "paperclipgaming-house-game", "catalog_slug": "paperclip-gaming-house-game", "name": "House Game", "studio": "Paperclip Gaming", "released_at": now - day, "game_id": 3},
        {"slug": "old-studio-old-game","catalog_slug": "old-studio-old-game", "name": "Old Game", "studio": "Old Studio", "released_at": now - 200 * day, "game_id": 2},
    ]
    feed = [
        {"betKey": "a1", "site": "com", "username": None, "gameSlug": "fresh-studio-new-game", "currency": "usdt", "amountUsd": 1500, "payoutUsd": 0, "createdAt": now - 60},
        {"betKey": "a2", "site": "com", "username": "someone", "gameSlug": "fresh-studio-new-game", "currency": "usdt", "amountUsd": 75000, "payoutUsd": 1000, "createdAt": now - 30},
        {"betKey": "a3", "site": "com", "username": "partner", "gameSlug": "fresh-studio-new-game", "currency": "usdt", "amountUsd": 90000, "payoutUsd": 0, "createdAt": now - 20},
        {"betKey": "a4", "site": "com", "username": None, "gameSlug": "old-studio-old-game", "currency": "usdt", "amountUsd": 30000, "payoutUsd": 0, "createdAt": now - 10},
        {"betKey": "a8", "site": "com", "username": None, "gameSlug": "paperclipgaming-house-game", "currency": "usdt", "amountUsd": 80000, "payoutUsd": 0, "createdAt": now - 8},
        {"betKey": "a5", "site": "us", "username": None, "gameSlug": "pragmatic-not-engine", "currency": "usdt", "amountUsd": 99000, "payoutUsd": 0, "createdAt": now - 5},
    ]
    # history: two weeks of counted bets so the dashboard has patterns to show
    hist = [
        {"betKey": f"h{i}", "site": "com", "username": None, "gameSlug": "fresh-studio-new-game", "currency": "usdt",
         "amountUsd": 1000 * (1 + i % 7), "payoutUsd": 0, "createdAt": now - 2 * day + i * 1800}
        for i in range(90)
    ]
    sc.feed = lambda min_usd, game_id=None, limit=100: list(feed)
    sc.game_info = lambda slug: None
    sc.game_modes = lambda s: modes

    with tempfile.TemporaryDirectory() as tmp:
        db = store.connect(Path(tmp) / "t.db")
        for g in games:
            store.upsert_game(db, g.pop("slug"), **g)
        db.commit()
        store.meta_set(db, "affiliates", [store.account_hash("com", "partner")])
        cfg = {"tiers": tiers, "new_release_days": 14, "exclude_affiliates": True, "exclude_studios": ["Paperclip Gaming"],
               "discord_whales_webhook": "W", "discord_bets_webhook": "B"}
        affiliates = set(store.meta_get(db, "affiliates"))
        for b in hist:
            collect.ingest(db, b, cfg, affiliates, now)
        rows = collect.poll(db, cfg, now)
        got = sorted(r["bet_key"] for r in rows)
        ok = got == ["a1", "a2", "a4"]  # partner (affiliate) and the non-engine game are dropped
        bad += not ok
        print(f"{'ok ' if ok else 'BAD'} poll kept {got} (want a1, a2, a4: affiliate, internal-studio and non-engine bets dropped)")
        stored = db.execute("SELECT COUNT(*) FROM bets WHERE bet_key IN ('a1','a2','a3','a4','a5')").fetchone()[0]
        names = db.execute("SELECT * FROM bets LIMIT 1").fetchone().keys()
        ok = stored == 3 and "username" not in names
        bad += not ok
        print(f"{'ok ' if ok else 'BAD'} {stored} stored, no player-name column: {'username' not in names}")

        win = {"betKey": "a6", "site": "com", "username": None, "gameSlug": "fresh-studio-new-game", "currency": "btc",
               "amountUsd": 1500, "payoutUsd": 3000, "createdAt": now - 15}
        row = collect.ingest(db, win, cfg, affiliates, now)
        ok = bool(row) and row["kind"] == "spin" and row["tier"] == "high"
        bad += not ok
        print(f"{'ok ' if ok else 'BAD'} winning bet at the price of a blank spin just before counts as a spin: {row and row['kind']}")
        db.execute("DELETE FROM bets WHERE bet_key='a6'")

        captured = []
        sent = alerts.dispatch(db, rows, cfg, now, sender=lambda url, p: captured.append((url, p)) or True)
        whales = [e["title"] for p in sent["whales"] for e in p["embeds"]]
        bets = [e["title"] for p in sent["bets"] for e in p["embeds"]]
        # a1 ($1,500) and a2 ($75k) each start a whale session on the new game; old game a4 is skipped
        ok = whales == ["Whale active on New Game"] * 2 and len(bets) == 2 and bets[1].startswith("EXTREMELY HIGH")
        ok = ok and [f["value"] for p in sent["bets"] for e in p["embeds"] for f in e["fields"] if f["name"] == "Player"] == ["Anonymous", "Public player"]
        ok = ok and "someone" not in json.dumps(sent)
        ok = ok and {u for u, _ in captured} == {"W", "B"}
        bad += not ok
        print(f"{'ok ' if ok else 'BAD'} whales {whales}, bets {bets}")
        more = {"betKey": "a7", "site": "com", "username": None, "gameSlug": "fresh-studio-new-game", "currency": "usdt",
                "amountUsd": 75000, "payoutUsd": 0, "createdAt": now - 5}
        row = collect.ingest(db, more, cfg, affiliates, now)
        cont = alerts.dispatch(db, [row], cfg, now, sender=lambda url, p: True)
        ok = not cont["whales"] and len(cont["bets"]) == 1
        bad += not ok
        print(f"{'ok ' if ok else 'BAD'} next $75k bet in the same session: bet alert only, no second whale alert")
        # a brand-new game with no demo page yet: its bet waits, then alerts (marked late) once known
        fresh = {"betKey": "n1", "site": "com", "username": None, "gameSlug": "brand-new-game", "currency": "usdt",
                 "amountUsd": 60000, "payoutUsd": 0, "createdAt": now - 20 * 60}
        sc.feed = lambda min_usd, game_id=None, limit=100: [fresh]
        held = collect.poll(db, cfg, now)
        g = store.game(db, "brand-new-game")
        ok = not held and "n1" in collect._pending and g["not_engine"] is None
        sc.feed = lambda min_usd, game_id=None, limit=100: []
        sc.game_info = lambda slug: {"provider": "stake-engine", "name": "Brand New", "studioName": "Fresh Studio",
                                     "engineSlug": "brand-new", "gameId": 9, "stakeReleasedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600))} \
            if slug == "brand-new-game" else {"provider": "pragmatic"}
        resolved = collect.poll(db, cfg, now + collect.UNKNOWN_RETRY)
        late_sent = alerts.dispatch(db, resolved, cfg, now + collect.UNKNOWN_RETRY, sender=lambda url, p: True)
        ok = ok and [r["bet_key"] for r in resolved] == ["n1"] and resolved[0].get("late")
        ok = ok and len(late_sent["bets"]) == 1 and "Spotted late" in late_sent["bets"][0]["embeds"][0]["description"]
        bad += not ok
        print(f"{'ok ' if ok else 'BAD'} brand-new game with no page yet: bet held, then alerted late once known")
        db.execute("DELETE FROM bets WHERE bet_key='n1'")
        sc.game_info = lambda slug: None

        late = alerts.dispatch(db, rows, cfg, now + 3600, sender=lambda url, p: True)
        ok = not late["whales"] and not late["bets"]
        bad += not ok
        print(f"{'ok ' if ok else 'BAD'} stale bets (backfill) never alert")

        rep = predict.write(db, cfg, now, Path(tmp) / "d.html", Path(tmp) / "r.json")
        html_ok = "Best time to release" in (Path(tmp) / "d.html").read_text(encoding="utf-8")
        ok = rep["new_bets"] == 93 and rep["release_picks"] and html_ok
        bad += not ok
        print(f"{'ok ' if ok else 'BAD'} report: {rep['new_bets']} new-release bets, picks {[p['when'] for p in rep['release_picks']]}, page ok {html_ok}")
        print(json.dumps(captured[0], indent=1)[:900] if captured else "")
        db.close()
    print("ALL OK" if not bad else f"{bad} FAILED")
    return bad


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "selftest":
        sys.exit(1 if selftest() else 0)
    cfg = config()
    db = store.connect(DB)
    if cmd == "collect":
        cmd_collect(cfg, db)
    elif cmd == "watch":
        minutes = float(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[2] == "--for" else None
        if minutes and not (cfg["discord_whales_webhook"] or cfg["discord_bets_webhook"]):
            minutes = min(minutes, 25)  # no secrets yet: hand over soon so a later run picks them up
        cmd_watch(cfg, db, minutes)
    elif cmd == "report":
        rep = predict.write(db, cfg, int(time.time()), PAGE, REPORT)
        print(f"{rep['bets']} counted bets -> {PAGE}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
