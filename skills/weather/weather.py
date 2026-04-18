#!/usr/bin/env python3
"""
Met Office DataHub — 2-day morning weather briefing for Leyland PR26.
Today + tomorrow, except Friday = Saturday + Sunday.
Exits 1 and prints WEATHER_UNAVAILABLE if API fails — never estimates.
"""
import sys
import json
import subprocess
from datetime import datetime, timedelta
import zoneinfo

TZ           = zoneinfo.ZoneInfo("Europe/London")
LAT          = "53.706579"
LON          = "-2.712051"
API_KEY_FILE = "/home/node/.openclaw/secrets.json"
ENDPOINT     = (
    "https://data.hub.api.metoffice.gov.uk/sitespecific/v0/point/hourly"
    f"?dataSource=BD1&excludeParameterMetadata=true"
    f"&includeLocationName=true&latitude={LAT}&longitude={LON}"
)

WEATHER_CODES = {
    -1: "Trace rain 🌦️",   0: "Clear ☀️",          1: "Sunny ☀️",
     2: "Partly cloudy ⛅",  3: "Partly cloudy ⛅",   7: "Cloudy ☁️",
     8: "Overcast ☁️",      9: "Light showers 🌦️",  10: "Light showers 🌦️",
    11: "Drizzle 🌦️",      12: "Light rain 🌧️",    13: "Heavy showers 🌧️",
    14: "Heavy showers 🌧️", 15: "Heavy rain 🌧️",   16: "Sleet 🌨️",
    17: "Sleet 🌨️",        18: "Sleet 🌨️",         19: "Hail 🌨️",
    20: "Hail 🌨️",         21: "Hail 🌨️",          22: "Light snow 🌨️",
    23: "Light snow 🌨️",   24: "Light snow 🌨️",    25: "Heavy snow ❄️",
    26: "Heavy snow ❄️",    27: "Heavy snow ❄️",     28: "Thunder ⛈️",
    29: "Thunder ⛈️",      30: "Thunder ⛈️",
}

PERIODS = {
    "morning":   (6,  11),
    "afternoon": (12, 17),
    "evening":   (18, 22),
}


def load_api_key():
    with open(API_KEY_FILE) as f:
        return json.load(f)["MET_OFFICE_API_KEY"]


def fetch_data(api_key):
    result = subprocess.run(
        ["curl", "-sf", "--max-time", "15",
         "-H", f"apikey: {api_key}", ENDPOINT],
        capture_output=True, text=True
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def period_summary(hours, target_date, period_name):
    """Summarise a specific period of the day."""
    start_h, end_h = PERIODS[period_name]
    period_hours = [
        h for h in hours
        if datetime.fromisoformat(h["time"].replace("Z", "+00:00"))
               .astimezone(TZ).date() == target_date
        and start_h <= datetime.fromisoformat(h["time"].replace("Z", "+00:00"))
                           .astimezone(TZ).hour <= end_h
    ]
    if not period_hours:
        return None

    temps  = [h["screenTemperature"] for h in period_hours]
    feels  = [h["feelsLikeTemperature"] for h in period_hours]
    codes  = [h["significantWeatherCode"] for h in period_hours]
    precip = [h.get("probOfPrecipitation", 0) for h in period_hours]
    wind   = [h.get("windSpeed10m", 0) for h in period_hours]

    temp_min  = round(min(temps))
    temp_max  = round(max(temps))
    feels_min = round(min(feels))
    feels_max = round(max(feels))
    max_rain  = max(precip)
    avg_wind  = round(sum(wind) / len(wind) * 3.6)  # m/s → km/h

    # Dominant weather code, excluding night-specific codes
    day_codes = [c for c in codes if c not in (2, 9, 13, 16, 19, 22, 25, 28)]
    dominant  = max(set(day_codes or codes), key=(day_codes or codes).count)
    condition = WEATHER_CODES.get(dominant, "Mixed")

    temp_str  = f"{temp_min}°C" if temp_min == temp_max else f"{temp_min}–{temp_max}°C"
    feels_str = f"{feels_min}°C" if feels_min == feels_max else f"{feels_min}–{feels_max}°C"
    rain_str  = f", {max_rain:.0f}% rain chance" if max_rain >= 20 else ""
    wind_str  = f", {avg_wind}km/h winds" if avg_wind >= 20 else ""

    return f"{condition} {temp_str} (feels {feels_str}){rain_str}{wind_str}"


def summarise_day(hours, target_date, day_label):
    """Build full day summary across morning, afternoon and evening."""
    morning   = period_summary(hours, target_date, "morning")
    afternoon = period_summary(hours, target_date, "afternoon")
    evening   = period_summary(hours, target_date, "evening")

    if not any([morning, afternoon, evening]):
        return None

    lines = [f"*{day_label}*"]
    if morning:
        lines.append(f"  Morning:   {morning}")
    if afternoon:
        lines.append(f"  Afternoon: {afternoon}")
    if evening:
        lines.append(f"  Evening:   {evening}")
    return "\n".join(lines)


def main():
    today   = datetime.now(TZ).date()
    weekday = today.weekday()  # 0=Mon … 4=Fri

    if weekday == 4:  # Friday — show weekend instead
        day1, day2   = today + timedelta(1), today + timedelta(2)
        label1, label2 = "Saturday", "Sunday"
    else:
        day1, day2   = today, today + timedelta(1)
        label1 = today.strftime("%A")
        label2 = (today + timedelta(1)).strftime("%A")

    try:
        api_key = load_api_key()
    except (FileNotFoundError, KeyError):
        print("WEATHER_UNAVAILABLE")
        sys.exit(1)

    data = fetch_data(api_key)
    if not data:
        print("WEATHER_UNAVAILABLE")
        sys.exit(1)

    try:
        hours    = data["features"][0]["properties"]["timeSeries"]
        location = data["features"][0]["properties"]["location"]["name"]
    except (KeyError, IndexError):
        print("WEATHER_UNAVAILABLE")
        sys.exit(1)

    s1 = summarise_day(hours, day1, label1)
    s2 = summarise_day(hours, day2, label2)

    if not s1 or not s2:
        print("WEATHER_UNAVAILABLE")
        sys.exit(1)

    print(f"📍 {location} weather forecast:\n")
    print(s1)
    print()
    print(s2)


if __name__ == "__main__":
    main()
