#!/usr/bin/env python3
"""
MLB Weather Flag Tool
Runs via GitHub Actions at 5am and 5pm CDT (10:00 and 22:00 UTC).
Outputs docs/index.html — served via GitHub Pages.

Data sources (no API keys required):
  MLB schedule: statsapi.mlb.com
  Weather:      api.open-meteo.com
"""

import json, math, os, sys, datetime, requests
from zoneinfo import ZoneInfo

UTC = ZoneInfo("UTC")

# ── Park data ─────────────────────────────────────────────────────────────────
# Coefficients: per-park OLS gap ~ temp + relh + eff_wind (June 2026 model)
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
        "temp_b": -0.0207, "temp_t": -0.82,
        "relh_b": -0.0330, "relh_t": -2.10,
        "wind_b":  0.0093, "wind_t":  0.17,
    },
    "Yankee Stadium": {
        "lat": 40.8296, "lon": -73.9262, "cf_bearing": 355, "tz": "America/New_York",
        "temp_b":  0.0445, "temp_t":  1.82,
        "relh_b":  0.0059, "relh_t":  0.35,
        "wind_b":  0.0168, "wind_t":  0.44,
    },
    "Citi Field": {
        "lat": 40.7571, "lon": -73.8458, "cf_bearing": 40, "tz": "America/New_York",
        "temp_b":  0.0448, "temp_t":  1.62,
        "relh_b": -0.0356, "relh_t": -2.11,
        "wind_b":  0.0318, "wind_t":  0.74,
    },
    "Oriole Park at Camden Yards": {
        "lat": 39.2838, "lon": -76.6218, "cf_bearing": 30, "tz": "America/New_York",
        "temp_b":  0.0211, "temp_t":  0.70,
        "relh_b": -0.0208, "relh_t": -1.33,
        "wind_b":  0.0216, "wind_t":  0.43,
    },
    "Nationals Park": {
        "lat": 38.8730, "lon": -77.0074, "cf_bearing": 355, "tz": "America/New_York",
        "temp_b":  0.0414, "temp_t":  1.46,
        "relh_b":  0.0040, "relh_t":  0.24,
        "wind_b": -0.0955, "wind_t": -1.95,
    },
    "Kauffman Stadium": {
        "lat": 39.0517, "lon": -94.4803, "cf_bearing": 10, "tz": "America/Chicago",
        "temp_b": -0.0047, "temp_t": -0.17,
        "relh_b":  0.0349, "relh_t":  1.92,
        "wind_b":  0.1497, "wind_t":  1.66,
    },
    "Truist Park": {
        "lat": 33.8908, "lon": -84.4678, "cf_bearing": 15, "tz": "America/New_York",
        "temp_b":  0.0191, "temp_t":  0.53,
        "relh_b":  0.0219, "relh_t":  1.13,
        "wind_b": -0.0791, "wind_t": -1.21,
    },
    "Oracle Park": {
        "lat": 37.7786, "lon": -122.3893, "cf_bearing": 315, "tz": "America/Los_Angeles",
        "temp_b":  0.0369, "temp_t":  0.53,
        "relh_b": -0.0435, "relh_t": -1.56,
        "wind_b": -0.0224, "wind_t": -0.54,
    },
    "Wrigley Field": {
        "lat": 41.9484, "lon": -87.6553, "cf_bearing": 50, "tz": "America/Chicago",
        "temp_b": -0.0260, "temp_t": -1.08,
        "relh_b":  0.0030, "relh_t":  0.16,
        "wind_b":  0.0806, "wind_t":  2.27,
    },
    "Target Field": {
        "lat": 44.9817, "lon": -93.2781, "cf_bearing": 5, "tz": "America/Chicago",
        "temp_b":  0.0349, "temp_t":  1.43,
        "relh_b":  0.0437, "relh_t":  2.27,
        "wind_b":  0.0114, "wind_t":  0.27,
    },
    "T-Mobile Park": {
        "lat": 47.5914, "lon": -122.3325, "cf_bearing": 345, "tz": "America/Los_Angeles",
        "temp_b":  0.0431, "temp_t":  1.24,
        "relh_b":  0.0299, "relh_t":  1.14,
        "wind_b": -0.0774, "wind_t": -1.18,
    },
    "Rogers Centre": {
        "lat": 43.6414, "lon": -79.3894, "cf_bearing": 355, "tz": "America/Toronto",
        "temp_b":  0.0075, "temp_t":  0.12,
        "relh_b":  0.0151, "relh_t":  0.60,
        "wind_b":  0.0940, "wind_t":  2.16,
        "notes": "Verify roof is OPEN before using signal.",
    },
    "PNC Park": {
        "lat": 40.4469, "lon": -80.0057, "cf_bearing": 30, "tz": "America/New_York",
        "temp_b":  0.0347, "temp_t":  1.28,
        "relh_b": -0.0026, "relh_t": -0.15,
        "wind_b":  0.0428, "wind_t":  0.91,
    },
    "Comerica Park": {
        "lat": 42.3390, "lon": -83.0485, "cf_bearing": 355, "tz": "America/Detroit",
        "temp_b":  0.0285, "temp_t":  1.09,
        "relh_b":  0.0031, "relh_t":  0.18,
        "wind_b": -0.0200, "wind_t": -0.52,
    },
    "Great American Ball Park": {
        "lat": 39.0979, "lon": -84.5079, "cf_bearing": 350, "tz": "America/New_York",
        "temp_b": -0.0067, "temp_t": -0.24,
        "relh_b":  0.0034, "relh_t":  0.20,
        "wind_b":  0.0778, "wind_t":  1.45,
        "notes": "GABP: humidity signals unreliable (possibly mispriced baseline). Wind signal only.",
    },
    "American Family Field": {
        "lat": 43.0280, "lon": -87.9712, "cf_bearing": 15, "tz": "America/Chicago",
        "temp_b":  0.0505, "temp_t":  1.03,
        "relh_b":  0.0130, "relh_t":  0.54,
        "wind_b": -0.0744, "wind_t": -1.12,
    },
    "Dodger Stadium": {
        "lat": 34.0739, "lon": -118.2400, "cf_bearing": 30, "tz": "America/Los_Angeles",
        "temp_b": -0.0536, "temp_t": -1.24,
        "relh_b": -0.0128, "relh_t": -0.53,
        "wind_b":  0.0575, "wind_t":  0.73,
    },
    "Citizens Bank Park": {
        "lat": 39.9061, "lon": -75.1665, "cf_bearing": 15, "tz": "America/New_York",
        "temp_b": -0.0221, "temp_t": -0.72,
        "relh_b":  0.0139, "relh_t":  0.86,
        "wind_b": -0.0534, "wind_t": -1.34,
    },
    "Coors Field": {
        "lat": 39.7559, "lon": -104.9942, "cf_bearing": 350, "tz": "America/Denver",
        "temp_b":  0.0288, "temp_t":  0.88,
        "relh_b":  0.0101, "relh_t":  0.44,
        "wind_b":  0.0394, "wind_t":  0.61,
    },
    "Progressive Field": {
        "lat": 41.4962, "lon": -81.6852, "cf_bearing": 5, "tz": "America/New_York",
        "temp_b":  0.0272, "temp_t":  1.10,
        "relh_b":  0.0097, "relh_t":  0.53,
        "wind_b":  0.0028, "wind_t":  0.06,
    },
    "Petco Park": {
        "lat": 32.7076, "lon": -117.1570, "cf_bearing": 320, "tz": "America/Los_Angeles",
        "temp_b":  0.0296, "temp_t":  0.64,
        "relh_b": -0.0037, "relh_t": -0.12,
        "wind_b": -0.0098, "wind_t": -0.08,
    },
}

# Normalize MLB Stats API venue names → PARKS dict keys
VENUE_MAP = {
    "Nationals Park at Half Street":   "Nationals Park",
    "Rate Field":                      "Guaranteed Rate Field",
    "Guaranteed Rate Field":           "Guaranteed Rate Field",  # keep
    "LoanDepot Park":                  "LoanDepot Park",
    "loanDepot park":                  "LoanDepot Park",
}

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
            "hourly": "temperature_2m,relativehumidity_2m,windspeed_10m,winddirection_10m",
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
            "temp":      h["temperature_2m"][i]      if i < len(h.get("temperature_2m", [])) else None,
            "relh":      h["relativehumidity_2m"][i] if i < len(h.get("relativehumidity_2m", [])) else None,
            "windspeed": h["windspeed_10m"][i]        if i < len(h.get("windspeed_10m", [])) else None,
            "winddir":   h["winddirection_10m"][i]    if i < len(h.get("winddirection_10m", [])) else None,
        }
    _wx_cache[key] = result
    return result

def wx_at_gametime(hourly, game_dt_str, tz_str):
    """Return (weather_dict, game_local_datetime) for the hour closest to game start."""
    if not game_dt_str:
        return None, None
    try:
        game_dt = datetime.datetime.fromisoformat(game_dt_str.replace("Z", "+00:00"))
        local = game_dt.astimezone(ZoneInfo(tz_str))
        # Round to nearest hour
        target = local.replace(minute=0, second=0, microsecond=0)
        if local.minute >= 30:
            target += datetime.timedelta(hours=1)
        for delta in [0, 1, -1, 2, -2]:
            key = (target + datetime.timedelta(hours=delta)).strftime("%Y-%m-%dT%H:%M")
            if key in hourly:
                return hourly[key], local
    except Exception as e:
        print(f"  Gametime lookup error: {e}", file=sys.stderr)
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

# ── HTML generation ───────────────────────────────────────────────────────────
def render_card(card):
    venue     = card["venue"]
    away      = card["away"]
    home      = card["home"]
    label     = card["date_label"]
    signals   = card["signals"]
    game_local = card.get("game_local")
    notes     = card.get("notes", "")

    # Card accent color
    if not signals:
        accent = "#adb5bd"  # gray
    elif any(s[2] == "OVER" for s in signals):
        accent = "#198754"  # Bootstrap green
    else:
        accent = "#dc3545"  # Bootstrap red

    # Time string
    time_str = game_local.strftime("%-I:%M %p %Z") if game_local else "TBD"

    # Day badge
    day_badge_color = "primary" if label == "TODAY" else "secondary"
    day_badge = f'<span class="badge text-bg-{day_badge_color} ms-1">{label}</span>'

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
    wx_line = " &nbsp;·&nbsp; ".join(wx_parts) if wx_parts else \
              "<em class='text-muted'>weather unavailable</em>"

    # Signal badges
    badge_html = ""
    for (factor, abs_t, direction, cond) in signals:
        color = "success" if direction == "OVER" else "danger"
        stars = "★★ " if abs_t >= 2.0 else "★ " if abs_t >= 1.5 else ""
        badge_html += (f'<span class="badge text-bg-{color} me-1 mb-1">'
                       f'{stars}{direction} · {factor.lower()} · t={abs_t:.1f}</span>')

    if not badge_html:
        badge_html = '<span class="text-muted" style="font-size:0.8rem">No signal flagged</span>'

    # Signal detail list
    detail_lines = ""
    for (factor, abs_t, direction, cond) in signals:
        detail_lines += f'<li style="font-size:0.8rem">{cond} → {direction} lean (t={abs_t:.2f})</li>'
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
      <div class="mt-1">{badge_html}</div>
      {detail_html}
      {notes_html}
    </div>
  </div>'''


def generate_html(cards, generated_at):
    flagged_today    = sum(1 for c in cards if c["signals"] and c["date_label"] == "TODAY")
    flagged_tomorrow = sum(1 for c in cards if c["signals"] and c["date_label"] == "TOMORROW")
    total_today      = sum(1 for c in cards if c["date_label"] == "TODAY")
    total_tomorrow   = sum(1 for c in cards if c["date_label"] == "TOMORROW")
    gen_str = generated_at.strftime("%b %-d, %Y · %-I:%M %p UTC")

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

  <h5 class="mb-1">⚾ MLB Weather Flags</h5>
  <div class="text-muted mb-2" style="font-size:0.78rem">
    Updated: {gen_str}<br>
    Today: <strong>{flagged_today}</strong>/{total_today} flagged &nbsp;·&nbsp;
    Tomorrow: <strong>{flagged_tomorrow}</strong>/{total_tomorrow} flagged
  </div>
  <div class="alert alert-secondary py-1 px-2 mb-3" style="font-size:0.75rem">
    ⚠️ Weather signals are <strong>confirmatory only</strong> — layer on top of
    a sharp-money signal. Never bet weather alone.
    ★★ = t≥2.0 &nbsp; ★ = t≥1.5
  </div>

  {card_html}

  <div class="text-muted text-center mt-3" style="font-size:0.72rem">
    Model: per-park OLS (gap ~ temp + relh + eff_wind) · 5,393 games 2023–2025<br>
    CF bearings approximate · Rogers Centre: verify roof open
  </div>
</div>
</body>
</html>'''


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now_utc = datetime.datetime.now(UTC)
    today   = now_utc.astimezone(ZoneInfo("America/Chicago")).date()
    tomorrow = today + datetime.timedelta(days=1)

    print(f"MLB Weather Flag Tool — {now_utc.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Running for: TODAY={today}  TOMORROW={tomorrow}\n")

    all_cards = []

    for date_label, date_obj in [("TODAY", today), ("TOMORROW", tomorrow)]:
        date_str = date_obj.strftime("%Y-%m-%d")
        games = fetch_schedule(date_str)
        print(f"{date_label} ({date_str}): {len(games)} scheduled game(s)")

        for game in games:
            venue = game["venue"]
            park  = PARKS.get(venue)

            card = {
                "venue": venue, "away": game["away"], "home": game["home"],
                "date_label": date_label, "game_dt_str": game["game_dt_str"],
                "park_in_model": park is not None,
                "temp": None, "relh": None, "wind_speed": None,
                "wind_dir": None, "wind_eff": 0.0, "game_local": None,
                "signals": [],
                "notes": park.get("notes", "") if park else "",
            }

            if park:
                hourly = fetch_hourly_wx(park["lat"], park["lon"], park["tz"])
                wx, game_local = wx_at_gametime(hourly, game["game_dt_str"], park["tz"])

                if wx and game_local:
                    temp = wx["temp"]
                    relh = wx["relh"]
                    wspd = wx["windspeed"]
                    wdir = wx["winddir"]
                    we   = calc_eff_wind(wspd, wdir, park["cf_bearing"])

                    card.update({
                        "temp": temp, "relh": relh,
                        "wind_speed": wspd, "wind_dir": wdir,
                        "wind_eff": we, "game_local": game_local,
                        "signals": detect_signals(park, temp, relh, we),
                    })
                    flag = "✓" if card["signals"] else "·"
                    sig_str = " | ".join(f"{s[2]} ({s[0].lower()}, t={s[1]:.1f})"
                                        for s in card["signals"])
                    print(f"  {flag} {game['away'][:3]}@{game['home'][:3]}"
                          f"  {temp:.0f}°F  {relh:.0f}%RH  {we:+.1f}mph"
                          + (f"  → {sig_str}" if sig_str else ""))
            else:
                print(f"  · {game['away'][:3]}@{game['home'][:3]}"
                      f"  {venue} (not in model)")

            all_cards.append(card)

    # Sort: flagged games first (by total |t| weight), then by game time
    def sort_key(c):
        sig_score = sum(s[1] for s in c["signals"])
        gt = c["game_local"].timestamp() if c["game_local"] else 9e9
        return (-sig_score, gt)

    all_cards.sort(key=sort_key)

    os.makedirs("docs", exist_ok=True)
    html = generate_html(all_cards, now_utc)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html)

    flagged = sum(1 for c in all_cards if c["signals"])
    print(f"\nOutput: docs/index.html  ({len(all_cards)} games, {flagged} flagged)")


if __name__ == "__main__":
    main()
