"""
Amazon price monitor.

Reads a list of product URLs from products.json, scrapes the current price
for each, compares it to the last known price stored in price_history.json,
and emails a summary via Gmail if anything changed (or on first run).

Environment variables required (set as GitHub Actions secrets):
    GMAIL_USER            - your gmail address, e.g. name@gmail.com
    GMAIL_APP_PASSWORD    - a 16-char Gmail App Password (not your normal password)
    NOTIFY_TO             - address to send alerts to (can be same as GMAIL_USER)
"""

import json
import os
import random
import re
import smtplib
import time
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).parent
PRODUCTS_FILE = BASE_DIR / "products.json"
HISTORY_FILE = BASE_DIR / "price_history.json"

# A few realistic, current desktop user agents to rotate between
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
]


def build_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "DNT": "1",
        "Referer": "https://www.google.com/",
    }


def load_json(path, default):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def extract_price(html):
    """Try a few known selectors, return (price_float, currency_str) or (None, None)."""
    soup = BeautifulSoup(html, "html.parser")

    # Most reliable: the hidden "offscreen" price span, e.g. "EGP 150.00"
    offscreen = soup.select_one(".a-price .a-offscreen")
    if offscreen and offscreen.text.strip():
        text = offscreen.text.strip()
        match = re.search(r"([\d,.]+)", text)
        if match:
            price = float(match.group(1).replace(",", ""))
            currency = text.replace(match.group(1), "").strip()
            return price, currency or None

    # Fallback: whole + fraction parts
    whole = soup.select_one(".a-price-whole")
    frac = soup.select_one(".a-price-fraction")
    if whole:
        try:
            price_str = whole.text.replace(",", "").replace(".", "").strip()
            if frac:
                price_str = f"{price_str}.{frac.text.strip()}"
            return float(price_str), None
        except ValueError:
            pass

    return None, None


def extract_title(html):
    soup = BeautifulSoup(html, "html.parser")
    title_tag = soup.select_one("#productTitle")
    return title_tag.text.strip() if title_tag else None


def fetch_product(url, session):
    resp = session.get(url, headers=build_headers(), timeout=15)
    if resp.status_code != 200:
        return {"error": f"HTTP {resp.status_code}"}

    price, currency = extract_price(resp.text)
    title = extract_title(resp.text)

    if price is None:
        # Could be blocked (captcha page) or genuinely out of stock
        if "captcha" in resp.text.lower() or "api-services-support@amazon.com" in resp.text:
            return {"error": "blocked_or_captcha"}
        return {"error": "price_not_found", "title": title}

    return {"price": price, "currency": currency, "title": title}


def send_email(subject, body, gmail_user, gmail_app_password, to_addr):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = to_addr

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_app_password)
        server.sendmail(gmail_user, [to_addr], msg.as_string())


def main():
    gmail_user = os.environ.get("GMAIL_USER")
    gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD")
    notify_to = os.environ.get("NOTIFY_TO", gmail_user)

    products = load_json(PRODUCTS_FILE, [])
    history = load_json(HISTORY_FILE, {})

    changes = []
    errors = []

    session = requests.Session()
    # Warm up the session by visiting the homepage first, so cookies look
    # more like a real browsing session rather than a cold, isolated request.
    try:
        session.get("https://www.amazon.eg/", headers=build_headers(), timeout=15)
        time.sleep(random.uniform(2, 4))
    except requests.RequestException:
        pass

    for product in products:
        label = product["label"]
        url = product["url"]

        result = fetch_product(url, session)
        time.sleep(random.uniform(6, 14))  # randomized delay, be polite

        if "error" in result:
            errors.append(f"- {label}: {result['error']} ({url})")
            continue

        price = result["price"]
        currency = result.get("currency") or ""
        title = result.get("title") or label

        prev = history.get(url)
        prev_price = prev["price"] if prev else None

        if prev_price is None:
            changes.append(f"- [NEW] {title}: {price} {currency}\n  {url}")
        elif price != prev_price:
            direction = "DROPPED" if price < prev_price else "ROSE"
            changes.append(
                f"- [{direction}] {title}: {prev_price} {currency} -> {price} {currency}\n  {url}"
            )

        history[url] = {"price": price, "currency": currency, "title": title}

    save_json(HISTORY_FILE, history)

    if not changes and not errors:
        print("No price changes detected. No email sent.")
        return

    body_parts = []
    if changes:
        body_parts.append("Price changes:\n" + "\n".join(changes))
    if errors:
        body_parts.append("Items that failed to fetch:\n" + "\n".join(errors))
    body = "\n\n".join(body_parts)

    print(body)

    if gmail_user and gmail_app_password and notify_to:
        send_email("Amazon Price Monitor Update", body, gmail_user, gmail_app_password, notify_to)
        print("Email sent.")
    else:
        print("Gmail credentials not set - skipping email send.")


if __name__ == "__main__":
    main()
