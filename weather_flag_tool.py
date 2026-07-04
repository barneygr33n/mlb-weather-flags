#!/usr/bin/env python3
"""
MLB Weather Flag Tool — v3: ANTICIPATORY OPENER TRIPWIRE (2026-07-04)
Runs via GitHub Actions at 5am and 5pm CDT (10:00 and 22:00 UTC).
Outputs docs/index.html — served via GitHub Pages.

v3 changes:
  - Adds an opener predictor (predict_opener): walk-forward-validated model of
    where the market total will FIRST post, using park mean + recent-league
    mean + each team's recent deviation vs park baseline. Lets the user know
    the night before whether to fire the moment a line posts.
  - Logs the first-seen market total per game to docs/totals_log.csv (opener
    proxy) — the log both feeds the predictor's team-deviation terms and
    accumulates a proprietary opener history over time.
  - Flagged cards with no market total yet show an anticipatory tripwire line
    (expected opener, fair number, action threshold). Flagged cards that
    already have a market total show a compact opener-check line instead.

v2 changes:
  - Coefficients replaced with gap-vs-OPENER betas (weather_market_residual.py,
    5,393 games 2023-2025). The opener is the bettable target: temp/wind get
    priced by close, humidity NEVER gets priced (see project memory).
  - Originates a fair total per game: market total + sum of beta x (forecast
    condition - park baseline). Confirmed-factor-only adjustment drives flags;
    full-model adjustment shown as info.
  - Market totals from The Odds API (env ODDS_API_KEY, GitHub secret).
    Degrades gracefully: no key/no line -> shows run adjustment only.
  - Flag tiers per park factor: STRONG (|t|>=2.0 vs opener), MODERATE (1.5-2.0).
    Flag fires when confirmed-factor adjustment >= 0.5 runs.

Data sources:
  MLB schedule: statsapi.mlb.com
  Weather:      api.open-meteo.com
  Totals:       api.the-odds-api.com (optional, ODDS_API_KEY)
"""

import csv, json, math, os, sys, datetime, requests
from zoneinfo import ZoneInfo

UTC = ZoneInfo("UTC")

# ── Park data ─────────────────────────────────────────────────────────────────
# Coefficients: per-park OLS gap_open ~ temp + relh + eff_wind
#   gap_open = actual runs − OPENING total (weather_market_residual.py, 2026-07-04)
#   These betas ARE the origination model: adj = Σ β × (condition − baseline)
#   predicts how far the opener will miss. fair total = market total + adj.
# base_temp/base_relh: park's historical mean conditions (2023–2025 sample).
#   Wind baseline is 0 (calm/cross = no adjustment).
# confirmed: factors with |t| >= 1.5 vs the opener at that park.
#   "strong" = |t| >= 2.0, "moderate" = 1.5–2.0. Only confirmed factors
#   contribute to the flag-driving adjustment; full-model adj is informational.
# cf_bearing: compass degrees FROM home plate TOWARD center field
#   used to compute eff_wind = wind_speed × cos(wind_vector_dir − cf_bearing)
#   wind_vector_dir = (met_wind_dir + 180) % 360  (where wind is GOING, not coming from)
#   eff_wind > 0 = blowing out;  eff_wind < 0 = blowing in
#
# Signal interpretation:
#   temp_b > 0 → warmer temps = more runs; temp_b < 0 → warmer = fewer
#   relh_b > 0 → more humid = more runs (DRY = UNDER lean)
#   relh_b < 0 → more humid = fewer runs (DRY = OVER lean)
#   wind_b > 0 → wind-out = more runs;  wind_b < 0 → wind-out = fewer

PARKS = {
    "Fenway Park": {
        "lat": 42.3467, "lon": -71.0972, "cf_bearing": 95, "tz": "America/New_York",
        "temp_b": -0.0074, "temp_t": -0.29,
        "relh_b": -0.0391, "relh_t": -2.45,
        "wind_b":  0.0155, "wind_t":  0.28,
        "base_temp": 68.9, "base_relh": 66.3,
        "confirmed": {"relh": "strong"},
    },
    "Yankee Stadium": {
        "lat": 40.8296, "lon": -73.9262, "cf_bearing": 355, "tz": "America/New_York",
        "temp_b":  0.0559, "temp_t":  2.27,
        "relh_b":  0.0014, "relh_t":  0.08,
        "wind_b":  0.0220, "wind_t":  0.57,
        "base_temp": 71.9, "base_relh": 54.3,
        "confirmed": {"temp": "strong"},
    },
    "Citi Field": {
        "lat": 40.7571, "lon": -73.8458, "cf_bearing": 40, "tz": "America/New_York",
        "temp_b":  0.0463, "temp_t":  1.66,
        "relh_b": -0.0370, "relh_t": -2.17,
        "wind_b":  0.0457, "wind_t":  1.05,
        "base_temp": 72.1, "base_relh": 51.4,
        "confirmed": {"relh": "strong", "temp": "moderate"},
    },
    "Oriole Park at Camden Yards": {
        "lat": 39.2838, "lon": -76.6218, "cf_bearing": 30, "tz": "America/New_York",
        "temp_b":  0.0261, "temp_t":  0.86,
        "relh_b": -0.0201, "relh_t": -1.28,
        "wind_b":  0.0421, "wind_t":  0.84,
        "base_temp": 77.6, "base_relh": 55.1,
        "confirmed": {},
    },
    "Nationals Park": {
        "lat": 38.8730, "lon": -77.0074, "cf_bearing": 355, "tz": "America/New_York",
        "temp_b":  0.0467, "temp_t":  1.63,
        "relh_b":  0.0008, "relh_t":  0.05,
        "wind_b": -0.0813, "wind_t": -1.64,
        "base_temp": 76.9, "base_relh": 54.7,
        "confirmed": {"temp": "moderate", "wind": "moderate"},
        "notes": "Wind-out → UNDER here is counterintuitive but held vs opener AND close; market moves it the wrong way.",
    },
    "Kauffman Stadium": {
        "lat": 39.0517, "lon": -94.4803, "cf_bearing": 10, "tz": "America/Chicago",
        "temp_b":  0.0083, "temp_t":  0.29,
        "relh_b":  0.0286, "relh_t":  1.56,
        "wind_b":  0.1585, "wind_t":  1.75,
        "base_temp": 78.1, "base_relh": 55.5,
        "confirmed": {"relh": "moderate", "wind": "moderate"},
        "notes": "Market moves the WRONG way on dry days here — edge grows toward close.",
    },
    "Truist Park": {
        "lat": 33.8908, "lon": -84.4678, "cf_bearing": 15, "tz": "America/New_York",
        "temp_b":  0.0140, "temp_t":  0.39,
        "relh_b":  0.0189, "relh_t":  0.97,
        "wind_b": -0.0734, "wind_t": -1.11,
        "base_temp": 78.7, "base_relh": 59.2,
        "confirmed": {},
    },
    "Oracle Park": {
        "lat": 37.7786, "lon": -122.3893, "cf_bearing": 315, "tz": "America/Los_Angeles",
        "temp_b":  0.0342, "temp_t":  0.49,
        "relh_b": -0.0452, "relh_t": -1.62,
        "wind_b": -0.0169, "wind_t": -0.41,
        "base_temp": 62.6, "base_relh": 66.8,
        "confirmed": {"relh": "moderate"},
    },
    "Wrigley Field": {
        "lat": 41.9484, "lon": -87.6553, "cf_bearing": 50, "tz": "America/Chicago",
        "temp_b":  0.0745, "temp_t":  1.13,
        "relh_b":  0.0551, "relh_t":  1.05,
        "wind_b":  0.1166, "wind_t":  1.20,
        "base_temp": 69.2, "base_relh": 52.2,
        "confirmed": {},
        "notes": "Wrigley wind is real vs the CLOSE (t=2.27) but partially priced into openers — "
                 "no confirmed opener edge. If betting wind here, you're racing the market, not beating the number.",
    },
    "Target Field": {
        "lat": 44.9817, "lon": -93.2781, "cf_bearing": 5, "tz": "America/Chicago",
        "temp_b":  0.0431, "temp_t":  1.77,
        "relh_b":  0.0426, "relh_t":  2.23,
        "wind_b":  0.0137, "wind_t":  0.33,
        "base_temp": 72.2, "base_relh": 48.6,
        "confirmed": {"relh": "strong", "temp": "moderate"},
    },
    "T-Mobile Park": {
        "lat": 47.5914, "lon": -122.3325, "cf_bearing": 345, "tz": "America/Los_Angeles",
        "temp_b":  0.0480, "temp_t":  1.39,
        "relh_b":  0.0326, "relh_t":  1.25,
        "wind_b": -0.0727, "wind_t": -1.11,
        "base_temp": 66.5, "base_relh": 48.3,
        "confirmed": {},
    },
    "Rogers Centre": {
        "lat": 43.6414, "lon": -79.3894, "cf_bearing": 355, "tz": "America/Toronto",
        "temp_b":  0.0004, "temp_t":  0.01,
        "relh_b":  0.0106, "relh_t":  0.42,
        "wind_b":  0.0939, "wind_t":  2.16,
        "base_temp": 71.1, "base_relh": 52.2,
        "confirmed": {"wind": "strong"},
        "notes": "",
    },
    "PNC Park": {
        "lat": 40.4469, "lon": -80.0057, "cf_bearing": 30, "tz": "America/New_York",
        "temp_b":  0.0414, "temp_t":  1.53,
        "relh_b": -0.0054, "relh_t": -0.32,
        "wind_b":  0.0568, "wind_t":  1.21,
        "base_temp": 73.9, "base_relh": 51.9,
        "confirmed": {"temp": "moderate"},
    },
    "Comerica Park": {
        "lat": 42.3390, "lon": -83.0485, "cf_bearing": 355, "tz": "America/Detroit",
        "temp_b":  0.0263, "temp_t":  0.99,
        "relh_b":  0.0046, "relh_t":  0.25,
        "wind_b": -0.0126, "wind_t": -0.32,
        "base_temp": 72.5, "base_relh": 54.5,
        "confirmed": {},
    },
    "Great American Ball Park": {
        "lat": 39.0979, "lon": -84.5079, "cf_bearing": 350, "tz": "America/New_York",
        "temp_b": -0.0033, "temp_t": -0.12,
        "relh_b":  0.0014, "relh_t":  0.08,
        "wind_b":  0.0805, "wind_t":  1.48,
        "base_temp": 76.7, "base_relh": 57.9,
        "confirmed": {},
        "notes": "GABP: historically suspicious park (always-Over baseline mispricing). Wind t=1.48 just under threshold — no confirmed edge.",
    },
    "American Family Field": {
        "lat": 43.0280, "lon": -87.9712, "cf_bearing": 15, "tz": "America/Chicago",
        "temp_b":  0.0627, "temp_t":  1.27,
        "relh_b":  0.0135, "relh_t":  0.57,
        "wind_b": -0.0739, "wind_t": -1.11,
        "base_temp": 76.4, "base_relh": 58.6,
        "confirmed": {},
    },
    "Dodger Stadium": {
        "lat": 34.0739, "lon": -118.2400, "cf_bearing": 30, "tz": "America/Los_Angeles",
        "temp_b": -0.0548, "temp_t": -1.26,
        "relh_b": -0.0160, "relh_t": -0.65,
        "wind_b":  0.0531, "wind_t":  0.68,
        "base_temp": 71.4, "base_relh": 54.4,
        "confirmed": {},
    },
    "Citizens Bank Park": {
        "lat": 39.9061, "lon": -75.1665, "cf_bearing": 15, "tz": "America/New_York",
        "temp_b": -0.0187, "temp_t": -0.60,
        "relh_b":  0.0123, "relh_t":  0.76,
        "wind_b": -0.0316, "wind_t": -0.79,
        "base_temp": 75.5, "base_relh": 54.7,
        "confirmed": {},
    },
    "Coors Field": {
        "lat": 39.7559, "lon": -104.9942, "cf_bearing": 350, "tz": "America/Denver",
        "temp_b":  0.0302, "temp_t":  0.92,
        "relh_b":  0.0052, "relh_t":  0.22,
        "wind_b":  0.0407, "wind_t":  0.62,
        "base_temp": 75.4, "base_relh": 33.7,
        "confirmed": {},
    },
    "Progressive Field": {
        "lat": 41.4962, "lon": -81.6852, "cf_bearing": 5, "tz": "America/New_York",
        "temp_b":  0.0268, "temp_t":  1.09,
        "relh_b":  0.0068, "relh_t":  0.37,
        "wind_b":  0.0167, "wind_t":  0.37,
        "base_temp": 71.4, "base_relh": 55.0,
        "confirmed": {},
    },
    "Petco Park": {
        "lat": 32.7076, "lon": -117.1570, "cf_bearing": 320, "tz": "America/Los_Angeles",
        "temp_b":  0.0267, "temp_t":  0.58,
        "relh_b": -0.0017, "relh_t": -0.05,
        "wind_b": -0.0070, "wind_t": -0.06,
        "base_temp": 68.3, "base_relh": 69.2,
        "confirmed": {},
    },
    "Angel Stadium": {
        "lat": 33.8003, "lon": -117.8827, "cf_bearing": 65, "tz": "America/Los_Angeles",
        "temp_b": -0.0186, "temp_t": -0.40,
        "relh_b": -0.0265, "relh_t": -0.84,
        "wind_b":  0.0414, "wind_t":  0.51,
        "base_temp": 73.9, "base_relh": 63.1,
        "confirmed": {},
    },
    "Busch Stadium": {
        "lat": 38.6226, "lon": -90.1928, "cf_bearing": 60, "tz": "America/Chicago",
        "temp_b":  0.0089, "temp_t":  0.31,
        "relh_b": -0.0140, "relh_t": -0.72,
        "wind_b":  0.0020, "wind_t":  0.05,
        "base_temp": 78.4, "base_relh": 51.2,
        "confirmed": {},
    },
    "Guaranteed Rate Field": {
        "lat": 41.8299, "lon": -87.6338, "cf_bearing": 125, "tz": "America/Chicago",
        "temp_b": -0.0011, "temp_t": -0.03,
        "relh_b":  0.0144, "relh_t":  0.63,
        "wind_b":  0.0125, "wind_t":  0.25,
        "base_temp": 71.8, "base_relh": 51.7,
        "confirmed": {},
    },
    # A's relocated to Sacramento in 2025 — not yet in regression model (insufficient sample)
    "Sutter Health Park": {
        "lat": 38.5769, "lon": -121.5059, "cf_bearing": 45, "tz": "America/Los_Angeles",
        "temp_b": 0.0, "temp_t": 0.0,
        "relh_b": 0.0, "relh_t": 0.0,
        "wind_b": 0.0, "wind_t": 0.0,
        "base_temp": 75.0, "base_relh": 50.0,
        "confirmed": {},
        "notes": "⚾ SACRAMENTOVERS: Sutter Health Park showed a strong unconditional Over lean in 2025 "
                 "(FG Over 54% W/L, F5 Over 61% W/L). Weather model not yet validated — insufficient sample. "
                 "Treat any sharp Over signal here as reinforced by park factor.",
    },
}

# Normalize MLB Stats API venue names → PARKS dict keys
VENUE_MAP = {
    "Nationals Park at Half Street":   "Nationals Park",
    "Rate Field":                      "Guaranteed Rate Field",
    "Guaranteed Rate Field":           "Guaranteed Rate Field",  # keep
    "LoanDepot Park":                  "LoanDepot Park",
    "loanDepot park":                  "LoanDepot Park",
    # A's moved to Sacramento in 2025
    "Sutter Health Park":              "Sutter Health Park",
    "Sutter Health Park West Sacramento": "Sutter Health Park",
    # Dodger Stadium naming rights change (2026)
    "UNIQLO Field at Dodger Stadium":  "Dodger Stadium",
    # PARKS dict spells it "Petco Park"; PARK_OPEN_MEAN (opener model) uses the
    # official "PETCO Park" capitalization — alias so both resolve the same way.
    "PETCO Park":                      "Petco Park",
}

# Dome and retractable-roof parks excluded from weather model.
# Roof status can't be determined automatically, so no weather signal is possible.
DOME_VENUES = {
    "Globe Life Field",       # Texas Rangers — retractable roof
    "Tropicana Field",        # Tampa Bay Rays — fixed dome
    "LoanDepot Park",         # Miami Marlins — retractable roof
    "loanDepot park",
    "Chase Field",            # Arizona Diamondbacks — retractable roof
    "Daikin Park",            # Houston Astros (fmr. Minute Maid Park) — retractable roof
    "Minute Maid Park",
}

# Parks in the model that have retractable roofs.
# Weather signals only apply when the roof is open.
RETRACTABLE_ROOF_PARKS = {
    "Rogers Centre",        # Toronto Blue Jays
    "American Family Field", # Milwaukee Brewers
    "T-Mobile Park",        # Seattle Mariners
}

def roof_status(temp, precip_prob):
    """Estimate whether a retractable roof is likely open or closed.
    Returns 'likely_open', 'likely_closed', or 'uncertain'."""
    if temp is None:
        return "uncertain"
    rain_likely = precip_prob is not None and precip_prob > 40
    cold = temp < 54
    warm_and_dry = temp >= 65 and (precip_prob is None or precip_prob <= 20)
    if cold or rain_likely:
        return "likely_closed"
    elif warm_and_dry:
        return "likely_open"
    return "uncertain"

def normalize_venue(name):
    return VENUE_MAP.get(name, name)

# ── MLB schedule ──────────────────────────────────────────────────────────────
def fetch_schedule(date_str):
    """Return list of game dicts for a YYYY-MM-DD date string."""
    url = ("https://statsapi.mlb.com/api/v1/schedule"
           f"?sportId=1&date={date_str}&hydrate=venue,teams(team)")
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  Schedule fetch error ({date_str}): {e}", file=sys.stderr)
        return []

    games = []
    for date_entry in data.get("dates", []):
        for game in date_entry.get("games", []):
            abstract_state = game.get("status", {}).get("abstractGameState", "")
            if abstract_state == "Final":
                continue  # skip completed games
            venue_raw = game.get("venue", {}).get("name", "")
            venue = normalize_venue(venue_raw)
            game_dt_str = game.get("gameDate", "")
            away = (game.get("teams", {}).get("away", {})
                       .get("team", {}).get("name", "?"))
            home = (game.get("teams", {}).get("home", {})
                       .get("team", {}).get("name", "?"))
            games.append({
                "venue": venue, "venue_raw": venue_raw,
                "away": away, "home": home,
                "game_dt_str": game_dt_str, "date": date_str,
            })
    return games

# ── Open-Meteo weather ────────────────────────────────────────────────────────
_wx_cache = {}

def fetch_hourly_wx(lat, lon, tz_str):
    """Return dict of {local_hour_str → {temp, relh, windspeed, winddir}}."""
    key = (lat, lon)
    if key in _wx_cache:
        return _wx_cache[key]
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", timeout=20, params={
            "latitude": lat, "longitude": lon,
            "hourly": "temperature_2m,relativehumidity_2m,windspeed_10m,winddirection_10m,precipitation_probability",
            "temperature_unit": "fahrenheit",
            "windspeed_unit": "mph",
            "timezone": tz_str,
            "forecast_days": 3,
        })
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  Weather fetch error ({lat},{lon}): {e}", file=sys.stderr)
        return {}

    h = data.get("hourly", {})
    result = {}
    for i, t in enumerate(h.get("time", [])):
        result[t] = {
            "temp":       h["temperature_2m"][i]           if i < len(h.get("temperature_2m", [])) else None,
            "relh":       h["relativehumidity_2m"][i]      if i < len(h.get("relativehumidity_2m", [])) else None,
            "windspeed":  h["windspeed_10m"][i]            if i < len(h.get("windspeed_10m", [])) else None,
            "winddir":    h["winddirection_10m"][i]        if i < len(h.get("winddirection_10m", [])) else None,
            "precip_prob": h["precipitation_probability"][i] if i < len(h.get("precipitation_probability", [])) else None,
        }
    _wx_cache[key] = result
    return result

def wx_at_gametime(hourly, game_dt_str, tz_str, date_str=None):
    """Return (weather_dict, game_local_datetime) for the hour closest to game start.
    Falls back to 3pm local time if game_dt_str is missing (TBD games)."""
    local = None
    if game_dt_str:
        try:
            game_dt = datetime.datetime.fromisoformat(game_dt_str.replace("Z", "+00:00"))
            local = game_dt.astimezone(ZoneInfo(tz_str))
        except Exception as e:
            print(f"  Gametime parse error: {e}", file=sys.stderr)

    if local is None:
        # TBD game — fall back to 3pm local as a reasonable median
        try:
            base = datetime.date.fromisoformat(date_str) if date_str else datetime.date.today()
            local = datetime.datetime(base.year, base.month, base.day, 15, 0,
                                      tzinfo=ZoneInfo(tz_str))
            print(f"  TBD game time — using 3pm local fallback", file=sys.stderr)
        except Exception as e:
            print(f"  TBD fallback error: {e}", file=sys.stderr)
            return None, None

    # Round to nearest hour
    target = local.replace(minute=0, second=0, microsecond=0)
    if local.minute >= 30:
        target += datetime.timedelta(hours=1)
    for delta in [0, 1, -1, 2, -2]:
        key = (target + datetime.timedelta(hours=delta)).strftime("%Y-%m-%dT%H:%M")
        if key in hourly:
            return hourly[key], local
    return None, None

# ── Wind math ─────────────────────────────────────────────────────────────────
def calc_eff_wind(wind_speed, wind_dir_met, cf_bearing):
    """
    Positive = blowing toward outfield (out), negative = blowing toward plate (in).
    wind_dir_met: meteorological convention (degrees wind comes FROM).
    cf_bearing: compass direction from home plate toward center field.
    """
    if wind_speed is None or wind_dir_met is None:
        return 0.0
    wind_vector_dir = (wind_dir_met + 180) % 360
    angle_diff = (wind_vector_dir - cf_bearing + 180) % 360 - 180
    return wind_speed * math.cos(math.radians(angle_diff))

def compass(deg):
    if deg is None:
        return "?"
    dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
            "S","SSW","SW","WSW","W","WNW","NW","NNW"]
    return dirs[round(deg / 22.5) % 16]

# ── Market totals (The Odds API) ──────────────────────────────────────────────
# Optional: requires ODDS_API_KEY env var (GitHub Actions secret).
# ~1 request per run against a 500/month quota. Missing key → totals skipped,
# tool still shows run adjustments.

def fetch_market_totals():
    """Return {(home_name, away_name, utc_date): median total across books}."""
    api_key = os.environ.get("ODDS_API_KEY", "").strip()
    if not api_key:
        print("  ODDS_API_KEY not set — skipping market totals", file=sys.stderr)
        return {}
    try:
        r = requests.get(
            "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds",
            timeout=25,
            params={"apiKey": api_key, "regions": "us",
                    "markets": "totals", "oddsFormat": "american"},
        )
        r.raise_for_status()
        events = r.json()
        remaining = r.headers.get("x-requests-remaining")
        if remaining is not None:
            print(f"  Odds API requests remaining this month: {remaining}")
    except Exception as e:
        print(f"  Odds API fetch error: {e}", file=sys.stderr)
        return {}

    totals = {}
    for ev in events:
        home, away = ev.get("home_team"), ev.get("away_team")
        utc_date = (ev.get("commence_time") or "")[:10]
        points = []
        for bk in ev.get("bookmakers", []):
            for mkt in bk.get("markets", []):
                if mkt.get("key") != "totals":
                    continue
                for oc in mkt.get("outcomes", []):
                    p = oc.get("point")
                    if p is not None and oc.get("name") == "Over":
                        points.append(float(p))
        if home and away and points:
            points.sort()
            mid = len(points) // 2
            med = points[mid] if len(points) % 2 else (points[mid-1] + points[mid]) / 2
            totals[(home, away, utc_date)] = med
    print(f"  Market totals loaded for {len(totals)} game(s)")
    return totals


def lookup_market_total(totals, home, away, game_dt_str):
    """Match by team names + UTC date, with a ±1 day fallback for late games."""
    if not totals:
        return None
    utc_date = (game_dt_str or "")[:10]
    for d_off in (0, 1, -1):
        if utc_date:
            try:
                d = (datetime.date.fromisoformat(utc_date)
                     + datetime.timedelta(days=d_off)).isoformat()
            except ValueError:
                d = utc_date
            v = totals.get((home, away, d))
            if v is not None:
                return v
    return None

# ── Totals log (first-seen opener capture) ────────────────────────────────────
# Persists the first market total ever seen per (date, home, away) to
# docs/totals_log.csv, alongside index.html. First-seen = opener proxy: once
# a game has a logged row, later runs never overwrite it, even if the market
# total moves. This both builds a proprietary opener history over time and
# feeds predict_opener()'s recent-league-mean and team-deviation terms.

TOTALS_LOG_COLUMNS = ["first_seen_utc", "date", "home", "away", "venue", "total"]

def load_totals_log(docs_dir="docs"):
    """Return list of dict rows from docs/totals_log.csv, or [] if absent."""
    path = os.path.join(docs_dir, "totals_log.csv")
    if not os.path.exists(path):
        return []
    rows = []
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
    except Exception as e:
        print(f"  Totals log read error: {e}", file=sys.stderr)
        return []
    return rows

def update_totals_log(log_rows, games_with_totals, now_utc, docs_dir="docs"):
    """
    games_with_totals: iterable of dicts with keys date, home, away, venue, total
    (total may be None — skipped, nothing to log yet).
    Mutates/extends log_rows in place with any new (date, home, away) games,
    using first-seen semantics, then writes docs/totals_log.csv.
    Returns the (possibly extended) log_rows list.
    """
    seen_keys = {(r["date"], r["home"], r["away"]) for r in log_rows}
    ts = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    for g in games_with_totals:
        if g.get("total") is None:
            continue
        key = (g["date"], g["home"], g["away"])
        if key in seen_keys:
            continue  # first-seen already logged — do not update
        log_rows.append({
            "first_seen_utc": ts,
            "date": g["date"],
            "home": g["home"],
            "away": g["away"],
            "venue": g["venue"],
            "total": g["total"],
        })
        seen_keys.add(key)

    os.makedirs(docs_dir, exist_ok=True)
    path = os.path.join(docs_dir, "totals_log.csv")
    try:
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=TOTALS_LOG_COLUMNS)
            writer.writeheader()
            for r in log_rows:
                writer.writerow({c: r.get(c, "") for c in TOTALS_LOG_COLUMNS})
    except Exception as e:
        print(f"  Totals log write error: {e}", file=sys.stderr)
    return log_rows

# ── Opener predictor (v3) ─────────────────────────────────────────────────────
# fitted 2026-07-04 on 11,002 games 2021-2025 (walk-forward validated: MAE
# 0.446, sigma 0.592, 92.4% of openers within ±1.0); park means from 2024-2025
# openers.

LEAGUE_OPEN_MEAN = 8.411
PARK_OPEN_MEAN = {
    "American Family Field": 8.150, "Angel Stadium": 8.586, "Busch Stadium": 8.194,
    "Chase Field": 8.815, "Citi Field": 8.085, "Citizens Bank Park": 8.441,
    "Comerica Park": 8.020, "Coors Field": 10.882, "Daikin Park": 8.255,
    "Dodger Stadium": 8.566, "Fenway Park": 8.991, "George M. Steinbrenner Field": 8.669,
    "Globe Life Field": 8.398, "Great American Ball Park": 9.029, "Kauffman Stadium": 8.600,
    "Nationals Park": 8.668, "Oakland Coliseum": 8.006, "Oracle Park": 7.690,
    "Oriole Park at Camden Yards": 8.729, "PETCO Park": 7.867, "PNC Park": 8.178,
    "Progressive Field": 8.138, "Rate Field": 8.315, "Rogers Centre": 8.329,
    "T-Mobile Park": 7.348, "Target Field": 8.234, "Tropicana Field": 7.822,
    "Truist Park": 8.511, "Wrigley Field": 8.214, "Yankee Stadium": 8.505,
    "loanDepot park": 8.147,
}

def _park_open_mean(venue):
    """Look up PARK_OPEN_MEAN, trying the raw venue name, the normalized
    PARKS-dict spelling, and the alt PETCO/Rate Field/loanDepot spellings."""
    if venue in PARK_OPEN_MEAN:
        return PARK_OPEN_MEAN[venue]
    normalized = normalize_venue(venue)
    if normalized in PARK_OPEN_MEAN:
        return PARK_OPEN_MEAN[normalized]
    # normalize_venue maps "PETCO Park" -> "Petco Park", but PARK_OPEN_MEAN
    # (verbatim from the opener model) is keyed "PETCO Park" — try the reverse.
    aliases = {"Petco Park": "PETCO Park", "Guaranteed Rate Field": "Rate Field",
               "LoanDepot Park": "loanDepot park"}
    if normalized in aliases and aliases[normalized] in PARK_OPEN_MEAN:
        return PARK_OPEN_MEAN[aliases[normalized]]
    return LEAGUE_OPEN_MEAN

def predict_opener(home_full_name, away_full_name, venue, log_rows):
    """
    Predict where the market total will FIRST open, using:
      pred = 1.9198 + 1.0867*pm - 0.3127*lgm + 0.7364*dev_h + 0.3654*dev_a
    pm  = park's opener mean (PARK_OPEN_MEAN, fallback LEAGUE_OPEN_MEAN)
    lgm = mean of logged totals from the last 60 days (>=50 rows required,
          else LEAGUE_OPEN_MEAN)
    dev_h/dev_a = home/away team's mean (logged total - that game's park mean)
          over their most recent 15 logged games (home or away), requires
          >=5 logged games for that team, else 0.0
    """
    pm = _park_open_mean(venue)

    # lgm: recent-60-day league mean of logged totals
    lgm = LEAGUE_OPEN_MEAN
    recent_totals = []
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=60)
    for r in log_rows:
        try:
            d = datetime.date.fromisoformat(r["date"])
        except (ValueError, KeyError, TypeError):
            continue
        if d >= cutoff:
            try:
                recent_totals.append(float(r["total"]))
            except (ValueError, KeyError, TypeError):
                continue
    if len(recent_totals) >= 50:
        lgm = sum(recent_totals) / len(recent_totals)

    def team_dev(team_name):
        team_rows = [r for r in log_rows if r.get("home") == team_name or r.get("away") == team_name]
        if len(team_rows) < 5:
            return 0.0
        # most recent 15 by first_seen_utc (fallback: date) descending
        def sort_key(r):
            return r.get("first_seen_utc") or r.get("date") or ""
        team_rows_sorted = sorted(team_rows, key=sort_key, reverse=True)[:15]
        devs = []
        for r in team_rows_sorted:
            try:
                total = float(r["total"])
            except (ValueError, KeyError, TypeError):
                continue
            park_mean = _park_open_mean(r.get("venue", ""))
            devs.append(total - park_mean)
        if not devs:
            return 0.0
        return sum(devs) / len(devs)

    dev_h = team_dev(home_full_name)
    dev_a = team_dev(away_full_name)

    pred = 1.9198 + 1.0867 * pm - 0.3127 * lgm + 0.7364 * dev_h + 0.3654 * dev_a
    return pred

# ── Totals origination ────────────────────────────────────────────────────────
# adj = Σ β × (forecast condition − park baseline), per factor.
# adj_conf (confirmed factors only) drives flags; adj_all is informational.
# fair total = market total + adj_conf.

EDGE_THRESHOLD_RUNS = 0.5

def originate(park, temp, relh, wind_eff):
    if park is None:
        return None
    contribs = {}
    if temp is not None:
        contribs["temp"] = park["temp_b"] * (temp - park["base_temp"])
    if relh is not None:
        contribs["relh"] = park["relh_b"] * (relh - park["base_relh"])
    if wind_eff is not None:
        contribs["wind"] = park["wind_b"] * wind_eff  # baseline 0 = calm/cross
    confirmed = park.get("confirmed", {})
    adj_all = sum(contribs.values())
    adj_conf = sum(v for f, v in contribs.items() if f in confirmed)
    strongest = max((confirmed[f] for f in confirmed if f in contribs), default=None,
                    key=lambda s: {"moderate": 1, "strong": 2}.get(s, 0))
    return {
        "contribs": contribs,
        "adj_all": adj_all,
        "adj_conf": adj_conf,
        "confirmed": confirmed,
        "flag": abs(adj_conf) >= EDGE_THRESHOLD_RUNS,
        "tier": strongest,
    }

# ── Signal detection ──────────────────────────────────────────────────────────
# Threshold philosophy:
#   TEMP:     extreme ends of the temp distribution at each park
#   HUMIDITY: matches the single-factor bucket boundaries that built the model
#   WIND:     meaningful effective wind in the outfield/infield direction

TEMP_WARM_F   = 84    # °F — warm threshold for positive temp signals
TEMP_COLD_F   = 62    # °F — cold threshold for negative temp signals
RELH_DRY_PCT  = 40    # % — dry bucket (< 40%)
RELH_HUM_PCT  = 70    # % — humid bucket (>= 70%)
WIND_EFF_MPH  = 6     # mph effective wind before wind signal triggers

def detect_signals(park, temp, relh, wind_eff):
    """
    Returns list of (factor, abs_t, direction, condition_description) tuples.
    direction: "OVER" or "UNDER"
    """
    sigs = []

    # Temperature
    b, t = park["temp_b"], park["temp_t"]
    if abs(t) >= 1.0 and temp is not None:
        over_if_warm = b > 0   # positive β → warm = more runs
        if temp >= TEMP_WARM_F:
            direction = "OVER" if over_if_warm else "UNDER"
            sigs.append(("TEMP", abs(t), direction, f"Warm ({temp:.0f}°F)"))
        elif temp <= TEMP_COLD_F:
            direction = "UNDER" if over_if_warm else "OVER"
            sigs.append(("TEMP", abs(t), direction, f"Cold ({temp:.0f}°F)"))

    # Humidity
    b, t = park["relh_b"], park["relh_t"]
    if abs(t) >= 1.0 and relh is not None:
        if relh < RELH_DRY_PCT:
            # DRY: relh is LOW → b>0 means UNDER, b<0 means OVER
            direction = "UNDER" if b > 0 else "OVER"
            sigs.append(("HUMIDITY", abs(t), direction, f"Dry ({relh:.0f}% RH)"))
        elif relh >= RELH_HUM_PCT:
            # HUMID: relh is HIGH → b>0 means OVER, b<0 means UNDER
            direction = "OVER" if b > 0 else "UNDER"
            sigs.append(("HUMIDITY", abs(t), direction, f"Humid ({relh:.0f}% RH)"))

    # Wind
    b, t = park["wind_b"], park["wind_t"]
    if abs(t) >= 1.0 and abs(wind_eff) >= WIND_EFF_MPH:
        blowing_out = wind_eff > 0
        if blowing_out:
            direction = "OVER" if b > 0 else "UNDER"
            sigs.append(("WIND", abs(t), direction, f"Out {wind_eff:.0f} mph"))
        else:
            direction = "UNDER" if b > 0 else "OVER"
            sigs.append(("WIND", abs(t), direction, f"In {abs(wind_eff):.0f} mph"))

    return sorted(sigs, key=lambda s: -s[1])

def net_signal(signals):
    """
    Combine all factor signals into a single net direction.
    Each signal contributes its t-stat as positive (OVER) or negative (UNDER).
    Returns (direction, strength_label, net_score).
    direction: "OVER", "UNDER", or "MIXED"
    """
    if not signals:
        return None, None, 0
    net = sum(s[1] if s[2] == "OVER" else -s[1] for s in signals)
    abs_net = abs(net)
    if abs_net < 0.5:
        return "MIXED", None, net
    direction = "OVER" if net > 0 else "UNDER"
    if abs_net >= 2.5:
        strength = "★★★ Strong"
    elif abs_net >= 1.5:
        strength = "★★ Moderate"
    else:
        strength = "★ Weak"
    return direction, strength, net

def stars(abs_t):
    if abs_t >= 2.0: return "★★★"
    if abs_t >= 1.5: return "★★"
    return "★"

# ── HTML generation ───────────────────────────────────────────────────────────
def render_card(card):
    venue      = card["venue"]
    away       = card["away"]
    home       = card["home"]
    label      = card["date_label"]
    signals    = card["signals"]
    game_local = card.get("game_local")
    notes      = card.get("notes", "")

    direction, strength, net_score = net_signal(signals)
    orig = card.get("orig")
    market_total = card.get("market_total")

    # Card accent color: originated edge takes priority, then bucket signals
    if orig and orig["flag"]:
        accent = "#198754" if orig["adj_conf"] > 0 else "#dc3545"
    elif direction == "OVER":
        accent = "#198754"   # green
    elif direction == "UNDER":
        accent = "#dc3545"   # red
    elif direction == "MIXED":
        accent = "#fd7e14"   # orange — conflicting signals
    else:
        accent = "#adb5bd"   # gray — no signal

    # Time string
    time_str = game_local.strftime("%-I:%M %p %Z") if game_local else "TBD"

    # Day badge — primary (blue) for the first/earlier date, secondary for the next
    day_badge_color = "primary" if card.get("is_first") else "secondary"
    day_badge = f'<span class="badge text-bg-{day_badge_color} ms-1">{label}</span>'

    # Roof status for retractable-roof parks
    precip_prob = card.get("precip_prob")
    temp_val    = card.get("temp")
    is_retractable = venue in RETRACTABLE_ROOF_PARKS
    r_status = roof_status(temp_val, precip_prob) if is_retractable else None

    if r_status == "likely_open":
        roof_html = ('<div class="mt-1"><span class="badge text-bg-success" style="font-size:0.78rem">'
                     '🏟 Roof likely OPEN — signals apply</span></div>')
    elif r_status == "likely_closed":
        roof_html = ('<div class="mt-1"><span class="badge text-bg-secondary" style="font-size:0.78rem">'
                     '🏟 Roof likely CLOSED — signals may not apply</span></div>')
        # suppress signals AND origination — roof closed makes weather irrelevant
        signals = []
        direction, strength, net_score = None, None, 0
        accent = "#adb5bd"
        orig = None
    elif r_status == "uncertain":
        roof_html = ('<div class="mt-1"><span class="badge text-bg-warning text-dark" style="font-size:0.78rem">'
                     '🏟 Roof status uncertain — verify before betting</span></div>')
    else:
        roof_html = ""

    # Weather summary line
    wx_parts = []
    temp = card.get("temp")
    relh = card.get("relh")
    wspd = card.get("wind_speed")
    wdir = card.get("wind_dir")
    we   = card.get("wind_eff", 0.0)

    if temp is not None:
        wx_parts.append(f"🌡&nbsp;{temp:.0f}°F")
    if relh is not None:
        relh_tag = ""
        if relh < RELH_DRY_PCT:
            relh_tag = ' <span class="badge text-bg-warning">DRY</span>'
        elif relh >= RELH_HUM_PCT:
            relh_tag = ' <span class="badge text-bg-info">HUMID</span>'
        wx_parts.append(f"💧&nbsp;{relh:.0f}%{relh_tag}")
    if wspd is not None:
        wd_str = compass(wdir)
        if we > WIND_EFF_MPH:
            we_tag = f' <span class="badge text-bg-warning">OUT&nbsp;{we:.0f}</span>'
        elif we < -WIND_EFF_MPH:
            we_tag = f' <span class="badge text-bg-warning">IN&nbsp;{abs(we):.0f}</span>'
        else:
            we_tag = ""
        wx_parts.append(f"🌬&nbsp;{wspd:.0f}&nbsp;mph&nbsp;{wd_str}{we_tag}")
    if wx_parts:
        wx_line = " &nbsp;·&nbsp; ".join(wx_parts)
    elif card.get("dome"):
        wx_line = "<em class='text-muted'>🏟 Dome/retractable roof — weather not applicable</em>"
    else:
        wx_line = "<em class='text-muted'>weather unavailable</em>"

    # ── Origination block (v2): fair number vs market ─────────────────────────
    orig_html = ""
    if orig is not None and not card.get("dome"):
        fct_names = {"temp": "temp", "relh": "humidity", "wind": "wind"}
        parts = []
        for f, v in orig["contribs"].items():
            if abs(v) < 0.05:
                continue
            tag = orig["confirmed"].get(f)
            star = "★★★" if tag == "strong" else "★★" if tag == "moderate" else ""
            mark = f"{star} " if star else ""
            parts.append(f"{mark}{fct_names[f]} {v:+.2f}")
        breakdown = " · ".join(parts) if parts else "conditions ≈ park average"

        if orig["flag"]:
            lean = "OVER" if orig["adj_conf"] > 0 else "UNDER"
            badge_cls = "text-bg-success" if lean == "OVER" else "text-bg-danger"
            tier_lbl = "★★★" if orig["tier"] == "strong" else "★★"
            if market_total is not None:
                fair = market_total + orig["adj_conf"]
                edge_txt = (f"🎯 {lean} {market_total:g} &nbsp;·&nbsp; "
                            f"fair {fair:.1f} ({orig['adj_conf']:+.1f} runs) {tier_lbl}")
            else:
                edge_txt = f"🎯 {lean} lean {orig['adj_conf']:+.1f} runs vs opener {tier_lbl}"
            orig_html = (f'<div class="mt-1"><span class="badge {badge_cls}" '
                         f'style="font-size:0.88rem">{edge_txt}</span>'
                         f'<div class="text-muted" style="font-size:0.74rem">{breakdown}'
                         + (f' · full model {orig["adj_all"]:+.2f}' if abs(orig["adj_all"] - orig["adj_conf"]) >= 0.1 else "")
                         + '</div></div>')
        else:
            mkt_str = f"market {market_total:g} · " if market_total is not None else ""
            orig_html = (f'<div class="text-muted mt-1" style="font-size:0.76rem">'
                         f'{mkt_str}confirmed adj {orig["adj_conf"]:+.2f} runs'
                         + (f' · full model {orig["adj_all"]:+.2f}' if abs(orig["adj_all"] - orig["adj_conf"]) >= 0.1 else "")
                         + f' · {breakdown}</div>')

    # ── Opener tripwire (v3): anticipatory prediction for flagged games ────────
    tripwire_html = ""
    pred = card.get("opener_pred")
    if orig is not None and orig.get("flag") and pred is not None:
        adj_conf = orig["adj_conf"]
        if market_total is None:
            fair = pred + adj_conf
            action_line = (f"⏱ Expected opener ~{pred:.1f} · fair ≈ {fair:.1f} · "
                           f"if opens ≤ {pred + 1.0:.1f} → standard play at open · "
                           f"if higher → verify pitching/roof/news before betting")
        else:
            diff = market_total - pred
            verify = "— verify before betting" if diff > 1.0 else ""
            action_line = (f"⏱ Opener check: expected ~{pred:.1f}, market {market_total:g} "
                           f"({diff:+.1f} vs expected{verify})")
        tripwire_html = (f'<div class="text-muted mt-1" style="font-size:0.74rem">{action_line}</div>')

    # Net signal headline
    if direction == "OVER":
        headline = (f'<span class="badge text-bg-success" style="font-size:0.85rem">'
                    f'⬆ OVER lean &nbsp; {strength}</span>')
    elif direction == "UNDER":
        headline = (f'<span class="badge text-bg-danger" style="font-size:0.85rem">'
                    f'⬇ UNDER lean &nbsp; {strength}</span>')
    elif direction == "MIXED":
        headline = (f'<span class="badge text-bg-warning text-dark" style="font-size:0.85rem">'
                    f'↔ Mixed signals — no clear lean</span>')
    else:
        headline = '<span class="text-muted" style="font-size:0.8rem">No signal at this venue</span>'

    # Factor detail list (supporting evidence)
    detail_lines = ""
    for (factor, abs_t, dir_, cond) in signals:
        arrow = "⬆" if dir_ == "OVER" else "⬇"
        detail_lines += (f'<li style="font-size:0.78rem">'
                         f'{stars(abs_t)} {cond} → {arrow} {dir_} ({factor.lower()})</li>')
    detail_html = f'<ul class="mb-0 ps-3 mt-1">{detail_lines}</ul>' if detail_lines else ""

    # Notes
    notes_html = (f'<div class="text-muted mt-1" style="font-size:0.75rem">⚠️ {notes}</div>'
                  if notes else "")

    return f'''
  <div class="card mb-2" style="border-left: 4px solid {accent}; border-radius: 6px">
    <div class="card-body py-2 px-3">
      <div class="d-flex justify-content-between align-items-start">
        <div>
          <strong style="font-size:0.95rem">{away} @ {home}</strong>{day_badge}
          <div class="text-muted" style="font-size:0.8rem">{venue} · {time_str}</div>
        </div>
      </div>
      <div class="mt-1" style="font-size:0.82rem">{wx_line}</div>
      {roof_html}
      {orig_html}
      {tripwire_html}
      <div class="mt-1">{headline}</div>
      {detail_html}
      {notes_html}
    </div>
  </div>'''


def generate_html(cards, generated_at):
    # Collect the two unique day labels in order
    seen = []
    for c in cards:
        if c["date_label"] not in seen:
            seen.append(c["date_label"])
    day1 = seen[0] if len(seen) > 0 else "Day 1"
    day2 = seen[1] if len(seen) > 1 else "Day 2"

    def card_flagged(c):
        """Originated-edge flag, honoring roof suppression for retractable parks."""
        orig = c.get("orig")
        if not (orig and orig["flag"]):
            return False
        if c["venue"] in RETRACTABLE_ROOF_PARKS:
            if roof_status(c.get("temp"), c.get("precip_prob")) == "likely_closed":
                return False
        return True

    flagged_today    = sum(1 for c in cards if card_flagged(c) and c.get("is_first"))
    flagged_tomorrow = sum(1 for c in cards if card_flagged(c) and not c.get("is_first"))
    total_today      = sum(1 for c in cards if c.get("is_first"))
    total_tomorrow   = sum(1 for c in cards if not c.get("is_first"))
    gen_central = generated_at.astimezone(ZoneInfo("America/Chicago"))
    gen_str = gen_central.strftime("%b %-d, %Y · %-I:%M %p %Z")

    card_html = "".join(render_card(c) for c in cards)

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, shrink-to-fit=no">
<title>⚾ MLB Weather Flags</title>
<link rel="stylesheet"
  href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css"
  integrity="sha384-T3c6CoIi6uLrA9TneNEoa7RxnatzjcDSCmG1MXxSR1GAsXEV/Dwwykc2MPK8M2HN"
  crossorigin="anonymous">
<style>
  body {{ font-size: 0.9rem; background: #f8f9fa; }}
  .summary-pill {{
    display: inline-block; padding: 2px 10px; border-radius: 12px;
    font-size: 0.8rem; font-weight: 600; margin-right: 6px;
  }}
</style>
</head>
<body>
<div class="container py-3" style="max-width: 620px">

  <h5 class="mb-1">⚾ MLB Weather Totals — Origination</h5>
  <div class="text-muted mb-2" style="font-size:0.78rem">
    Updated: {gen_str}<br>
    {day1}: <strong>{flagged_today}</strong>/{total_today} flagged &nbsp;·&nbsp;
    {day2}: <strong>{flagged_tomorrow}</strong>/{total_tomorrow} flagged
  </div>
  <div class="alert alert-secondary py-1 px-2 mb-3" style="font-size:0.75rem">
    🎯 = originated edge ≥ {EDGE_THRESHOLD_RUNS} runs vs the opener, from factors with
    historical vs-opener evidence at that park (★★★ t≥2.0 · ★★ t≥1.5).<br>
    Temp/wind edges get priced by close — <strong>bet early</strong>. Humidity edges are
    never priced — timing flexible.<br>
    ⚠️ Best used alongside the sharp-money process; model is 2023–2025 in-sample,
    not yet walk-forward validated.
  </div>

  {card_html}

  <div class="text-muted text-center mt-3" style="font-size:0.72rem">
    Model v2: per-park OLS vs OPENING line (gap_open ~ temp + relh + eff_wind) · 5,393 games 2023–2025<br>
    fair total = market + Σ β×(forecast − park baseline), confirmed factors only<br>
    CF bearings approximate · Roof status estimated from temp/precip · Totals: The Odds API (median across books)<br>
    Opener predictor: walk-forward MAE 0.45
  </div>
</div>
</body>
</html>'''


# ── Main ──────────────────────────────────────────────────────────────────────
DOCS_DIR = "docs"

def main():
    now_utc = datetime.datetime.now(UTC)
    today   = now_utc.astimezone(ZoneInfo("America/Chicago")).date()
    tomorrow = today + datetime.timedelta(days=1)

    print(f"MLB Weather Flag Tool v3 (opener tripwire) — {now_utc.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Running for: {today.strftime('%A %Y-%m-%d')}  {tomorrow.strftime('%A %Y-%m-%d')}\n")

    market_totals = fetch_market_totals()
    log_rows = load_totals_log(DOCS_DIR)
    print(f"  Totals log loaded: {len(log_rows)} row(s)")
    all_cards = []
    log_candidates = []   # games this run with a market total, for first-seen logging

    for is_first, date_obj in [(True, today), (False, tomorrow)]:
        date_label = date_obj.strftime("%A")   # "Thursday", "Friday", etc.
        date_str   = date_obj.strftime("%Y-%m-%d")
        games = fetch_schedule(date_str)
        print(f"{date_label} ({date_str}): {len(games)} scheduled game(s)")

        for game in games:
            venue = game["venue"]
            park  = PARKS.get(venue)

            card = {
                "venue": venue, "away": game["away"], "home": game["home"],
                "date_label": date_label, "is_first": is_first,
                "game_dt_str": game["game_dt_str"],
                "park_in_model": park is not None,
                "dome": game.get("venue_raw", venue) in DOME_VENUES or venue in DOME_VENUES,
                "temp": None, "relh": None, "wind_speed": None,
                "wind_dir": None, "wind_eff": 0.0, "game_local": None,
                "precip_prob": None,
                "signals": [],
                "orig": None, "market_total": None, "opener_pred": None,
                "notes": park.get("notes", "") if park else "",
            }

            if park:
                hourly = fetch_hourly_wx(park["lat"], park["lon"], park["tz"])
                wx, game_local = wx_at_gametime(hourly, game["game_dt_str"], park["tz"], game["date"])

                if wx and game_local:
                    temp       = wx["temp"]
                    relh       = wx["relh"]
                    wspd       = wx["windspeed"]
                    wdir       = wx["winddir"]
                    precip_prob = wx.get("precip_prob")
                    we         = calc_eff_wind(wspd, wdir, park["cf_bearing"])

                    orig = originate(park, temp, relh, we)
                    mkt = lookup_market_total(market_totals, game["home"],
                                              game["away"], game["game_dt_str"])
                    opener_pred = None
                    if orig and orig["flag"]:
                        opener_pred = predict_opener(game["home"], game["away"], venue, log_rows)
                    card.update({
                        "temp": temp, "relh": relh,
                        "wind_speed": wspd, "wind_dir": wdir,
                        "wind_eff": we, "game_local": game_local,
                        "precip_prob": precip_prob,
                        "signals": detect_signals(park, temp, relh, we),
                        "orig": orig, "market_total": mkt,
                        "opener_pred": opener_pred,
                    })
                    flag = "🎯" if (orig and orig["flag"]) else ("✓" if card["signals"] else "·")
                    orig_str = ""
                    if orig and orig["flag"]:
                        lean = "OVER" if orig["adj_conf"] > 0 else "UNDER"
                        mkt_s = f" vs mkt {mkt:g}" if mkt is not None else ""
                        orig_str = f"  ⇒ {lean} {orig['adj_conf']:+.2f}r{mkt_s}"
                    print(f"  {flag} {game['away'][:3]}@{game['home'][:3]}"
                          f"  {temp:.0f}°F  {relh:.0f}%RH  {we:+.1f}mph{orig_str}")

                    if mkt is not None:
                        log_candidates.append({
                            "date": date_str, "home": game["home"], "away": game["away"],
                            "venue": venue, "total": mkt,
                        })
            else:
                print(f"  · {game['away'][:3]}@{game['home'][:3]}"
                      f"  {venue} (not in model)")

            all_cards.append(card)

    # Sort: originated edges first (by |edge|), then bucket-flagged, then time
    def sort_key(c):
        orig = c.get("orig")
        edge = abs(orig["adj_conf"]) if (orig and orig["flag"]) else 0.0
        sig_score = sum(s[1] for s in c["signals"])
        gt = c["game_local"].timestamp() if c["game_local"] else 9e9
        return (-edge, -sig_score, gt)

    all_cards.sort(key=sort_key)

    os.makedirs(DOCS_DIR, exist_ok=True)
    html = generate_html(all_cards, now_utc)
    with open(os.path.join(DOCS_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)

    log_rows = update_totals_log(log_rows, log_candidates, now_utc, DOCS_DIR)
    print(f"  Totals log: {len(log_rows)} row(s) total after this run")

    flagged = sum(1 for c in all_cards if c.get("orig") and c["orig"]["flag"])
    print(f"\nOutput: docs/index.html  ({len(all_cards)} games, {flagged} originated edge(s))")


if __name__ == "__main__":
    main()
