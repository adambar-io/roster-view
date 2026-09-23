# Sleeper Roster View — Project Context

A single-file local web app for viewing a Sleeper fantasy football roster with clearer stats/projections and a local (non-syncing) lineup planner. Built iteratively in Claude chat; handing off to Claude Code from here.

**Give Claude Code both this file and `sleeper-roster-view.html`** — this file explains the *why* and the gotchas; the HTML file is the current, working implementation.

## What it does today

- Looks up a Sleeper username → resolves to `user_id` → lists their leagues → loads one league's roster
- Shows starters (in the league's real slot order) and bench, each with photo, position, team, injury status
- Position-based color accents (QB blue, RB green, WR gold, TE purple, K grey, DEF cyan)
- Projected points for the current week, computed against **the league's own `scoring_settings`** (not a generic PPR/standard default — this matters for leagues with custom scoring)
- Season-total key stats shown inline per position (e.g. RBs show rush yd/TD/rec, WRs show rec/yd/TD/targets) — no dropdown, always visible
- Click any player → large focus panel: big photo/name, proj/last-game/season-avg/season-total, full raw stat breakdown, and swap candidates for that slot: the slot's current occupant (pinned on top, labeled "Starting", no swap button) plus **bench players only** who are eligible for the slot (including FLEX-type slots), sorted by projection. Other starters are deliberately excluded even if eligible. Clicking a bench player targets the lowest-projected slot they're eligible for
- **Local lineup builder**: "Swap in" on any candidate card updates a draft lineup (not pushed to Sleeper — there's no public write endpoint for this). A comparison bar shows current-lineup-proj vs new-lineup-proj vs the difference, with a Reset button
- **Compare Lineups view** (button appears alongside Reset once the draft differs): side-by-side Original vs New per starting slot in roster order, changed slots highlighted green in both columns, totals + signed/colored difference, Reset and Done
- **Available Players tab** (informational only, not part of the lineup builder): free agents = every QB/RB/WR/TE/K/DEF with a current NFL team who isn't on *any* roster in the league (union of all `rosters[].players`, computed once per load in `computeFreeAgents`). Position pills, sorted by projection, capped at `FA_LIST_LIMIT` (15) with a "Show more" button. Each row shows "+X vs your [lowest-projected rostered player at that position]" — players with no projection (byes/IR) are skipped when picking that comparison player. No extra projections fetch (the bulk projections call already covers free agents); a free agent's season history is fetched only when you open them
- **Player Analysis screen** (`openAnalysis` / `renderAnalysis`): tap a player's **name or photo** anywhere (roster rows, focus panel hero + cards, compare view, waiver list) → full-screen game log for that player. It's a separate overlay that can stack on top of the focus panel, and Escape closes the top one first. Tap targets are marked with `data-analyze="<pid>"` and handled by one capture-phase document click listener, so they win over the row's own click (which still opens the focus panel). Game log: one row per completed week, Wk (pinned when scrolling sideways) · Opp (`vs`/`@` from Sleeper's history entries) · Pts (league scoring) · every stat that's non-zero at least once, labeled via `statLabel`, ordered by `STAT_LABELS` then alphabetically. Total + Per-game rows: counts sum; `*_lng` takes the max; per-play rates (`*_ypr`, `*_ypt`, `*_pct`…) show no total and average the weekly values (`statKind`). Sleeper's generic `pts_*`, ranks and ADP are excluded (`NON_STAT_KEY`). History is loaded on demand via `ensureHistory` — never prefetched for free agents
- Slot eligibility is keyed off the **slot** label (`groupForSlot`), never the player's position — getting this wrong makes every non-FLEX slot look eligible for everyone
- Player database and per-player season stats are cached in `localStorage` to cut down on repeat requests. The player DB is trimmed to the fields we render (`PLAYER_FIELDS`, cache key `v2`) because the full `/players/nfl` dump is bigger than iOS Safari's ~5MB localStorage quota
- **Mobile / iPhone layout** (≤560px): tighter roster rows with the column header hidden, a full-screen focus sheet with a pinned close button and background scroll lock, 2-row candidate cards that keep Proj/Last/Avg visible, and short injury chips (Q/O/IR). It also handles safe areas for the notch and home bar, uses 16px inputs so iOS doesn't zoom, gates hover styles behind `(hover: hover)`, and turns off auto-capitalize on the username field. Tested down to 320px wide

- **nflverse signals** (from `nflverse.json`, see below):
  - **Opponent / bye**: "vs KC" / "@ KC" / "BYE" on every player row (roster, waiver list, focus cards, Compare view) and in the Analysis header. A **starter on bye** gets a red row + red slot tag + "BYE" chip, and a red banner above the tabs lists every bye starter in the draft lineup
  - **Snap share**: roster + waiver rows show the latest offensive snap % with an arrow (↑/↓ = moved ≥5 points vs the average of the previous two weeks, → otherwise; hover shows the weekly values). Analysis game log has a per-week "Snap %" column with a season average
  - **Target share / air-yards share** (RB/WR/TE): per-week columns + season average in the game log; season averages on waiver rows. Air-yards share can be negative for RBs (targets behind the line) — that's real data
  - **Depth chart** ("RB2" chip) on roster + waiver rows, from the latest nflverse snapshot
  - Analysis game log also inserts **BYE rows** for byes already played (based on the player's *current* team)

## Architecture

One HTML file (vanilla JS, no build step, no framework), inline CSS, Google Fonts (Inter) loaded via `<link>`. No backend — Sleeper's API is called directly from the browser. The one companion is **`nflverse.json`**, a static data file built by **`build_nflverse.py`** and served next to the HTML (see "nflverse" below for why it can't be fetched from the browser directly).

Files:
- `sleeper-roster-view.html` — the app
- `build_nflverse.py` — Python 3, standard library only. `python build_nflverse.py [--season 2026]` → writes `nflverse.json` (~50 KB in week 3). Skips rewriting if only the timestamp would change
- `nflverse.json` — generated; commit/deploy it alongside the HTML
- `.github/workflows/update-nflverse.yml` — daily GitHub Action (14:00 UTC + manual "Run workflow" button) that re-runs the script and commits the JSON as `github-actions[bot]`

**Hosting (live since Sept 23, 2026):** GitHub Pages from `adambar-io/roster-view`, branch `main`, root. App: https://adambar-io.github.io/roster-view/sleeper-roster-view.html (the bare `/roster-view/` URL 404s unless the HTML is renamed to `index.html`). Verified end to end: the Action's bot commit automatically triggers a "pages build and deployment" run, so the refreshed `nflverse.json` goes live with no manual step. Uploading through GitHub's web drag-and-drop **skips the `.github` folder** — the workflow file had to be created with "Add file → Create new file". Scheduled Actions are paused by GitHub after 60 days of no repo activity (likely in the off-season); re-enable from the Actions tab.

Design language (Sept 2026 redesign, "style A"): **clean native app first** (Apple Sports / Spotify / Revolut / Notion / Flighty references), a light data-terminal influence for tables, and only subtle broadcast touches. Rules:
- **Tokens only.** All colors are CSS custom properties in `:root`, with a light base and a dark set applied via `prefers-color-scheme` *or* `data-theme="dark"` (keep the two dark blocks identical). No hard-coded hex outside the token blocks. `--muted` is kept as an alias because JS-built markup uses it.
- **Theme toggle**: Auto / Light / Dark (`.theme-seg`, `applyTheme`), saved in `localStorage['rv-theme']`, applied by an inline script in `<head>` before first paint; also rewrites the `theme-color` metas so iOS/Android browser chrome matches.
- **Type**: Inter only (Oswald was removed), `font-variant-numeric: tabular-nums` on every number so columns align. Big Revolut-style numbers for projections/totals; small uppercase letter-spaced labels.
- **Color meaning**: one green accent (active states, positive deltas, "New", Swap in). **Red is reserved for things you must act on** — bye starters, Out/IR/Doubtful, negative deltas. Amber = Questionable only. Position colors are small accents (DEF moved off red to cyan).
- **Shape**: borderless rounded cards (20px), separation by background shade + spacing; pill buttons/chips; primary button = ink pill (text color background).
- **Motion**: 150–350ms eased/spring transitions; everything disabled under `prefers-reduced-motion`.
- Design previews that led here (mobile A/B, desktop two-pane, full-screen expand) were throwaway HTML files, not in the repo. Planned next: desktop sidebar + two-pane layout and mobile bottom tab bar (step 2), roster table/cards + team-color accents + full-screen expand views (step 3), motion polish (step 4).

## Data sources

### Official, documented API (`api.sleeper.app/v1/...`)
Stable, safe to rely on:
- `/user/<username>` → user_id
- `/user/<user_id>/leagues/nfl/<season>` → leagues
- `/league/<league_id>`, `/rosters`, `/users` → league/roster/owner data
- `/players/nfl` → full player dictionary (large, cached 3 days)
- `/state/nfl` → current NFL week

### Images (Sleeper CDN, all via `playerImageUrl`)
- Players: `https://sleepercdn.com/content/nfl/players/<player_id>.jpg`
- Team defenses have no headshot at that path (their "player_id" is the team abbreviation), so they use the team logo: `https://sleepercdn.com/images/team_logos/nfl/<team lowercase>.png`. Verified all 32 teams load. Like the headshots, this is an unofficial CDN path

### Unofficial/undocumented endpoints (`api.sleeper.app/...`, no `/v1/`)
**Not documented by Sleeper, no uptime/shape guarantee, could change without notice.** Reverse-engineered from community write-ups. Two gotchas that already bit us once — don't reintroduce them:
1. `season_type` is a **query parameter** (`?season_type=regular`), never a URL path segment
2. Projections come back as a **JSON array** of per-player entries (not an object keyed by player_id); the per-player history endpoint is the opposite — see Gotcha #3 below. Both are normalized

Endpoints in use:
- Projections (current week, bulk): `GET /projections/nfl/<season>/<week>?season_type=regular&position[]=QB&position[]=RB&position[]=WR&position[]=TE&position[]=K&position[]=DEF`
- Season history (one call per roster player — much cheaper than looping the bulk endpoint per week): `GET /stats/nfl/player/<player_id>?season_type=regular&season=<season>&grouping=week` → array of that player's weekly actuals for the season

Both are normalized client-side: raw stat categories may come back nested under `.stats` or flat on the entry — the code checks `entry.stats || entry` and filters to `typeof === 'number'` before summing, since flat responses can include string metadata fields (team, opponent, date) that would otherwise get concatenated into the totals.

**Gotcha #3 (fixed Sept 2026):** the season-history endpoint actually returns an **object keyed by week** (`{"1": {...}, "2": {...}, "3": null, …}`), not an array — projections *are* an array. The old array-only parser silently produced empty histories for every real player (Last gm / season stats all "—"). `normalizeHistory` accepts both shapes; the cache prefix was bumped to `sleeper_stats_v2_` to drop the cached empties. Each week's entry also carries `opponent`, `is_away_team`, and rich stats including `off_snp` / `tm_off_snp` (snap counts) and `rec_air_yd`, plus Sleeper's generic `pts_*` and `pos_rank_*` fields (excluded from stat displays).

### nflverse (via `build_nflverse.py` → `nflverse.json`)
**Why a build script:** nflverse data lives in GitHub *release assets*. Browsers can't fetch them — `github.com/.../releases/download/...` redirects to a storage host without CORS headers ("Failed to fetch"), and the GitHub API asset route fails the same way (and is capped at 60 req/hour unauthenticated). Verified Sept 2026. The depth-chart file is also 52 MB (194 daily snapshots), far too big for a phone.

Sources (all `https://github.com/nflverse/nflverse-data/releases/download/<tag>/<file>`, `.csv.gz` versions):
| Signal | Release tag / file | Columns used | Keyed by |
|---|---|---|---|
| Opponent, bye | `schedules/games.csv.gz` | `season, game_type=REG, week, home_team, away_team` | team (a team with no game in a week = bye) |
| Target / air-yards share | `stats_player/stats_player_week_<season>.csv.gz` | `week, season_type=REG, target_share, air_yards_share` | `player_id` (= gsis_id) |
| Snap share | `snap_counts/snap_counts_<season>.csv.gz` | `week, game_type=REG, position, offense_pct` | `pfr_player_id` (mapped to gsis) |
| Depth chart | `depth_charts/depth_charts_<season>.csv.gz` | `dt, team, gsis_id, pos_abb, pos_rank` — latest `dt` per team only; label = `pos_abb + pos_rank` (e.g. WR1) | `gsis_id` |
| pfr↔gsis, names | `players/players.csv.gz` | `gsis_id, pfr_id, display_name, latest_team` | |
| Sleeper↔gsis | `https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_playerids.csv` (the map nflreadr's `load_ff_playerids` uses) | `sleeper_id, gsis_id, pfr_id` | |

**Gotcha:** the older `player_stats` release stopped updating in May 2025 — use `stats_player`. Target share lives in the weekly player stats, not Next Gen Stats.

**Join (done in the script, output keyed by Sleeper ID):** for every Sleeper QB/RB/WR/TE on a team: (1) dynastyprocess `sleeper_id→gsis_id`, (2) Sleeper's own `gsis_id` (only ~20% populated and some have stray spaces — hence last resort), (3) unique normalized name + team match against nflverse `players`. Sept 2026 result: 828 players → 741 / 3 / 64 matched, **20 unmatched** (fringe rookies/practice squad). Snap counts are keyed by PFR id, mapped via `players.pfr_id` + dynastyprocess `pfr_id`. Team abbreviations: nflverse `LA` → Sleeper `LAR` (`TEAM_FIX`).

**`nflverse.json` shape:** `{season, generated, stats_through_week, depth_as_of, sched_weeks, sched: {TEAM: {"<week>": "vs KC"|"@ KC"}}, players: {<sleeper_id>: {d: "WR1", w: {"<week>": {s: snap%, t: tgt share, a: air share}}}}, coverage}`. Every matched player has an entry (possibly `{}`), so a **missing key = no ID match**; the app logs roster and free-agent match counts to the console (`logNflCoverage`).

**In the app:** `loadNflverse` fetches `nflverse.json` (relative URL) in parallel with the roster load and re-renders when it arrives — it never blocks. Cached in **IndexedDB** (`roster-view` DB, `kv` store, key `nflverse`, 6 h TTL; stale copy used if a refresh fails). Sleeper caches stay in localStorage. If the file is missing/blocked/wrong season → console warning, `ctx.nfl = null`, all nflverse values show "—"; roster, projections, lineup builder, waiver list and Analysis work normally (verified with the fetch blocked and cache cleared). Must be served over http(s) — `file://` can't fetch it.

### Deliberately not used
- **FantasyPros** — real projections but paid/restrictive API, not worth it for a personal tool
- **nflverse Next Gen Stats / PFR advanced stats** — not needed for the current signals
- **Pro Football Reference** — no public API; would require scraping, which is against their ToS. Not used, not recommended.

## Scoring math

Every points value (`Proj`, `Last Gm`, `Season Avg`, `Season Total`) is computed client-side via `calcPoints(rawStats, scoringSettings)`, which multiplies each raw stat category the league's `scoring_settings` actually knows about. This means the numbers reflect this specific league's rules, not Sleeper's generic `pts_ppr`/`pts_std` fields.

## Known limitations / open issues

- **No write-back to Sleeper.** The lineup builder is purely local planning — there's no public API to submit a lineup change. Options discussed but not built: (a) browser automation to fill Sleeper's own UI, (b) a browser extension overlaying Sleeper's site so real form submissions still go through Sleeper, (c) reverse-engineering Sleeper's authenticated internal write endpoints (needs session cookie, higher risk of breaking).
- **nflverse data is only as fresh as the last `build_nflverse.py` run.** nflverse itself updates stats/snaps the day after games and depth charts several times a day. Without the GitHub Action, re-run the script weekly (e.g. Tuesday).
- **~2–3% of skill players have no nflverse match** (mostly fringe rookies); they show "—". The console lists which roster players are missing.
- **Bye rows / opponent use the player's *current* team**, so a mid-season trade can mislabel earlier weeks in the game log (Sleeper's own per-game `opponent` is used for played weeks, so only BYE rows are affected).
- **Season share averages are the mean of weekly shares**, not total targets ÷ total team targets (team totals aren't in the JSON). Close, but not identical.
- **Team defense (`DEF`) history**: verified the per-player history endpoint works for team IDs (e.g. `BAL`). DEF/K get no nflverse signals besides opponent/bye.
- **IDP (individual defensive player) leagues untested** — the FLEX_GROUPS mapping includes an `IDP_FLEX` guess but hasn't been exercised against a real IDP league.
- **Season averages** are total ÷ weeks with a Sleeper stat entry. Byes have no entry (null weeks are dropped), so they don't drag the average down; a week where the player was inactive but has an entry would.
- **Undocumented endpoints are the biggest fragility risk.** If projections/season-stats suddenly show "—" everywhere again, check the browser console first (warnings are logged when a response parses to 0 players) before assuming the whole architecture is broken — it's usually a shape or URL-structure mismatch, not total endpoint removal.

## Natural next steps (not yet built)

- Wire up an actual write path for lineups (pick one of the three options above)
- Charts on the Analysis screen (game log is a plain table for now)
- Verify IDP stat handling
- Mobile: a sticky lineup-diff bar at the bottom of the screen while a draft differs from Sleeper (on phones the compare bar scrolls out of view)
- Persist the draft lineup across page reloads (currently resets on refresh — everything is in-memory `ctx` state)
