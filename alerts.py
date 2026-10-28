"""Discord alerts for big money on new Stake Engine releases. No player names, ever.

Two channels:
  whales  - "a whale is active": the first bet of a new whale session. A session is a run of
            counted bets on the same game at the same price (within 1%) with gaps under
            SESSION_GAP; it stands for one high roller playing right now, without saying who.
  bets    - every counted bet, batched up to 10 per message.

No player names: alerts say "Anonymous" for players in Stake's hidden mode and
"Public player" for everyone else.
"""
import json
import time
import urllib.error
import urllib.request

COLORS = {"high": 0xF5A524, "extreme": 0xE5484D, "whale": 0x3B82F6}
SITES = {"com": "https://stake.com", "us": "https://stake.us"}
SESSION_GAP = 20 * 60
FRESH = 15 * 60  # only alert on bets that landed in the last 15 minutes (not on old history)
LATE = 3 * 3600  # bets held while a brand-new game was unknown still alert, marked late
LATE_NOTE = " Spotted late: the game was too new for stakecruncher's lists when this bet landed."


def usd(x):
    return f"${x:,.0f}"


def age_text(days):
    if days is None:
        return "release date unknown"
    if days < 1:
        return f"{max(1, round(days * 24))}h old"
    return f"{days:.1f} days old"


def kind_text(row):
    if row["kind"] == "spin":
        return "normal spin"
    if row["kind"] == "buy":
        cost = f" ({row['cost']:g}x)" if row.get("cost") else ""
        return f"{(row.get('mode') or 'bonus').replace('_', ' ').lower()} buy{cost}"
    return "spin or buy (unclear)"


def tier_text(row):
    return "EXTREMELY HIGH" if row["tier"] == "extreme" else "HIGH"


def link(row):
    return f"{SITES.get(row['site'], SITES['com'])}/casino/games/{row['slug']}"


def player(row):
    return "Anonymous" if row["hidden"] else "Public player"


def new_session(db, row):
    """True when no counted bet at this price hit this game in the SESSION_GAP before it."""
    prev = db.execute(
        "SELECT 1 FROM bets WHERE site=? AND slug=? AND bet_key<>? AND ABS(amount_usd-?)<=?*0.01 AND created_at BETWEEN ? AND ?",
        (row["site"], row["slug"], row["bet_key"], row["amount_usd"], row["amount_usd"], row["created_at"] - SESSION_GAP, row["created_at"]),
    ).fetchone()
    return prev is None


def whale_embed(row):
    who = "An anonymous whale" if row["hidden"] else "A whale"
    return {
        "title": f"Whale active on {row['name']}"[:256],
        "url": link(row),
        "color": COLORS["whale"],
        "description": (
            f"{who} just started playing **{row['name']}** on stake.{row['site']}: "
            f"**{usd(row['amount_usd'])}** per bet ({kind_text(row)}), {tier_text(row).lower()}."
            + (LATE_NOTE if row.get("late") else "")
        ),
        "fields": [
            {"name": "Studio", "value": row.get("studio") or "?", "inline": True},
            {"name": "Game age", "value": age_text(row.get("age_days")), "inline": True},
        ],
        "thumbnail": {"url": row["image"]} if row.get("image") else None,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(row["created_at"])),
    }


def bet_embed(row):
    multi = row["payout_usd"] / row["amount_usd"] if row["amount_usd"] else 0
    return {
        "title": f"{tier_text(row)}: {usd(row['amount_usd'])} on {row['name']}"[:256],
        "url": link(row),
        "color": COLORS[row["tier"]],
        "description": f"{kind_text(row).capitalize()} on stake.{row['site']}. Paid {usd(row['payout_usd'])} ({multi:.2f}x)."
        + (LATE_NOTE if row.get("late") else ""),
        "fields": [
            {"name": "Player", "value": player(row), "inline": True},
            {"name": "Studio", "value": row.get("studio") or "?", "inline": True},
            {"name": "Game age", "value": age_text(row.get("age_days")), "inline": True},
        ],
        "thumbnail": {"url": row["image"]} if row.get("image") else None,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(row["created_at"])),
    }


def clean(embed):
    return {k: v for k, v in embed.items() if v is not None}


def send(webhook, payload):
    """POST to a Discord webhook, waiting out rate limits when Discord asks."""
    body = json.dumps(payload).encode()
    for _ in range(4):
        req = urllib.request.Request(webhook, data=body, headers={"Content-Type": "application/json", "User-Agent": "MoneyRadar/1.0"})
        try:
            urllib.request.urlopen(req, timeout=15).close()
            time.sleep(0.6)  # stay well under 5 messages / 2 s per webhook
            return True
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(float(json.loads(e.read() or b"{}").get("retry_after", 2)) + 0.2)
                continue
            print(f"discord error {e.code}: {e.read()[:200]!r}")
            return False
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"discord unreachable: {e}")
            time.sleep(3)
    return False


def wanted(row, cfg, now):
    if now - row["created_at"] > (LATE if row.get("late") else FRESH):
        return False
    if cfg.get("alert_all_engine_games"):
        return True
    age = row.get("age_days")
    return age is not None and age <= cfg["new_release_days"]


def dispatch(db, rows, cfg, now, sender=send, say=print):
    """Send whale-session and per-bet alerts for freshly polled rows. Returns what was sent."""
    rows = sorted((r for r in rows if wanted(r, cfg, now)), key=lambda r: r["created_at"])
    whales = [clean(whale_embed(r)) for r in rows if new_session(db, r)]
    bets = [clean(bet_embed(r)) for r in rows]
    sent = {"whales": [], "bets": []}
    for channel, embeds in (("whales", whales), ("bets", bets)):
        hook = cfg.get(f"discord_{channel}_webhook")
        for i in range(0, len(embeds), 10):
            payload = {"username": "Money Radar", "embeds": embeds[i:i + 10]}
            ok = sender(hook, payload) if hook else False
            for e in payload["embeds"]:
                say(f"[{channel}{' sent' if ok else ' NOT sent'}] {e['title']}")
            sent[channel].append(payload)
    return sent
