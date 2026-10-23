"""When does big money play new Stake Engine releases, and which release time catches the most?

Everything is in the viewer's local time. Money is the price paid on counted bets (see
classify.py for the counting rules).
"""
import html
import json
import statistics
import time
from datetime import datetime, timedelta

import collect

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def how(ts):
    """Hour of the week in local time, Monday 00:00 = 0."""
    d = datetime.fromtimestamp(ts)
    return d.weekday() * 24 + d.hour


def how_label(h):
    return f"{DAYS[h // 24]} {h % 24:02d}:00"


def report(db, cfg, now):
    n_days = cfg["new_release_days"]
    rows = [dict(r) for r in db.execute(
        "SELECT b.*, g.name, g.studio, g.released_at FROM bets b JOIN games g ON g.slug=b.slug ORDER BY b.created_at"
    ) if not collect.excluded_studio(cfg, r["studio"])]
    out = {"generated_at": now, "new_release_days": n_days, "tiers": cfg["tiers"], "bets": len(rows)}
    if not rows:
        return out
    first, last = rows[0]["created_at"], rows[-1]["created_at"]
    span_days = max(1.0, (last - first) / 86400)
    weeks = max(1.0, span_days / 7)
    new = [r for r in rows if r["age_days"] is not None and 0 <= r["age_days"] <= n_days]
    total = sum(r["amount_usd"] for r in rows)
    new_total = sum(r["amount_usd"] for r in new)

    # When: money on new releases by local weekday x hour, averaged per week.
    heat = [[0.0] * 24 for _ in range(7)]
    for r in new:
        h = how(r["created_at"])
        heat[h // 24][h % 24] += r["amount_usd"] / weeks
    profile = [heat[h // 24][h % 24] for h in range(168)]
    by_day = [sum(row) for row in heat]

    # How soon after release: share of new-release money by game age in days.
    age = [0.0] * (n_days + 1)
    for r in new:
        age[min(n_days, int(r["age_days"]))] += r["amount_usd"]
    age_share = [a / new_total if new_total else 0 for a in age]
    firsts = {}
    for r in new:
        if r["released_at"] and r["released_at"] >= first:  # only games we watched from launch
            firsts.setdefault(r["slug"], r["age_days"])
    median_first = statistics.median(firsts.values()) if firsts else None

    # Best release time: expected big money a game collects in its first N days if it goes
    # live at hour-of-week s = sum over its first N days of the weekly profile x the age curve.
    def catch(s):
        return sum(profile[(s + t) % 168] * age_share[min(n_days, t // 24)] / 24 for t in range(n_days * 24))
    scores = [catch(s) for s in range(168)]
    mean = sum(scores) / 168 or 1
    best = sorted(range(168), key=lambda s: -scores[s])
    picks, used = [], set()
    for s in best:  # one pick per weekday
        if s // 24 not in used:
            used.add(s // 24)
            picks.append({"when": how_label(s), "vs_avg": scores[s] / mean - 1})
        if len(picks) == 3:
            break

    # Next likely windows in the coming 7 days.
    now_h = how(now)
    ahead = sorted(range(1, 168), key=lambda k: -profile[(now_h + k) % 168])[:5]
    nxt = []
    for k in sorted(ahead):
        t = datetime.fromtimestamp(now) + timedelta(hours=k)
        nxt.append({"when": t.strftime("%a %d %b %H:00"), "usd_per_week": profile[(now_h + k) % 168]})

    games = {}
    for r in new:
        g = games.setdefault(r["slug"], {"name": r["name"], "studio": r["studio"], "usd": 0, "bets": 0, "extreme": 0,
                                          "first_age": r["age_days"], "released_at": r["released_at"]})
        g["usd"] += r["amount_usd"]
        g["bets"] += 1
        g["extreme"] += r["tier"] == "extreme"
    studios = {}
    for g in games.values():
        s = studios.setdefault(g["studio"] or "?", {"usd": 0, "games": 0})
        s["usd"] += g["usd"]
        s["games"] += 1

    kinds = {}
    for r in new:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + r["amount_usd"]

    out.update({
        "from": first, "to": last, "span_days": span_days, "total_usd": total, "new_usd": new_total,
        "new_share": new_total / total if total else 0,
        "hidden_share": sum(r["amount_usd"] for r in new if r["hidden"]) / new_total if new_total else 0,
        "extreme_bets": sum(r["tier"] == "extreme" for r in new), "new_bets": len(new),
        "heat": heat, "by_day": by_day, "busiest_day": DAYS[max(range(7), key=lambda d: by_day[d])] if new else None,
        "busiest_hour": how_label(max(range(168), key=lambda h: profile[h])) if new else None,
        "age_share": age_share, "median_first_day": median_first, "release_picks": picks, "next": nxt,
        "kinds": kinds,
        "games": sorted(games.values(), key=lambda g: -g["usd"])[:20],
        "studios": sorted(({"studio": k, **v} for k, v in studios.items()), key=lambda s: -s["usd"])[:12],
    })
    return out


def usd(x):
    if x >= 1e6:
        return f"${x / 1e6:.1f}M"
    if x >= 1e3:
        return f"${x / 1e3:.0f}k"
    return f"${x:,.0f}"


def render(rep):
    e = html.escape
    if not rep.get("bets"):
        body = "<p class='muted'>No counted bets yet. Leave the watcher running; the first numbers appear after a few polls.</p>"
        return PAGE.replace("{{BODY}}", body).replace("{{UPDATED}}", "")
    peak = max(max(r) for r in rep["heat"]) or 1
    cells = []
    for d, row in enumerate(rep["heat"]):
        cells.append(f"<div class='hl'>{DAYS[d]}</div>")
        for h, v in enumerate(row):
            a = v / peak
            cells.append(f"<div class='hc' style='--a:{a:.3f}' title='{DAYS[d]} {h:02d}:00 - {usd(v)}/week'></div>")
    hours = "".join(f"<div class='hh'>{h:02d}</div>" if h % 3 == 0 else "<div></div>" for h in range(24))
    amax = max(rep["age_share"]) or 1
    bars = "".join(
        f"<div class='bar'><div class='fill' style='height:{100 * a / amax:.0f}%'></div><span>{i}{'+' if i == len(rep['age_share']) - 1 else ''}</span></div>"
        for i, a in enumerate(rep["age_share"])
    )
    picks = "".join(
        f"<li><b>{e(p['when'])}</b> <span class='muted'>{p['vs_avg']:+.0%} vs an average release time</span></li>" for p in rep["release_picks"]
    ) if rep["span_days"] >= 7 else f"<li class='muted'>Needs a full week of data ({rep['span_days']:.1f} days so far).</li>"
    nxt = "".join(f"<li><b>{e(n['when'])}</b> <span class='muted'>~{usd(n['usd_per_week'])} usually</span></li>" for n in rep["next"])
    games = "".join(
        f"<tr><td>{e(g['name'] or '?')}</td><td>{e(g['studio'] or '?')}</td><td class='n'>{usd(g['usd'])}</td>"
        f"<td class='n'>{g['bets']}</td><td class='n'>{g['extreme']}</td><td class='n'>{g['first_age']:.1f}d</td></tr>"
        for g in rep["games"]
    )
    studios = "".join(
        f"<tr><td>{e(s['studio'])}</td><td class='n'>{s['games']}</td><td class='n'>{usd(s['usd'])}</td></tr>" for s in rep["studios"]
    )
    kinds = " · ".join(f"{k}: {usd(v)}" for k, v in sorted(rep["kinds"].items(), key=lambda kv: -kv[1]))
    first_day = f"{rep['median_first_day']:.1f} days" if rep["median_first_day"] is not None else "not enough launches yet"
    thin = rep["span_days"] < 14
    t = rep["tiers"]
    body = f"""
<p class='muted'>Counted: normal spins of {usd(t['spin_high_usd'])}+, buys and feature spins of {usd(t['buy_high_usd'])}+, and anything over {usd(t['extreme_usd'])} as extremely high. A new release means a game {rep['new_release_days']} days old or less. Player names are never stored.</p>
{"<p class='warn'>Only " + f"{rep['span_days']:.1f}" + " days of data so far. The day and hour patterns firm up after two or three weeks of watching.</p>" if thin else ""}
<div class='tiles'>
  <div class='tile'><span>Big money on new releases</span><b>{usd(rep['new_usd'])}</b><small>{rep['new_bets']} bets, {rep['extreme_bets']} extremely high</small></div>
  <div class='tile'><span>Busiest day for new releases</span><b>{e(rep['busiest_day'] or '-')}</b><small>peak hour {e(rep['busiest_hour'] or '-')}</small></div>
  <div class='tile'><span>First big bet after launch</span><b>{e(first_day)}</b><small>median, games watched from launch</small></div>
  <div class='tile'><span>From hidden players</span><b>{rep['hidden_share']:.0%}</b><small>of new-release money</small></div>
</div>
<div class='two'>
  <section><h2>Best time to release</h2><p class='muted'>Release times whose first {rep['new_release_days']} days catch the most big money, from the patterns below.</p><ol>{picks}</ol></section>
  <section><h2>Next likely big-money hours</h2><p class='muted'>Coming 7 days, ranked by how much usually lands on new releases.</p><ol>{nxt}</ol></section>
</div>
<section><h2>When big money plays new releases</h2><p class='muted'>Average per week, your local time. Darker = more money.</p>
<div class='heat'><div></div>{hours}{''.join(cells)}</div></section>
<section><h2>How soon after release it arrives</h2><p class='muted'>Share of new-release big money by game age in days. {e(kinds)}</p><div class='bars'>{bars}</div></section>
<section><h2>New releases big money played</h2><div class='scroll'><table><tr><th>Game</th><th>Studio</th><th class='n'>Money</th><th class='n'>Bets</th><th class='n'>Extreme</th><th class='n'>First big bet</th></tr>{games}</table></div></section>
<section><h2>Studios</h2><div class='scroll'><table><tr><th>Studio</th><th class='n'>Games</th><th class='n'>Money</th></tr>{studios}</table></div></section>
"""
    span = f"{datetime.fromtimestamp(rep['from']):%d %b %H:%M} to {datetime.fromtimestamp(rep['to']):%d %b %H:%M}"
    updated = f"Data {span} · updated {datetime.fromtimestamp(rep['generated_at']):%a %d %b %H:%M}"
    return PAGE.replace("{{BODY}}", body).replace("{{UPDATED}}", e(updated))


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Money Radar</title>
<style>
:root{--bg:#f6f5f2;--card:#fff;--ink:#1c1b19;--muted:#6b6862;--line:#e4e1da;--hot:#d9480f;--warn:#9a6700}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#121212;--card:#1c1c1c;--ink:#ecebe8;--muted:#9a978f;--line:#2e2d2a;--hot:#ff7a3d;--warn:#e3b341}}
:root[data-theme="dark"]{--bg:#121212;--card:#1c1c1c;--ink:#ecebe8;--muted:#9a978f;--line:#2e2d2a;--hot:#ff7a3d;--warn:#e3b341}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 48px}h1{margin:0 0 4px;font-size:24px}h2{font-size:17px;margin:0 0 4px}
.muted{color:var(--muted)}.warn{color:var(--warn)}section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-top:16px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-top:16px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;display:flex;flex-direction:column}
.tile span{color:var(--muted);font-size:13px}.tile b{font-size:24px;margin:4px 0}.tile small{color:var(--muted)}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}.two section{margin-top:16px}
ol{margin:8px 0 0;padding-left:22px}li{margin:4px 0}
.heat{display:grid;grid-template-columns:36px repeat(24,1fr);gap:2px;margin-top:10px}
.hc{aspect-ratio:1;border-radius:3px;background:color-mix(in srgb,var(--hot) calc(var(--a)*100%),var(--line))}
.hl,.hh{font-size:11px;color:var(--muted)}.hl{align-self:center}
.bars{display:flex;align-items:flex-end;gap:4px;height:160px;margin-top:10px}
.bar{flex:1;height:100%;display:flex;flex-direction:column;justify-content:flex-end;align-items:center}
.fill{width:100%;background:var(--hot);border-radius:4px 4px 0 0;min-height:1px}.bar span{font-size:11px;color:var(--muted)}
.scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;margin-top:8px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--muted);font-weight:500;font-size:13px}.n{text-align:right;font-variant-numeric:tabular-nums}
</style></head><body><main>
<h1>Money Radar</h1><p class="muted">Big money on new Stake Engine releases. {{UPDATED}}</p>
{{BODY}}
</main></body></html>
"""


def write(db, cfg, now, html_path, json_path):
    rep = report(db, cfg, max(now, int(time.time())))
    json_path.write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    html_path.write_text(render(rep), encoding="utf-8")
    return rep
