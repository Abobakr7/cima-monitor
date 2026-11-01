
import json
import os
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# Configuration
BASE_URL = "https://egy.voxcinemas.com"
SHOWTIMES_URL = f"{BASE_URL}/showtimes/city-centre-almaza"
CINEMA = "city-centre-almaza"
CINEMA_NAME = "City Centre Almaza"

STATE_FILE = Path("state.json")

MOVIES = [
    {
        "key": "avengers-doomsday",
        "name": "Avengers: Doomsday",
        "aliases": ["Avengers: Doomsday"],
        "target": date(2026, 12, 24),
    },
    {
        "key": "dune-part-three",
        "name": "Dune: Part Three",
        "aliases": ["Dune: Part Three", "Dune 3"],
        "target": date(2026, 12, 24),
    },
    {
        "key": "whalefall",
        "name": "Whalefall",
        "aliases": ["Whalefall"],
        "target": date(2026, 11, 5),
    },
    {
        "key": "clayface",
        "name": "Clayface",
        "aliases": ["Clayface"],
        "target": date(2026, 11, 5),
    },
    {
        "key": "resident-evil",
        "name": "Resident Evil",
        "aliases": ["Resident Evil"],
        "target": date(2026, 11, 5),
    },
]


def create_session():
    session = requests.Session()

    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; VoxMonitor/1.0)",
        "Accept-Language": "en-US,en;q=0.9",
    })

    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )

    adapter = HTTPAdapter(max_retries=retry)

    session.mount("https://", adapter)

    return session


def normalize_title(title):
    title = re.sub(r"\([^)]*\)", "", title)
    title = title.lower()
    title = re.sub(r"[^a-z0-9]+", " ", title)

    return title.strip()


def load_state():
    if not STATE_FILE.exists():
        return {}

    with STATE_FILE.open(encoding="utf-8") as file:
        return json.load(file)


def save_state(state):
    with STATE_FILE.open("w", encoding="utf-8") as file:
        json.dump(state, file, indent=4)


def get_page(session, url):
    max_attempts = 3

    for attempt in range(1, max_attempts + 1):
        try:
            print(f"Fetching {url} (attempt {attempt}/{max_attempts})")

            response = session.get(
                url,
                timeout=(10, 60)
            )

            response.raise_for_status()

            return BeautifulSoup(response.text, "html.parser")

        except requests.exceptions.RequestException as e:
            print(f"Request failed: {e}")

            if attempt == max_attempts:
                raise

            delay = 2 ** attempt * 5

            print(f"Retrying in {delay} seconds...")
            time.sleep(delay)


def get_available_dates(soup):
    """
    Extract dates currently published in VOX's
    City Centre Almaza showtime date selector.
    """

    dates = set()

    for anchor in soup.select("a[href]"):
        parsed = urlparse(urljoin(BASE_URL, anchor["href"]))
        params = parse_qs(parsed.query)

        cinema = params.get("c", [None])[0]
        raw_date = params.get("d", [None])[0]

        if cinema != CINEMA:
            continue

        if not raw_date or not re.fullmatch(r"\d{8}", raw_date):
            continue

        try:
            show_date = datetime.strptime(
                raw_date, "%Y%m%d"
            ).date()

            dates.add(show_date)

        except ValueError:
            continue

    return sorted(dates)


def find_movie_booking(soup, aliases):
    """
    Find a real booking link inside the matching movie's
    City Centre Almaza showtime section.

    A displayed movie title or plain text showtime is
    not sufficient.
    """

    wanted = {
        normalize_title(alias)
        for alias in aliases
    }

    for heading in soup.find_all(re.compile(r"^h[1-6]$")):

        title = normalize_title(
            heading.get_text(" ", strip=True)
        )

        if title not in wanted:
            continue

        # Search the movie card's ancestors.
        for container in heading.parents:

            if container.name in ("body", "html"):
                break

            text = container.get_text(" ", strip=True)

            if CINEMA_NAME.lower() not in text.lower():
                continue

            booking_links = []

            for anchor in container.select("a[href]"):
                url = urljoin(BASE_URL, anchor["href"])

                if "/booking/" in urlparse(url).path:
                    booking_links.append((anchor, url))

            if booking_links:
                anchor, booking_url = booking_links[0]

                return {
                    "time": anchor.get_text(
                        " ", strip=True
                    ) or "Showtime available",
                    "booking_url": booking_url,
                }

    return None


def check_vox():
    session = create_session()

    try:
        print("Fetching VOX showtime page...")

        landing_page = get_page(
            session,
            SHOWTIMES_URL
        )

        dates = get_available_dates(landing_page)

        print(f"VOX published {len(dates)} future dates.")

        results = {}

        pending = {
            movie["key"]: movie
            for movie in MOVIES
        }

        for show_date in dates:

            relevant_movies = [
                movie
                for movie in pending.values()
                if show_date >= movie["target"]
            ]

            if not relevant_movies:
                continue

            date_string = show_date.strftime("%Y%m%d")

            url = (
                f"{BASE_URL}/showtimes"
                f"?c={CINEMA}&d={date_string}"
            )

            print(f"Checking {show_date}...")

            soup = get_page(session, url)

            for movie in relevant_movies:

                booking = find_movie_booking(
                    soup,
                    movie["aliases"]
                )

                if booking:

                    results[movie["key"]] = {
                        "movie": movie["name"],
                        "target_date": movie["target"].isoformat(),
                        "available_date": show_date.isoformat(),
                        **booking,
                    }

                    # We only need one bookable session.
                    pending.pop(movie["key"])

        return results

    finally:
        session.close()


def send_telegram(message):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    response = requests.post(
        url,
        data={
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": True,
        },
        timeout=30,
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(f"Telegram error: {result}")


def main():
    state = load_state()

    pending_movies = [
        movie
        for movie in MOVIES
        if not state.get(movie["key"], {}).get("notified")
    ]

    if not pending_movies:
        print("All movies have already triggered alerts.")
        return

    available = check_vox()

    if not available:
        print("No new bookable sessions found.")
        return

    for key, item in available.items():

        message = (
            "🎬 VOX BOOKING AVAILABLE!\n\n"
            f"Movie: {item['movie']}\n"
            f"Cinema: {CINEMA_NAME}\n"
            f"Target date: {item['target_date']}\n"
            f"Available date: {item['available_date']}\n"
            f"Showtime: {item['time']}\n\n"
            f"Book now:\n{item['booking_url']}"
        )

        # Only save the alert after Telegram confirms success.
        send_telegram(message)

        state[key] = {
            "notified": True,
            "available_date": item["available_date"],
            "booking_url": item["booking_url"],
            "notified_at": datetime.now(
                timezone.utc
            ).isoformat(),
        }

        save_state(state)

        print(f"Notification sent: {item['movie']}")


if __name__ == "__main__":
    main()
