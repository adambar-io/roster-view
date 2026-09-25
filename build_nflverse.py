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
import argparse, csv, datetime, gzip, hashlib, html, io, json, os, re, sys, threading, time, unicodedata, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

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
    'espn_injuries':   'https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries',   # unofficial; ~8.7 MB, trimmed to espn.json
}
ESPN_TEAM_FIX = {'WSH': 'WAS'}     # ESPN abbreviation -> Sleeper abbreviation
ESPN_SKIP_POS = {'C', 'G', 'T', 'OT', 'OG', 'OL', 'LS', 'P'}   # offensive line, long snapper, punter: never on a fantasy roster
TEAM_FIX = {'LA': 'LAR'}           # nflverse abbreviation -> Sleeper abbreviation
SKILL = ('QB', 'RB', 'WR', 'TE')   # positions that get snap / share / depth data
INJURY_POS = SKILL + ('K',)        # positions that get injury / practice data
# players.json: the Sleeper player fields the app uses (keep in sync with PLAYER_FIELDS in index.html). About 260 KB
# gzipped instead of the 2.6 MB / 14.7 MB raw /players/nfl, so the app can re-check it on every open.
PLAYER_FIELDS = ['full_name', 'first_name', 'last_name', 'position', 'fantasy_positions', 'team', 'injury_status',
                 'injury_body_part', 'injury_notes', 'injury_start_date', 'practice_participation', 'practice_description', 'espn_id']
# Sleeper stat/projection keys that never score (the app scores with the league's own settings).
NON_SCORING = re.compile(r'^(pts_(std|ppr|half_ppr|idp)$|pos_rank|rank_|adp_|pos_adp|gp$|gs$|gms_active$|tm_|off_snp$|def_snp$|st_snp$|cmp_pct$|.*_(ypa|ypc|ypr|ypt|pct|rtg|lng|avg)$)')
DVP_POS = ('QB', 'RB', 'WR', 'TE', 'K')
# weekly.json keeps every stat the app can show or score: counts, bonus buckets and "long" plays. Dropped: Sleeper's own
# points/ranks, games flags, team snap counts and per-attempt rates (the app works rates out from the counts).
WEEKLY_DROP = re.compile(r'^(pts_(std|ppr|half_ppr|idp)$|pos_rank|rank_|adp_|pos_adp|gp$|gs$|gms_active$|tm_|off_snp$|def_snp$|st_snp$|cmp_pct$|fan_pts_allow|.*_(ypa|ypc|ypr|ypt|pct|rtg|avg)$)')
ROS_POS = ('QB', 'RB', 'WR', 'TE', 'K', 'DEF')
LAST_FANTASY_WEEK = 17   # rest-of-season sums run through the usual fantasy championship week
PRACTICE = {'Did Not Participate In Practice': 'DNP', 'Limited Participation in Practice': 'LP', 'Full Participation in Practice': 'FP'}


def default_season():
    now = datetime.date.today()
    return now.year - 1 if now.month <= 2 else now.year


def get(url):
    headers = {'User-Agent': 'sleepa build_nflverse.py'}
    if 'espn.com' in url:   # ESPN answers 403 to the custom agent string above (checked Sept 2026)
        headers['User-Agent'] = 'Mozilla/5.0 (compatible; sleepa/1.0; +https://github.com/adambar-io/sleepa)'
    if url.startswith('https://api.github.com/') and os.environ.get('GITHUB_TOKEN'):
        headers['Authorization'] = 'Bearer ' + os.environ['GITHUB_TOKEN']   # in the Action: avoids the anonymous rate limit
    req = urllib.request.Request(url, headers=headers)
    # GitHub release downloads and Sleeper occasionally answer 5xx or time out; retry a few times before failing.
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                data = r.read()
            break
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            code = getattr(e, 'code', None)
            if attempt == 3 or (code is not None and code < 500 and code != 429):
                raise
            wait = 2 ** attempt * 3
            print(f'  retrying {url.rsplit("/", 1)[-1]} in {wait}s ({e})', file=sys.stderr)
            time.sleep(wait)
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


def compact_num(v):
    return int(v) if float(v).is_integer() else round(v, 2)


def build_dvp(season, through_week, weekly=None):
    """Raw stats each defense has allowed to each position, summed over completed weeks, plus games played.
    Scoring is linear (stat x weight), so the app can score these sums with any league's settings.
    If weekly is given, it is filled with each player's own games: {pid: {week: {t: team, o: opponent, s: stats}}}. Only weeks the player
    actually played (Sleeper's gp > 0): Sleeper also lists active players who never got on the field, with no stats,
    and counting those as 0-point games dragged season averages down."""
    dvp = {}
    for week in range(1, through_week + 1):
        print(f'  sleeper stats week {week} (defense vs position, weekly actuals)', file=sys.stderr)
        for e in sleeper_weekly('stats', season, week, DVP_POS + ('DEF',)):
            pos, opp = (e.get('player') or {}).get('position'), team(e.get('opponent') or '')
            raw = e.get('stats') or {}
            st = scoring_stats(raw)
            if weekly is not None and (raw.get('gp') or 0) > 0:
                keep = {k: compact_num(v) for k, v in raw.items() if isinstance(v, (int, float)) and v and not WEEKLY_DROP.match(k)}
                g = {'t': team(e.get('team') or ''), 'o': opp, 's': keep}   # home/away comes from the schedule in the app
                weekly.setdefault(str(e['player_id']), {})[str(week)] = g
            if not st:
                continue
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
    projections, so they add nothing. 'w' = weeks with a projection, 'k' = those weeks (for the trade helper)."""
    ros = {}
    for week in range(from_week, last_week + 1):
        print(f'  sleeper projections week {week} (rest of season)', file=sys.stderr)
        for e in sleeper_weekly('projections', season, week, ROS_POS):
            st = scoring_stats(e.get('stats'))
            if not st:
                continue
            r = ros.setdefault(str(e['player_id']), {'w': 0, 'k': [], 's': {}})
            r['w'] += 1
            r['k'].append(week)   # which weeks have a projection (byes and injury weeks don't)
            for k, v in st.items():
                r['s'][k] = round(r['s'].get(k, 0) + v, 2)
    # keep fantasy-relevant players only (more than a token projection)
    return {pid: r for pid, r in ros.items() if r['s'].get('rec', 0) + r['s'].get('rush_yd', 0) + r['s'].get('pass_yd', 0) + r['s'].get('fgm', 0) + r['s'].get('xpm', 0) + r['w'] * (1 if pid.isalpha() else 0) > 1}


def build_espn(sleeper):
    """ESPN's unofficial injury list, trimmed to what the app shows and keyed by Sleeper ID.
    Matching: Sleeper's espn_id when present (only ~1/3 of ESPN's entries), otherwise a unique normalized
    name + team match (checked Sept 2026: all fantasy-position entries matched, no disagreements where both apply).
    Anything unmatched is listed, not dropped."""
    d = json.loads(get(SOURCES['espn_injuries']))
    by_espn = {str(p['espn_id']): sid for sid, p in sleeper.items() if p.get('espn_id')}
    by_nt = {}
    for sid, p in sleeper.items():
        if p.get('team'):
            by_nt.setdefault((norm_name(p.get('full_name')), p['team']), []).append(sid)
    players, unmatched = {}, []
    for t in d.get('injuries', []):
        for e in t.get('injuries', []):
            a = e.get('athlete') or {}
            pos = (a.get('position') or {}).get('abbreviation')
            if pos in ESPN_SKIP_POS:
                continue
            href = ((a.get('links') or [{}])[0] or {}).get('href', '')
            m = re.search(r'/id/(\d+)', href)
            eid = m.group(1) if m else None
            tm = ESPN_TEAM_FIX.get((a.get('team') or {}).get('abbreviation'), (a.get('team') or {}).get('abbreviation'))
            sid = by_espn.get(eid) if eid else None
            how = 'espn_id'
            if not sid:
                cands = by_nt.get((norm_name(a.get('displayName')), tm), [])
                sid, how = (cands[0], 'name+team') if len(cands) == 1 else (None, None)
            det = e.get('details') or {}
            rec = {k: v for k, v in {
                'st': e.get('status'), 'date': e.get('date'), 'short': e.get('shortComment'), 'long': e.get('longComment'),
                'type': det.get('type'), 'loc': det.get('location'), 'side': det.get('side'), 'detail': det.get('detail'),
                'ret': det.get('returnDate'), 'fant': (det.get('fantasyStatus') or {}).get('description'),
            }.items() if v and v != 'Not Specified'}
            if sid:
                rec['m'] = how
                players[sid] = rec
            else:   # listed, not dropped
                unmatched.append({'n': a.get('displayName'), 't': tm, 'pos': pos, 'st': e.get('status'), 'espn': eid})
    return {'espn_timestamp': d.get('timestamp'), 'players': players, 'unmatched': unmatched}


# ---------- outbound links on the player page ----------
# NFL.com: https://www.nfl.com/players/<slug>/stats/ . The slug is name-based but NOT predictable (Ja'Marr Chase is
# ja-marr-chase, De'Von Achane is devon-achane, DK Metcalf is d-k-metcalf, Travis Etienne Jr. is travis-etienne) and
# names collide (josh-allen = the Bills QB), and the pages carry no player ID. So each slug is verified: the page's
# name and position must match, plus the team or the birth date (profile page JSON-LD). Verified slugs are kept between builds.
NFL_TEAM_SLUGS = {
    'ARI': 'arizona-cardinals', 'ATL': 'atlanta-falcons', 'BAL': 'baltimore-ravens', 'BUF': 'buffalo-bills',
    'CAR': 'carolina-panthers', 'CHI': 'chicago-bears', 'CIN': 'cincinnati-bengals', 'CLE': 'cleveland-browns',
    'DAL': 'dallas-cowboys', 'DEN': 'denver-broncos', 'DET': 'detroit-lions', 'GB': 'green-bay-packers',
    'HOU': 'houston-texans', 'IND': 'indianapolis-colts', 'JAX': 'jacksonville-jaguars', 'KC': 'kansas-city-chiefs',
    'LV': 'las-vegas-raiders', 'LAC': 'los-angeles-chargers', 'LAR': 'los-angeles-rams', 'MIA': 'miami-dolphins',
    'MIN': 'minnesota-vikings', 'NE': 'new-england-patriots', 'NO': 'new-orleans-saints', 'NYG': 'new-york-giants',
    'NYJ': 'new-york-jets', 'PHI': 'philadelphia-eagles', 'PIT': 'pittsburgh-steelers', 'SF': 'san-francisco-49ers',
    'SEA': 'seattle-seahawks', 'TB': 'tampa-bay-buccaneers', 'TEN': 'tennessee-titans', 'WAS': 'washington-commanders',
}
NFL_UA = 'Mozilla/5.0 (compatible; sleepa/1.0; +https://github.com/adambar-io/sleepa)'
SUFFIX_RE = re.compile(r'\s+(jr|sr|ii|iii|iv|v)\.?$', re.I)


def stathead_key(gsis):
    """stathead.app's player key: 'sh_' + blake2b-80 of 'NFL:<gsis_id>' (their scripts/build-player-crosswalk.py).
    Verified Sept 2026: reproduces all 12,257 keys in their published player-crosswalk.json."""
    return 'sh_' + hashlib.blake2b(('NFL:' + gsis).encode('utf-8'), digest_size=5).hexdigest()


def fetch_once(url):
    """One try, no retry (unknown NFL.com slugs answer 500; retrying those would only waste time)."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': NFL_UA}), timeout=40) as r:
            return r.read().decode('utf-8', 'ignore')
    except Exception:
        return None


def slugify(text):
    t = unicodedata.normalize('NFKD', text or '').encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+', '-', t).strip('-')


def nfl_slug_guesses(name):
    out = []
    names = [name, SUFFIX_RE.sub('', name or '')]
    m = re.match(r'^([A-Z])\.?([A-Z])\.?\s+(.*)$', name or '')   # initials: DK Metcalf -> d-k-metcalf, A.J. Brown -> a-j-brown
    if m:
        names.append(m.group(1) + ' ' + m.group(2) + ' ' + m.group(3))
    for n in names:
        for x in (re.sub(r"[.']", '', n), re.sub(r"[.']", '-', n)):
            sl = slugify(x)
            if sl and sl not in out:
                out.append(sl)
    return out


def resolve_nfl_slug(player, budget):
    """Return a verified NFL.com slug for a Sleeper player dict, or None. budget() -> False when out of fetches."""
    want_name, want_team, want_pos = norm_name(player.get('full_name')), NFL_TEAM_SLUGS.get(player.get('team')), player.get('position')
    want_born = player.get('birth_date')

    def stats_header(slug):
        # older check, for profile pages that render without JSON-LD (e.g. michael-pittman-jr, a broken template):
        # the stats page's title name, header team and position
        if not budget():
            return None
        page = fetch_once(f'https://www.nfl.com/players/{slug}/stats/')
        if not page:
            return False
        t = re.search(r'<title>([^<]*?) Stats Summary \| NFL\.com', page)
        tm = re.search(r'nfl-c-player-header__team[^>]*>\s*<a[^>]*href="/teams/([a-z0-9-]+)/', page)
        ps = re.search(r'nfl-c-player-header__position">\s*([A-Z]+)\s*<', page)
        name_ok = t and norm_name(html.unescape(t.group(1))) == want_name
        pos_ok = ps and (ps.group(1) == want_pos or (want_pos == 'K' and ps.group(1) in ('K', 'PK')))
        return bool(name_ok and tm and tm.group(1) == want_team and pos_ok)

    def matches(slug):
        # The profile page's schema.org JSON-LD carries name, current team, position (not always) and birth date; the
        # stats page's header often has no team (e.g. Josh Jacobs). Name must match, plus two of team / birth date /
        # position, and a listed position must never disagree.
        if not budget():
            return None
        page = fetch_once(f'https://www.nfl.com/players/{slug}/')
        if not page:
            return False                                   # unknown slug (NFL.com answers 500)
        m = re.search(r'<script type="application/ld\+json">(\{"@type":"SportsTeam".*?)</script>', page, re.S)
        try:
            d = json.loads(m.group(1)) if m else None
        except Exception:
            d = None
        if not d:
            return stats_header(slug)
        role = d.get('member') or {}
        person = role.get('member') or {}
        pos = role.get('roleName')
        if norm_name(html.unescape(person.get('name') or '')) != want_name:
            return False
        if pos and not (pos == want_pos or (want_pos == 'K' and pos in ('K', 'PK'))):
            return False
        team_ok = slugify(d.get('name')) == want_team
        born_ok = bool(want_born) and person.get('birthDate') == want_born
        return (team_ok + born_ok + bool(pos)) >= 2

    for slug in nfl_slug_guesses(player.get('full_name')):
        ok = matches(slug)
        if ok is None:
            return None
        if ok:
            return slug
    # Sleeper often drops suffixes NFL.com keeps (Marvin Harrison -> marvin-harrison-jr, Kenneth Walker -> kenneth-walker-iii).
    # (NFL.com's directory ?query= only filters in the browser via JavaScript; fetched server-side it returns a cached,
    # unrelated list, so it can't be used here.)
    if not SUFFIX_RE.search(player.get('full_name') or ''):
        base = slugify(re.sub(r"[.']", '', player.get('full_name') or ''))
        for suffix in ('jr', 'iii', 'ii', 'sr', 'iv', '2', '3'):   # -2/-3: NFL.com's de-dup of shared names (michael-pittman-2)
            ok = matches(base + '-' + suffix)
            if ok is None:
                return None
            if ok:
                return base + '-' + suffix
    return None


def add_player_links(players, sleeper, gsis_of, previous, max_fetches):
    """Adds 'sh' (stathead key) and, when verified, 'nfl' (+ 'nflt' = team it was verified for) to players[sid].
    Unverified players are listed in the returned dict so the app can fall back to NFL.com's search."""
    prev_players = previous.get('players', {}) if previous else {}
    for sid, g in gsis_of.items():
        players.setdefault(sid, {})['sh'] = stathead_key(g)
    todo = []
    for sid in gsis_of:
        p = sleeper.get(sid) or {}
        old = prev_players.get(sid, {})
        if old.get('nfl') and old.get('nflt') == p.get('team'):
            players[sid]['nfl'], players[sid]['nflt'] = old['nfl'], old['nflt']    # verified before, same team
        elif p.get('team') in NFL_TEAM_SLUGS and p.get('full_name'):
            todo.append(sid)
    lock, used = threading.Lock(), [0]

    def budget():
        with lock:
            if used[0] >= max_fetches:
                return False
            used[0] += 1
            return True

    def work(sid):
        return sid, resolve_nfl_slug(sleeper[sid], budget)

    with ThreadPoolExecutor(max_workers=6) as ex:
        for sid, slug in ex.map(work, todo):
            if slug:
                players[sid]['nfl'], players[sid]['nflt'] = slug, sleeper[sid]['team']
    unresolved = [sid for sid in gsis_of if not players.get(sid, {}).get('nfl')]
    print(f'  NFL.com links: {len(gsis_of) - len(unresolved)} verified, {len(unresolved)} not yet ({used[0]} page fetches this run, cap {max_fetches})', file=sys.stderr)
    return unresolved


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

    # Target/air share, snap share and depth charts are each optional: if one nflverse file can't be downloaded
    # (it happens: GitHub answered 500 for snap counts for a whole morning), keep that part from the previous
    # nflverse.json instead of failing the whole build. `stale` lists what was carried forward.
    stale = []

    # --- target share / air yards share (weekly player stats) ---
    try:
        for r in csv_rows(SOURCES['stats'].format(season=season)):
            if r.get('season_type') != 'REG':
                continue
            put(r['player_id'], r['week'], 't', num(r.get('target_share')))
            put(r['player_id'], r['week'], 'a', num(r.get('air_yards_share')))
    except Exception as e:
        print('  (weekly player stats unavailable, keeping previous target/air share:', e, ')', file=sys.stderr)
        stale.append('shares')
    stats_through = max((int(w) for d in weekly.values() for w in d), default=0)

    # --- offensive snap share (keyed by PFR id, mapped to gsis) ---
    snap_unmapped = 0
    try:
        for r in csv_rows(SOURCES['snaps'].format(season=season)):
            if r.get('game_type') != 'REG' or r.get('position') not in SKILL:
                continue
            g = pfr_gsis.get(r.get('pfr_player_id'))
            if not g:
                snap_unmapped += 1
                continue
            put(g, r['week'], 's', num(r.get('offense_pct')))
    except Exception as e:
        print('  (snap counts unavailable, keeping previous snap share:', e, ')', file=sys.stderr)
        stale.append('snaps')

    # --- depth chart: latest snapshot per team, offense skill positions, label = pos_abb + pos_rank ---
    latest = {}  # team -> (dt, rows)
    try:
        for r in csv_rows(SOURCES['depth'].format(season=season)):
            if r.get('pos_abb') not in SKILL or not r.get('gsis_id'):
                continue
            t, dt = team(r['team']), r['dt']
            cur = latest.get(t)
            if cur is None or dt > cur[0]:
                latest[t] = (dt, [r])
            elif dt == cur[0]:
                cur[1].append(r)
    except Exception as e:
        print('  (depth charts unavailable, keeping previous depth chart:', e, ')', file=sys.stderr)
        stale.append('depth')
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
    inj_unmatched = []
    for r in inj_rows:
        sid, t = sid_of.get(r.get('gsis_id')), team(r.get('team', ''))
        if t not in upcoming or r.get('season_type') != 'REG' or int(r['week']) != upcoming[t][0]:
            continue
        if not sid:
            # on this week's report but not matched to a Sleeper QB/RB/WR/TE/K: flag fantasy positions instead of dropping silently
            if r.get('position') in INJURY_POS:
                inj_unmatched.append({'n': r.get('full_name'), 't': t, 'pos': r.get('position'), 'gsis': r.get('gsis_id')})
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
        # the raw columns, unmerged: reported injury, practice injury, latest practice status
        for key, col in (('ri', 'report_primary_injury'), ('ri2', 'report_secondary_injury'), ('pi', 'practice_primary_injury'),
                         ('pi2', 'practice_secondary_injury'), ('ps', 'practice_status')):
            if r.get(col):
                e[key] = r[col]
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
    # carry forward whatever couldn't be downloaded this time
    prev_players = prev.get('players', {}) if prev.get('season') == season else {}
    keys = [k for k, part in (('t', 'shares'), ('a', 'shares'), ('s', 'snaps')) if part in stale]
    for sid, old in prev_players.items():
        e = players.setdefault(sid, {})
        if 'depth' in stale and 'd' in old:
            e['d'] = old['d']
        for wk, vals in (old.get('w') or {}).items():
            for k in keys:
                if k in vals:
                    e.setdefault('w', {}).setdefault(wk, {})[k] = vals[k]
    if 'shares' in stale:
        stats_through = prev.get('stats_through_week', stats_through)
    if 'depth' in stale:
        depth_as_of = prev.get('depth_as_of', depth_as_of)

    # outbound links for the player page (stathead.app key for everyone matched; NFL.com slug when verified)
    nfl_unresolved = add_player_links(players, sleeper, gsis_of, prev, int(os.environ.get('NFL_LINK_BUDGET', '200')))

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
        'inj_unmatched': inj_unmatched,
        'nfl_team_slugs': NFL_TEAM_SLUGS,
        'nfl_unresolved': nfl_unresolved,
        'inj_as_of': inj_updated.strftime('%Y-%m-%dT%H:%MZ'),
        'stale': stale,
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
    weekly = {}
    try:
        out['dvp'] = build_dvp(args.season, min(cur_week - 1, 18), weekly)
        out['dvp_through'] = min(cur_week - 1, 18)
    except Exception as e:
        print('  (defense vs position unavailable:', e, ')', file=sys.stderr)
    # weekly.json: every fantasy player's stats for each completed game (QB/RB/WR/TE/K/DEF), so the app can show last
    # game / season / recent form for anyone (free agents, other teams, defenses) and score it with the league's settings.
    weekly_path = os.path.join(os.path.dirname(os.path.abspath(args.out)), 'weekly.json')
    old_weekly = load_previous(weekly_path)
    if weekly and (old_weekly.get('players') != weekly or old_weekly.get('through_week') != cur_week - 1):
        with open(weekly_path, 'w', encoding='utf-8') as f:
            json.dump({'season': args.season, 'generated': out['generated'], 'through_week': min(cur_week - 1, 18), 'players': weekly}, f, separators=(',', ':'))
        print(f'Wrote {weekly_path} ({os.path.getsize(weekly_path):,} bytes, {len(weekly):,} players).', file=sys.stderr)
    # espn.json: ESPN injuries (unofficial). On failure the previous file stays, so the app shows the last good copy.
    espn_path = os.path.join(os.path.dirname(os.path.abspath(args.out)), 'espn.json')
    try:
        sleeper_full = json.loads(get(SOURCES['sleeper_players']))
        espn = build_espn(sleeper_full)
        old_espn = load_previous(espn_path)
        if old_espn.get('players') != espn['players'] or old_espn.get('unmatched') != espn['unmatched']:
            espn['fetched'] = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%MZ')
            with open(espn_path, 'w', encoding='utf-8') as f:
                json.dump(espn, f, separators=(',', ':'))
            print(f"Wrote {espn_path} ({os.path.getsize(espn_path):,} bytes, {len(espn['players'])} matched, {len(espn['unmatched'])} unmatched).", file=sys.stderr)
        else:
            print('No ESPN injury changes; espn.json left as is.', file=sys.stderr)
    except Exception as e:
        print('  (ESPN injuries unavailable, keeping the previous espn.json:', e, ')', file=sys.stderr)

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
