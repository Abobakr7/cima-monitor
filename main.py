import json
import os
import re
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

HEADING = re.compile(r"^h[1-6]$")

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
    """One retry layer only (the adapter), so waits stay bounded."""
    session = requests.Session()

    session.headers.update({
        "User-Agent": "curl/8.5.0",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })

    retry = Retry(
        total=4,
        backoff_factor=3,  # sleeps ~0s, 6s, 12s, 24s between tries
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
    print(f"Fetching {url}")

    response = session.get(url, timeout=(10, 60))
    response.raise_for_status()

    return BeautifulSoup(response.text, "html.parser")


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
            dates.add(datetime.strptime(raw_date, "%Y%m%d").date())
        except ValueError:
            continue

    return sorted(dates)


def find_movie_booking(soup, aliases):
    """
    Find a real booking link inside the matching movie's own card.

    Starting from the movie's heading, climb to the smallest
    container that holds a /booking/ link. If that container also
    holds another movie's heading, we have climbed too far (into a
    multi-movie container), so give up on this heading rather than
    return another movie's link.
    """

    wanted = {normalize_title(alias) for alias in aliases}

    # Each movie card has a "City Centre Almaza" sub-heading. It is
    # not another movie, so it must not trigger the "too far up" check.
    ignored = wanted | {normalize_title(CINEMA_NAME)}

    for heading in soup.find_all(HEADING):

        if normalize_title(heading.get_text(" ", strip=True)) not in wanted:
            continue

        for container in heading.parents:

            if container.name in ("body", "html", "[document]"):
                break

            links = []

            for anchor in container.select("a[href]"):
                url = urljoin(BASE_URL, anchor["href"])

                if "/booking/" in urlparse(url).path:
                    links.append((anchor, url))

            if not links:
                continue  # card not reached yet, keep climbing

            other_titles = [
                h for h in container.find_all(HEADING)
                if normalize_title(h.get_text(" ", strip=True)) not in ignored
            ]

            if other_titles:
                break  # too far up: container holds other movies

            anchor, booking_url = links[0]

            return {
                "time": anchor.get_text(" ", strip=True)
                or "Showtime available",
                "booking_url": booking_url,
            }

    return None


def check_vox(movies):
    """Check only the given movies. Returns {movie_key: result}."""
    session = create_session()

    try:
        print("Fetching VOX showtime page...")

        landing_page = get_page(session, SHOWTIMES_URL)
        dates = get_available_dates(landing_page)

        # Fail loudly if the markup changed, instead of silently
        # reporting "nothing found" forever.
        if not dates:
            raise RuntimeError(
                "No dates found in the VOX date selector. "
                "The page structure may have changed or the request "
                "was blocked."
            )

        print(f"VOX published {len(dates)} dates.")

        results = {}
        pending = {movie["key"]: movie for movie in movies}

        fetched = 0
        failed = 0

        for show_date in dates:

            relevant = [
                movie
                for movie in pending.values()
                if show_date >= movie["target"]
            ]

            if not relevant:
                continue

            url = (
                f"{BASE_URL}/showtimes"
                f"?c={CINEMA}&d={show_date.strftime('%Y%m%d')}"
            )

            print(f"Checking {show_date}...")

            try:
                soup = get_page(session, url)
            except requests.exceptions.RequestException as e:
                failed += 1
                print(f"Skipping {show_date}: {e}")
                continue

            fetched += 1

            for movie in relevant:
                booking = find_movie_booking(soup, movie["aliases"])

                if booking:
                    results[movie["key"]] = {
                        "movie": movie["name"],
                        "target_date": movie["target"].isoformat(),
                        "available_date": show_date.isoformat(),
                        **booking,
                    }

                    # Only one bookable session is needed.
                    pending.pop(movie["key"])

            if not pending:
                break

        if failed and not fetched:
            raise RuntimeError("Every date page request failed.")

        return results

    finally:
        session.close()


def send_telegram(message):
    # token = os.environ["TELEGRAM_BOT_TOKEN"]
    # chat_id = os.environ["TELEGRAM_CHAT_ID"]
    token = "9120847968:AAGfPjQj5zZdW3_hqGMUOHqcF3_l7TswQ_A"
    chat_id = "1082410156"

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

    available = check_vox(pending_movies)

    if not available:
        print("No new bookable sessions found.")
        return

    errors = []

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

        try:
            send_telegram(message)
        except Exception as e:
            # Don't mark as notified; it will retry next run.
            # Keep going so one failure doesn't block other movies.
            print(f"Failed to notify for {item['movie']}: {e}")
            errors.append(item["movie"])
            continue

        # Only save the alert after Telegram confirms success.
        state[key] = {
            "notified": True,
            "available_date": item["available_date"],
            "booking_url": item["booking_url"],
            "notified_at": datetime.now(timezone.utc).isoformat(),
        }

        save_state(state)

        print(f"Notification sent: {item['movie']}")

    if errors:
        raise RuntimeError(f"Notifications failed for: {', '.join(errors)}")


if __name__ == "__main__":
    main()