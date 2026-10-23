"""Was a bet a normal spin or a bought bonus / feature spin, and how big was it?

The feed only gives the price paid, not the mode. Each game's modes have a cost multiplier
(BASE 1x, a bonus 100x, a 500x mystery spin ...), so a price P is either a P-sized spin or a
P/cost-sized buy. On stablecoin bets the real stake lands on a clean bet size, which picks the
mode; a bet that paid nothing is almost always a spin (bought bonuses rarely blank), and so is a
winning bet at the same price as blank spins on that game minutes earlier (collect.py checks
that). Anything still ambiguous is "unclear" and has to clear the stricter buy bar.

Counting rules (config.json "tiers"):
  normal spin            >= $1,000   high
  buy / feature / unclear >= $10,000 high
  anything               >= $50,000  extreme
"""
STABLE = {"usd", "usdt", "usdc", "busd", "dai", "sweeps", "gold", "pyusd"}
SPIN_MAX_COST = 5  # modes cheaper than 5x (base, ante) are normal spins; 5x and up are buys
STEPS = (1, 1.5, 2, 2.5, 3, 4, 5, 6, 7.5, 8)
LADDER = sorted({round(m * 10 ** k, 2) for k in range(-1, 7) for m in STEPS})


def cleanliness(base):
    """2 for a common bet size, 1 for whole dollars, 0 for whole cents, -1 when it can't be a stake."""
    cents = round(base, 2)
    if abs(base - cents) > 0.0005:
        return -1
    if any(abs(cents - step) < 0.0005 for step in LADDER):
        return 2
    return 1 if abs(cents - round(cents)) < 0.0005 else 0


def classify(amount, payout, currency, modes):
    """Return (kind, mode_name, cost): kind is spin, buy or unclear."""
    modes = modes or [{"name": "BASE", "cost": 1.0}]
    spins = [m for m in modes if m["cost"] < SPIN_MAX_COST]
    buys = [m for m in modes if m["cost"] >= SPIN_MAX_COST]
    if not buys:
        m = min(spins, key=lambda m: m["cost"])
        return "spin", m["name"], m["cost"]
    if not spins:
        m = max(buys, key=lambda m: m["cost"])
        return "buy", m["name"], m["cost"]

    if (currency or "").lower() in STABLE:
        def best(group):
            return max(((cleanliness(amount / m["cost"]), m) for m in group), key=lambda t: (t[0], -t[1]["cost"]))
        spin_score, spin = best(spins)
        buy_score, buy = best(buys)
        if spin_score > buy_score:
            return "spin", spin["name"], spin["cost"]
        if buy_score > spin_score:
            return "buy", buy["name"], buy["cost"]

    base = min(spins, key=lambda m: m["cost"])
    if not payout:
        return "spin", base["name"], base["cost"]
    return "unclear", None, None


def tier(amount, kind, tiers):
    """high / extreme / None under the user's counting rules."""
    if amount >= tiers["extreme_usd"]:
        return "extreme"
    if kind == "spin" and amount >= tiers["spin_high_usd"]:
        return "high"
    if kind in ("buy", "unclear") and amount >= tiers["buy_high_usd"]:
        return "high"
    return None
