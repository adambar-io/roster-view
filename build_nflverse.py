#!/usr/bin/env python3
"""
Builds nflverse.json for sleepa (index.html).

Why this exists: nflverse publishes its data as GitHub release files, and browsers can't fetch those
directly (GitHub doesn't send CORS headers for release downloads). So this script downloads them,
joins everything to Sleeper player IDs, keeps only the current season and the columns the app uses,
and writes one small JSON file that sits next to the HTML.

Usage:   python build_nflverse.py                 (season defaults to the current NFL season)
         python build_nflverse.py --season 2026
Re-run it whenever you want fresher data (nflverse updates daily in season). Standard library only.
"""
import argparse, csv, datetime, gzip, io, json, os, re, sys, unicodedata, urllib.request

REL = 'https://github.com/nflverse/nflverse-data/releases/download'
SOURCES = {
    'schedule':        REL + '/schedules/games.csv.gz',
    'stats':           REL + '/stats_player/stats_player_week_{season}.csv.gz',   # target_share, air_yards_share (keyed by gsis_id)
    'snaps':           REL + '/snap_counts/snap_counts_{season}.csv.gz',          # offense_pct (keyed by pfr_player_id)
    'depth':           REL + '/depth_charts/depth_charts_{season}.csv.gz',        # pos_abb + pos_rank (keyed by gsis_id), many daily snapshots
    'nfl_players':     REL + '/players/players.csv.gz',                           # gsis_id <-> pfr_id, names, teams
    'id_map':          'https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_playerids.csv',  # sleeper_id <-> gsis_id
    'sleeper_players': 'https://api.sleeper.app/v1/players/nfl',
}
TEAM_FIX = {'LA': 'LAR'}           # nflverse abbreviation -> Sleeper abbreviation
SKILL = ('QB', 'RB', 'WR', 'TE')   # positions that get snap / share / depth data


def default_season():
    now = datetime.date.today()
    return now.year - 1 if now.month <= 2 else now.year


def get(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'sleepa build_nflverse.py'})
    with urllib.request.urlopen(req, timeout=180) as r:
        data = r.read()
    return gzip.decompress(data) if url.endswith('.gz') else data


def csv_rows(url):
    print('  downloading', url.rsplit('/', 1)[-1], file=sys.stderr)
    return csv.DictReader(io.StringIO(get(url).decode('utf-8')))


def num(v):
    try:
        x = float(v)
        return None if x != x else x  # NaN -> None
    except (TypeError, ValueError):
        return None


def team(t):
    return TEAM_FIX.get(t, t)


def norm_name(s):
    s = unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode().lower()
    s = re.sub(r"[.'\-]", '', s)
    s = re.sub(r'\b(jr|sr|ii|iii|iv|v)\b', '', s)
    return re.sub(r'\s+', ' ', s).strip()


def build(season):
    print(f'Building nflverse.json for {season}', file=sys.stderr)

    # --- schedule: opponent per team per regular-season week (a missing week = bye) ---
    sched, max_week = {}, 0
    for r in csv_rows(SOURCES['schedule']):
        if r['season'] != str(season) or r['game_type'] != 'REG':
            continue
        w, home, away = r['week'], team(r['home_team']), team(r['away_team'])
        sched.setdefault(home, {})[w] = 'vs ' + away
        sched.setdefault(away, {})[w] = '@ ' + home
        max_week = max(max_week, int(w))
    if not sched:
        sys.exit(f'No {season} regular-season games in the schedule — is the season right?')

    # --- ID mapping: Sleeper -> gsis (dynastyprocess map, then Sleeper's own gsis_id, then unique name+team) ---
    print('  downloading Sleeper players', file=sys.stderr)
    sleeper = json.loads(get(SOURCES['sleeper_players']))
    dp_sleeper_gsis, pfr_gsis = {}, {}
    for r in csv_rows(SOURCES['id_map']):
        sid, gsis, pfr = r.get('sleeper_id', ''), r.get('gsis_id', ''), r.get('pfr_id', '')
        if gsis in ('', 'NA'):
            continue
        if sid not in ('', 'NA'):
            dp_sleeper_gsis[sid] = gsis
        if pfr not in ('', 'NA'):
            pfr_gsis[pfr] = gsis
    by_name_team = {}
    for r in csv_rows(SOURCES['nfl_players']):
        gsis = r.get('gsis_id', '')
        if not gsis:
            continue
        if r.get('pfr_id'):
            pfr_gsis.setdefault(r['pfr_id'], gsis)
        key = (norm_name(r.get('display_name')), team(r.get('latest_team', '')))
        by_name_team.setdefault(key, set()).add(gsis)

    relevant = {sid: p for sid, p in sleeper.items() if p.get('position') in SKILL and p.get('team')}
    gsis_of, method = {}, {'id_map': 0, 'sleeper_gsis': 0, 'name_team': 0, 'unmatched': 0}
    for sid, p in relevant.items():
        g = dp_sleeper_gsis.get(sid)
        if g:
            method['id_map'] += 1
        else:
            g = (p.get('gsis_id') or '').strip()   # Sleeper's own field: sparse, sometimes has stray spaces
            if g:
                method['sleeper_gsis'] += 1
            else:
                cands = by_name_team.get((norm_name(p.get('full_name')), p.get('team')), set())
                g = next(iter(cands)) if len(cands) == 1 else None
                method['name_team' if g else 'unmatched'] += 1
        if g:
            gsis_of[sid] = g

    weekly = {}  # gsis -> week -> {s, t, a}
    def put(gsis, week, k, v):
        if v is not None:
            weekly.setdefault(gsis, {}).setdefault(str(int(week)), {})[k] = round(v, 3)

    # --- target share / air yards share (weekly player stats) ---
    for r in csv_rows(SOURCES['stats'].format(season=season)):
        if r.get('season_type') != 'REG':
            continue
        put(r['player_id'], r['week'], 't', num(r.get('target_share')))
        put(r['player_id'], r['week'], 'a', num(r.get('air_yards_share')))
    stats_through = max((int(w) for d in weekly.values() for w in d), default=0)

    # --- offensive snap share (keyed by PFR id, mapped to gsis) ---
    snap_unmapped = 0
    for r in csv_rows(SOURCES['snaps'].format(season=season)):
        if r.get('game_type') != 'REG' or r.get('position') not in SKILL:
            continue
        g = pfr_gsis.get(r.get('pfr_player_id'))
        if not g:
            snap_unmapped += 1
            continue
        put(g, r['week'], 's', num(r.get('offense_pct')))

    # --- depth chart: latest snapshot per team, offense skill positions, label = pos_abb + pos_rank ---
    latest = {}  # team -> (dt, rows)
    for r in csv_rows(SOURCES['depth'].format(season=season)):
        if r.get('pos_abb') not in SKILL or not r.get('gsis_id'):
            continue
        t, dt = team(r['team']), r['dt']
        cur = latest.get(t)
        if cur is None or dt > cur[0]:
            latest[t] = (dt, [r])
        elif dt == cur[0]:
            cur[1].append(r)
    depth, depth_as_of = {}, ''
    for t, (dt, rows) in latest.items():
        depth_as_of = max(depth_as_of, dt)
        for r in rows:
            rank = int(num(r['pos_rank']) or 99)
            prev = depth.get(r['gsis_id'])
            if prev is None or rank < prev[1]:
                depth[r['gsis_id']] = (r['pos_abb'], rank)

    # --- output keyed by Sleeper ID. Every matched player gets an entry (even if empty),
    #     so the app can tell "no ID match" (missing key) from "matched but no data yet". ---
    players = {}
    for sid, g in gsis_of.items():
        e = {}
        if g in depth:
            e['d'] = depth[g][0] + str(depth[g][1])
        if g in weekly:
            e['w'] = weekly[g]
        players[sid] = e

    out = {
        'season': season,
        'generated': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%MZ'),
        'stats_through_week': stats_through,
        'depth_as_of': depth_as_of,
        'sched_weeks': max_week,
        'sched': sched,
        'players': players,
        'coverage': dict(method, relevant=len(relevant), snap_rows_unmapped=snap_unmapped),
    }
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--season', type=int, default=default_season())
    ap.add_argument('--out', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'nflverse.json'))
    args = ap.parse_args()

    out = build(args.season)

    # Skip rewriting when only the timestamp would change (keeps scheduled commits quiet).
    try:
        with open(args.out, encoding='utf-8') as f:
            old = json.load(f)
        if {k: v for k, v in old.items() if k != 'generated'} == {k: v for k, v in out.items() if k != 'generated'}:
            print('No data changes; nflverse.json left as is.', file=sys.stderr)
            return
    except (OSError, ValueError):
        pass

    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump(out, f, separators=(',', ':'))
    c = out['coverage']
    print(f"Wrote {args.out} ({os.path.getsize(args.out):,} bytes). Stats through week {out['stats_through_week']}, "
          f"depth chart as of {out['depth_as_of']}.", file=sys.stderr)
    print(f"ID matches for {c['relevant']} Sleeper QB/RB/WR/TE on a team: {c['id_map']} via ID map, "
          f"{c['sleeper_gsis']} via Sleeper gsis_id, {c['name_team']} via name+team, {c['unmatched']} unmatched.", file=sys.stderr)


if __name__ == '__main__':
    main()
