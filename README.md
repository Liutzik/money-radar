# Money Radar

Watches big money on **new Stake Engine releases** (games 14 days old or less), learns
which days and hours it shows up, suggests the best time to release a game, and posts
Discord alerts the moment big money starts on a new release.

It counts money, not people: no player names are stored, hidden players count the same as
everyone else, and Stake's partnered streamers (affiliates playing on Stake's money) are
left out. Stake's internal studios (Paperclip, Titan, Massive, Twist ...) are left out of
everything; the list is `exclude_studios` in `config.json`.

It reads every $1,000+ bet on all of Stake every 20 seconds (the feed holds about two
minutes of them), so any engine game counts the moment it gets a big bet, including games
released after the radar started.

## What counts

| Bet | High | Extremely high |
| --- | --- | --- |
| Normal spin | $1,000+ | $50,000+ |
| Bonus buy, feature spin, mystery spin | $10,000+ | $50,000+ |

Stakecruncher's feed shows the price of a bet, not the mode. The radar works it out from
each game's mode prices: a bet that paid nothing is a spin, so is a win at the same price
as blank spins just before, and a price that can't be split into a real bonus stake is a
spin. When it still can't tell, the bet must clear the $10,000 bar. Change the numbers in
`config.json`.

## Where it runs

On GitHub Actions (`.github/workflows/money-radar.yml`), nonstop: each run watches for
about 5h40m and hands over to the next. The dashboard is pushed to the `gh-pages` branch
every 15 minutes and served by GitHub Pages.

The two Discord webhooks are repository secrets `MONEY_RADAR_WHALES_WEBHOOK` and
`MONEY_RADAR_BETS_WEBHOOK` (Settings -> Secrets and variables -> Actions).

Locally, for testing:

    python radar.py selftest   offline check (no network)
    python radar.py collect    one pass, then rebuild dashboard.html
    python radar.py watch      poll until stopped

## Discord

- whales: "Whale active on <game>" when a run of big bets at one price starts on a new
  release (a new run = no bet at that price on that game for 20 minutes).
- bets: every counted bet on a new release.

Players show as "Anonymous" (Stake's hidden mode) or "Public player"; no names.

Data comes from stakecruncher.com's public tracker, at most one request every 1.5 seconds.
