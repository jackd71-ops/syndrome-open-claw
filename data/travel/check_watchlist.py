#!/usr/bin/env python3
import json, os, sys, requests
from datetime import datetime
from fli.search import SearchFlights, SearchDates
from fli.models import (
    Airport, PassengerInfo, SeatType, MaxStops, SortBy,
    TripType, FlightSearchFilters, FlightSegment, DateSearchFilters, PriceLimit
)

WATCHLIST_PATH = "/opt/openclaw/data/travel/watchlist.json"
SECRETS_PATH = "/opt/openclaw/secrets.json"
TELEGRAM_CHAT_ID = "1163684840"

def get_telegram_token():
    try:
        with open(SECRETS_PATH) as f:
            return json.load(f).get("TELEGRAM_TOKEN", "")
    except Exception:
        return os.environ.get("TELEGRAM_BOT_TOKEN", "")
STOPS_MAP = {"ANY": MaxStops.ANY, "NON_STOP": MaxStops.NON_STOP, "ONE_STOP_OR_FEWER": MaxStops.ONE_STOP_OR_FEWER}

def send_telegram(message):
    token = get_telegram_token()
    if not token:
        print(f"[TELEGRAM] {message}")
        return
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                  json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"})

def check_route(route):
    fly_from = Airport[route["fly_from"]]
    fly_to = Airport[route["fly_to"]]
    adults = route.get("adults", 1)
    filters = DateSearchFilters(
        passenger_info=PassengerInfo(adults=adults),
        flight_segments=[FlightSegment(
            departure_airport=[[fly_from, 0]],
            arrival_airport=[[fly_to, 0]],
            travel_date=route["date_from"],
        )],
        from_date=route["date_from"],
        to_date=route["date_to"],
    )
    results = SearchDates().search(filters)
    if not results:
        return None
    results.sort(key=lambda x: x.price)
    return results[0].price, results[0].date[0]

def main():
    with open(WATCHLIST_PATH) as f:
        data = json.load(f)
    now = datetime.utcnow().isoformat()
    for route in data.get("routes", []):
        try:
            result = check_route(route)
            if not result:
                print(f"[{route['id']}] No results.")
                continue
            price, best_date = result
            route["last_checked"] = now
            route["last_price"] = price
            print(f"[{route['id']}] £{price} on {best_date} (target £{route['max_price_gbp']})")
            if price <= route["max_price_gbp"] and not route.get("alerted"):
                send_telegram(
                    f"✈️ <b>Price Alert: {route['label']}</b>\n\n"
                    f"📍 {route['fly_from']} → {route['fly_to']}\n"
                    f"💷 £{price} (target £{route['max_price_gbp']})\n"
                    f"📅 Best date: {best_date}\n"
                    f"👥 {route.get('adults',1)} adult(s)\n\n"
                    f"🔎 https://www.google.com/flights\n"
                    f"(Checked: {now[:16]} UTC)"
                )
                route["alerted"] = True
            elif price > route["max_price_gbp"] * 1.1:
                route["alerted"] = False
        except Exception as e:
            print(f"[{route['id']}] Error: {e}")
    with open(WATCHLIST_PATH, "w") as f:
        json.dump(data, f, indent=2)

if __name__ == "__main__":
    main()
