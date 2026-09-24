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
    'injuries':        REL + '/injuries/injuries_{season}.csv',                   # official weekly report: one row per player-week, latest status
    'injuries_meta':   'https://api.github.com/repos/nflverse/nflverse-data/releases/tags/injuries',  # when that file was last updated
    'nfl_players':     REL + '/players/players.csv.gz',                           # gsis_id <-> pfr_id, names, teams
    'id_map':          'https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_playerids.csv',  # sleeper_id <-> gsis_id
    'sleeper_players': 'https://api.sleeper.app/v1/players/nfl',
}
TEAM_FIX = {'LA': 'LAR'}           # nflverse abbreviation -> Sleeper abbreviation
SKILL = ('QB', 'RB', 'WR', 'TE')   # positions that get snap / share / depth data
INJURY_POS = SKILL + ('K',)        # positions that get injury / practice data
# players.json: the Sleeper player fields the app uses (keep in sync with PLAYER_FIELDS in index.html). About 260 KB
# gzipped instead of the 2.6 MB / 14.7 MB raw /players/nfl, so the app can re-check it on every open.
PLAYER_FIELDS = ['full_name', 'first_name', 'last_name', 'position', 'fantasy_positions', 'team', 'injury_status',
                 'injury_body_part', 'injury_notes', 'injury_start_date', 'practice_participation', 'practice_description']
# Sleeper stat/projection keys that never score (the app scores with the league's own settings).
NON_SCORING = re.compile(r'^(pts_|pos_rank|rank_|adp_|pos_adp|gp$|gs$|gms_active$|tm_|off_snp$|def_snp$|st_snp$|cmp_pct$|.*_(ypa|ypc|ypr|ypt|pct|rtg|lng|avg)$)')
DVP_POS = ('QB', 'RB', 'WR', 'TE', 'K')
ROS_POS = ('QB', 'RB', 'WR', 'TE', 'K', 'DEF')
LAST_FANTASY_WEEK = 17   # rest-of-season sums run through the usual fantasy championship week
PRACTICE = {'Did Not Participate In Practice': 'DNP', 'Limited Participation in Practice': 'LP', 'Full Participation in Practice': 'FP'}


def default_season():
    now = datetime.date.today()
    return now.year - 1 if now.month <= 2 else now.year


def get(url):
    headers = {'User-Agent': 'sleepa build_nflverse.py'}
    if url.startswith('https://api.github.com/') and os.environ.get('GITHUB_TOKEN'):
        headers['Authorization'] = 'Bearer ' + os.environ['GITHUB_TOKEN']   # in the Action: avoids the anonymous rate limit
    req = urllib.request.Request(url, headers=headers)
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


def us_eastern(dt_utc):
    """UTC datetime -> naive US Eastern (DST: 2nd Sunday of March to 1st Sunday of November). No tzdata needed."""
    y = dt_utc.year
    def nth_sunday(month, n):
        d = datetime.date(y, month, 1)
        return d + datetime.timedelta(days=(6 - d.weekday()) % 7 + 7 * (n - 1))
    start = datetime.datetime.combine(nth_sunday(3, 2), datetime.time(7))   # 2am EST = 07:00 UTC
    end = datetime.datetime.combine(nth_sunday(11, 1), datetime.time(6))    # 2am EDT = 06:00 UTC
    naive = dt_utc.replace(tzinfo=None)
    return naive - datetime.timedelta(hours=4 if start <= naive < end else 5)


def eastern_to_utc(naive_et):
    """Naive US Eastern datetime -> UTC string 'YYYY-MM-DDTHH:MM:00Z' (DST from 2am on the 2nd Sunday of March
    to 2am on the 1st Sunday of November, local time)."""
    y = naive_et.year
    def nth_sunday(month, n):
        d = datetime.date(y, month, 1)
        return d + datetime.timedelta(days=(6 - d.weekday()) % 7 + 7 * (n - 1))
    dst = datetime.datetime.combine(nth_sunday(3, 2), datetime.time(2)) <= naive_et < datetime.datetime.combine(nth_sunday(11, 1), datetime.time(2))
    return (naive_et + datetime.timedelta(hours=4 if dst else 5)).strftime('%Y-%m-%dT%H:%M:00Z')


def practice_days(game_date):
    """The three practice-report days before a game: Wed/Thu/Fri for Sunday, Thu/Fri/Sat for Monday,
    Mon/Tue/Wed for Thursday (short week), Wed/Thu/Fri for Saturday. Mirrored in index.html (practiceDays)."""
    back = (3, 2, 1) if game_date.weekday() in (3, 5) else (4, 3, 2)   # Thu / Sat games
    return [game_date - datetime.timedelta(days=b) for b in back]


def sleeper_weekly(kind, season, week, positions):
    """Sleeper's weekly stats or projections (unofficial endpoint the app already uses): a list of
    {player_id, team, opponent, player: {position}, stats}."""
    q = '&'.join('position[]=' + x for x in positions)
    return json.loads(get(f'https://api.sleeper.app/{kind}/nfl/{season}/{week}?season_type=regular&{q}'))


def scoring_stats(stats):
    return {k: v for k, v in (stats or {}).items() if isinstance(v, (int, float)) and v and not NON_SCORING.match(k)}


def build_dvp(season, through_week, totals=None):
    """Raw stats each defense has allowed to each position, summed over completed weeks, plus games played.
    Scoring is linear (stat x weight), so the app can score these sums with any league's settings.
    If totals is given, it is filled with each player's own season sums: {pid: {'g': games, 's': stats}}."""
    dvp = {}
    for week in range(1, through_week + 1):
        print(f'  sleeper stats week {week} (defense vs position, season totals)', file=sys.stderr)
        for e in sleeper_weekly('stats', season, week, DVP_POS + ('DEF',)):
            pos, opp = (e.get('player') or {}).get('position'), team(e.get('opponent') or '')
            st = scoring_stats(e.get('stats'))
            if not st:
                continue
            if totals is not None:
                t = totals.setdefault(str(e['player_id']), {'g': 0, 's': {}})
                t['g'] += 1
                for k, v in st.items():
                    t['s'][k] = round(t['s'].get(k, 0) + v, 2)
            if pos not in DVP_POS or not opp:
                continue
            d = dvp.setdefault(opp, {}).setdefault(pos, {'g': [], 's': {}})
            if week not in d['g']:
                d['g'].append(week)
            for k, v in st.items():
                d['s'][k] = round(d['s'].get(k, 0) + v, 3)
    for opp in dvp.values():
        for d in opp.values():
            d['g'] = len(d['g'])
    return dvp


def build_ros(season, from_week, last_week=LAST_FANTASY_WEEK):
    """Each player's projected raw stats summed from from_week through last_week. Bye weeks have empty
    projections, so they add nothing. 'w' = weeks with a projection."""
    ros = {}
    for week in range(from_week, last_week + 1):
        print(f'  sleeper projections week {week} (rest of season)', file=sys.stderr)
        for e in sleeper_weekly('projections', season, week, ROS_POS):
            st = scoring_stats(e.get('stats'))
            if not st:
                continue
            r = ros.setdefault(str(e['player_id']), {'w': 0, 's': {}})
            r['w'] += 1
            for k, v in st.items():
                r['s'][k] = round(r['s'].get(k, 0) + v, 2)
    # keep fantasy-relevant players only (more than a token projection)
    return {pid: r for pid, r in ros.items() if r['s'].get('rec', 0) + r['s'].get('rush_yd', 0) + r['s'].get('pass_yd', 0) + r['s'].get('fgm', 0) + r['s'].get('xpm', 0) + r['w'] * (1 if pid.isalpha() else 0) > 1}


def load_previous(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def build(season, previous=None):
    print(f'Building nflverse.json for {season}', file=sys.stderr)

    # --- schedule: opponent per team per regular-season week (a missing week = bye) ---
    sched, max_week, game_dates, kick = {}, 0, {}, {}
    for r in csv_rows(SOURCES['schedule']):
        if r['season'] != str(season) or r['game_type'] != 'REG':
            continue
        w, home, away = r['week'], team(r['home_team']), team(r['away_team'])
        sched.setdefault(home, {})[w] = 'vs ' + away
        sched.setdefault(away, {})[w] = '@ ' + home
        if r.get('gameday'):
            gd = datetime.date.fromisoformat(r['gameday'])
            game_dates.setdefault(home, {})[int(w)] = gd
            game_dates.setdefault(away, {})[int(w)] = gd
            if r.get('gametime'):   # kickoff, ET -> UTC; the app treats a player as locked once his team has kicked off
                hh, mm = (int(x) for x in r['gametime'].split(':')[:2])
                k = eastern_to_utc(datetime.datetime.combine(gd, datetime.time(hh, mm)))
                kick.setdefault(home, {})[w] = k
                kick.setdefault(away, {})[w] = k
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

    relevant = {sid: p for sid, p in sleeper.items() if p.get('position') in INJURY_POS and p.get('team')}
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

    # --- injuries: official report for each team's upcoming game, plus a by-day practice log ---
    # nflverse keeps one row per player-week holding the *latest* practice status, with no dates. To show
    # DNP / Limited / Full by day, each build records the current status under the practice day it belongs to
    # and keeps the days recorded by earlier builds (read back from the previous nflverse.json).
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    today_et = us_eastern(now_utc).date()
    try:
        meta = json.loads(get(SOURCES['injuries_meta']))
        asset = next(a for a in meta.get('assets', []) if a.get('name') == f'injuries_{season}.csv')
        inj_updated = datetime.datetime.fromisoformat(asset['updated_at'].replace('Z', '+00:00'))
    except Exception as e:  # rate limit, network: fall back to "now"
        print('  (could not read injuries update time:', e, ')', file=sys.stderr)
        inj_updated = now_utc
    inj_updated_et = us_eastern(inj_updated)
    upcoming = {}  # team -> (week, game date): the next game on or after today
    for t, weeks in game_dates.items():
        nxt = [(w, d) for w, d in weeks.items() if d >= today_et]
        if nxt:
            upcoming[t] = min(nxt, key=lambda x: x[1])
    sid_of = {g: sid for sid, g in gsis_of.items()}
    prev = previous or {}
    prev_inj = prev.get('inj', {}) if prev.get('season') == season else {}
    inj = {}
    try:
        inj_rows = list(csv_rows(SOURCES['injuries'].format(season=season)))
    except Exception as e:
        print('  (injuries unavailable:', e, ')', file=sys.stderr)
        inj_rows = []
    for r in inj_rows:
        sid, t = sid_of.get(r.get('gsis_id')), team(r.get('team', ''))
        if not sid or t not in upcoming or r.get('season_type') != 'REG' or int(r['week']) != upcoming[t][0]:
            continue
        week, gd = upcoming[t]
        days = practice_days(gd)
        # the practice day this status belongs to: the latest one whose report (~4pm ET) was out before
        # nflverse last updated the file; before the first report of the week, the first day
        reported = [d for d in days if datetime.datetime.combine(d, datetime.time(16)) <= inj_updated_et]
        day = (reported[-1] if reported else days[0]).isoformat()
        old = prev_inj.get(sid, {})
        log = dict(old.get('p', {})) if old.get('w') == week else {}
        code = PRACTICE.get(r.get('practice_status', ''))
        if code:
            log[day] = code
        e = {'w': week, 'g': gd.isoformat()}
        if r.get('report_status'):
            e['st'] = r['report_status']
        body = r.get('report_primary_injury') or r.get('practice_primary_injury')
        body2 = r.get('report_secondary_injury') or r.get('practice_secondary_injury')
        if body:
            e['b'] = body + (', ' + body2 if body2 and body2 != body else '')
        if log:
            e['p'] = dict(sorted(log.items()))
        inj[sid] = e

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
        'kick': kick,
        'players': players,
        'inj': inj,
        'inj_as_of': inj_updated.strftime('%Y-%m-%dT%H:%MZ'),
        'coverage': dict(method, relevant=len(relevant), snap_rows_unmapped=snap_unmapped),
    }
    slim = {sid: {f: p[f] for f in PLAYER_FIELDS if p.get(f) is not None} for sid, p in sleeper.items()}
    return out, slim


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--season', type=int, default=default_season())
    ap.add_argument('--out', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'nflverse.json'))
    args = ap.parse_args()

    old = load_previous(args.out)
    out, slim = build(args.season, old)

    # Defense vs position (completed weeks) goes in nflverse.json; rest-of-season projections in their own file,
    # which the app only loads for the waiver screen and the trade helper.
    state = json.loads(get('https://api.sleeper.app/v1/state/nfl'))
    cur_week = int(state.get('week') or 1) if str(state.get('season')) == str(args.season) else LAST_FANTASY_WEEK + 1
    totals = {}
    try:
        out['dvp'] = build_dvp(args.season, min(cur_week - 1, 18), totals)
        out['dvp_through'] = min(cur_week - 1, 18)
    except Exception as e:
        print('  (defense vs position unavailable:', e, ')', file=sys.stderr)
    # season.json: every player's season stat totals + games, so the Players tab can filter/sort all players by
    # season average without fetching each player's history.
    season_path = os.path.join(os.path.dirname(os.path.abspath(args.out)), 'season.json')
    old_season = load_previous(season_path)
    if totals and (old_season.get('players') != totals or old_season.get('through_week') != cur_week - 1):
        with open(season_path, 'w', encoding='utf-8') as f:
            json.dump({'season': args.season, 'generated': out['generated'], 'through_week': cur_week - 1, 'players': totals}, f, separators=(',', ':'))
        print(f'Wrote {season_path} ({os.path.getsize(season_path):,} bytes, {len(totals):,} players).', file=sys.stderr)
    ros_path = os.path.join(os.path.dirname(os.path.abspath(args.out)), 'ros.json')
    try:
        ros = build_ros(args.season, cur_week) if cur_week <= LAST_FANTASY_WEEK else {}
        old_ros = load_previous(ros_path)
        if old_ros.get('players') != ros or old_ros.get('from_week') != cur_week:
            with open(ros_path, 'w', encoding='utf-8') as f:
                json.dump({'season': args.season, 'generated': out['generated'], 'from_week': cur_week, 'through_week': LAST_FANTASY_WEEK, 'players': ros}, f, separators=(',', ':'))
            print(f'Wrote {ros_path} ({os.path.getsize(ros_path):,} bytes, {len(ros):,} players, weeks {cur_week}-{LAST_FANTASY_WEEK}).', file=sys.stderr)
        else:
            print('No projection changes; ros.json left as is.', file=sys.stderr)
    except Exception as e:
        print('  (rest-of-season projections unavailable:', e, ')', file=sys.stderr)

    # players.json (Sleeper player database, trimmed): rewritten only when a player's fields changed.
    players_path = os.path.join(os.path.dirname(os.path.abspath(args.out)), 'players.json')
    old_players = load_previous(players_path)
    if old_players.get('players') != slim:
        with open(players_path, 'w', encoding='utf-8') as f:
            json.dump({'generated': out['generated'], 'players': slim}, f, separators=(',', ':'))
        print(f'Wrote {players_path} ({os.path.getsize(players_path):,} bytes, {len(slim):,} players).', file=sys.stderr)
    else:
        print('No player changes; players.json left as is.', file=sys.stderr)

    # Skip rewriting when only the timestamp would change (keeps scheduled commits quiet).
    if old and {k: v for k, v in old.items() if k != 'generated'} == {k: v for k, v in out.items() if k != 'generated'}:
        print('No data changes; nflverse.json left as is.', file=sys.stderr)
        return

    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump(out, f, separators=(',', ':'))
    c = out['coverage']
    print(f"Wrote {args.out} ({os.path.getsize(args.out):,} bytes). Stats through week {out['stats_through_week']}, "
          f"depth chart as of {out['depth_as_of']}, {len(out['inj'])} players on this week's injury reports.", file=sys.stderr)
    print(f"ID matches for {c['relevant']} Sleeper QB/RB/WR/TE on a team: {c['id_map']} via ID map, "
          f"{c['sleeper_gsis']} via Sleeper gsis_id, {c['name_team']} via name+team, {c['unmatched']} unmatched.", file=sys.stderr)


if __name__ == '__main__':
    main()
