#!/usr/bin/env python3
"""
Met Office DataHub — morning weather briefing for Leyland PR26.
Mon–Thu: today + tomorrow (hourly data).
Friday:  today + Saturday (hourly) + Sunday (three-hourly fallback).
Exits 1 and prints WEATHER_UNAVAILABLE if API fails — never estimates.
"""
import sys
import json
import os
import subprocess
import urllib.request
from datetime import datetime, timedelta
import zoneinfo

TZ           = zoneinfo.ZoneInfo("Europe/London")
LAT          = "53.706579"
LON          = "-2.712051"
API_KEY_FILE = "/home/node/.openclaw/secrets.json"
JOB_ID       = "134bc9e9-2177-4408-ad8d-9e060dbdf546"
TELEGRAM_CHAT_ID = "1163684840"

ENDPOINT_HOURLY = (
    "https://data.hub.api.metoffice.gov.uk/sitespecific/v0/point/hourly"
    f"?dataSource=BD1&excludeParameterMetadata=true"
    f"&includeLocationName=true&latitude={LAT}&longitude={LON}"
)
ENDPOINT_3H = (
    "https://data.hub.api.metoffice.gov.uk/sitespecific/v0/point/three-hourly"
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


def _load_telegram_token() -> str:
    try:
        with open(API_KEY_FILE) as f:
            return json.load(f).get("TELEGRAM_TOKEN", "")
    except Exception:
        return ""


def _send_telegram(text: str) -> bool:
    token = _load_telegram_token()
    if not token:
        print("TELEGRAM_TOKEN not found", file=sys.stderr)
        return False
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }).encode()
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.load(resp)
            return result.get("ok", False)
    except Exception as e:
        print(f"Telegram error: {e}", file=sys.stderr)
        return False


def _write_job_status(job_id: str) -> None:
    path = f"/home/node/.openclaw/workspace/data/job-status/{job_id}.json"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({
            "status": "ok",
            "job": "Daily weather - Leyland PR26",
            "completed_at": datetime.now(zoneinfo.ZoneInfo("UTC")).isoformat(),
        }, f)


def load_api_key():
    with open(API_KEY_FILE) as f:
        return json.load(f)["MET_OFFICE_API_KEY"]


def fetch_data(api_key, endpoint):
    result = subprocess.run(
        ["curl", "-sf", "--max-time", "15",
         "-H", f"apikey: {api_key}", endpoint],
        capture_output=True, text=True
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _extract(data):
    try:
        hours    = data["features"][0]["properties"]["timeSeries"]
        location = data["features"][0]["properties"]["location"]["name"]
        return hours, location
    except (KeyError, IndexError):
        return None, None


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

    # Hourly: single screenTemperature per slot
    # Three-hourly: min/max per slot, feelsLikeTemp
    if "screenTemperature" in period_hours[0]:
        temps_min = temps_max = [h["screenTemperature"] for h in period_hours]
        feels_vals = [h["feelsLikeTemperature"] for h in period_hours]
    else:
        temps_min = [h["minScreenAirTemp"] for h in period_hours]
        temps_max = [h["maxScreenAirTemp"] for h in period_hours]
        feels_vals = [h["feelsLikeTemp"] for h in period_hours]

    codes  = [h["significantWeatherCode"] for h in period_hours]
    precip = [h.get("probOfPrecipitation", 0) for h in period_hours]
    wind   = [h.get("windSpeed10m", 0) for h in period_hours]

    temp_min  = round(min(temps_min))
    temp_max  = round(max(temps_max))
    feels_min = round(min(feels_vals))
    feels_max = round(max(feels_vals))
    max_rain  = max(precip)
    avg_wind  = round(sum(wind) / len(wind) * 3.6)  # m/s → km/h

    day_codes = [c for c in codes if c not in (2, 9, 13, 16, 19, 22, 25, 28)]
    dominant  = max(set(day_codes or codes), key=(day_codes or codes).count)
    condition = WEATHER_CODES.get(dominant, "Mixed")

    temp_str  = f"{temp_min}°C" if temp_min == temp_max else f"{temp_min}–{temp_max}°C"
    feels_str = f"{feels_min}°C" if feels_min == feels_max else f"{feels_min}–{feels_max}°C"
    rain_str  = f", {max_rain:.0f}% rain chance" if max_rain >= 20 else ""
    wind_str  = f", {avg_wind}km/h winds" if avg_wind >= 20 else ""

    return f"{condition} {temp_str} (feels {feels_str}){rain_str}{wind_str}"


def summarise_day(hours, target_date, day_label):
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

    try:
        api_key = load_api_key()
    except (FileNotFoundError, KeyError):
        print("WEATHER_UNAVAILABLE")
        sys.exit(1)

    # Fetch hourly data (covers today + tomorrow)
    hourly_data = fetch_data(api_key, ENDPOINT_HOURLY)
    if not hourly_data:
        print("WEATHER_UNAVAILABLE")
        sys.exit(1)

    hourly_hours, location = _extract(hourly_data)
    if not hourly_hours:
        print("WEATHER_UNAVAILABLE")
        sys.exit(1)

    if weekday == 4:  # Friday — use hourly for Fri+Sat, three-hourly for Sun
        three_h_data = fetch_data(api_key, ENDPOINT_3H)
        if not three_h_data:
            print("WEATHER_UNAVAILABLE")
            sys.exit(1)
        three_h_hours, _ = _extract(three_h_data)
        if not three_h_hours:
            print("WEATHER_UNAVAILABLE")
            sys.exit(1)

        sunday = today + timedelta(2)
        days_and_hours = [
            (today,                "Friday",   hourly_hours),
            (today + timedelta(1), "Saturday", hourly_hours),
            (sunday,               "Sunday",   three_h_hours),
        ]
    else:
        days_and_hours = [
            (today,                today.strftime("%A"),                  hourly_hours),
            (today + timedelta(1), (today + timedelta(1)).strftime("%A"), hourly_hours),
        ]

    summaries = [summarise_day(hrs, d, lbl) for d, lbl, hrs in days_and_hours]

    if not all(summaries):
        print("WEATHER_UNAVAILABLE")
        sys.exit(1)

    message = f"📍 *{location} weather forecast:*\n\n" + "\n\n".join(summaries)
    print(message)

    if not _send_telegram(message):
        print("TELEGRAM_SEND_FAILED", file=sys.stderr)
        sys.exit(1)

    _write_job_status(JOB_ID)


if __name__ == "__main__":
    main()
