import requests
import re
import json
import time
from datetime import datetime
from bs4 import BeautifulSoup

# ---------- CONFIGURATION ----------
PRODUCTS = [
    {"url": "https://www.tokopedia.com/teknotrend/nintendo-switch-2-ns2-ns-2-console-new-model-1731484030680598466", "platform": "tokopedia"},
    # ... (add all 9 URLs from your list exactly as before)
]

OUTPUT_FILE = "data.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}
session = requests.Session()
session.headers.update(HEADERS)

# ---------- PRICE PARSER ----------
def parse_price(price_str):
    cleaned = re.sub(r'[^\d,\.]', '', price_str)
    if ',' in cleaned and '.' in cleaned:
        cleaned = cleaned.replace('.', '').replace(',', '.')
    elif ',' in cleaned:
        parts = cleaned.split(',')
        if len(parts[-1]) <= 2:
            cleaned = ''.join(parts[:-1]) + '.' + parts[-1]
        else:
            cleaned = cleaned.replace(',', '')
    try:
        return float(cleaned)
    except:
        return None

# ---------- SCRAPERS ----------
def scrape_tokopedia(url):
    try:
        resp = session.get(url, timeout=15)
        soup = BeautifulSoup(resp.text, 'html.parser')
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string)
                if data.get("@type") == "Product":
                    name = data.get("name", "Unknown")
                    offers = data.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0]
                    price = parse_price(offers.get("price", "0"))
                    return price, name
            except:
                continue
    except Exception as e:
        print(f"Tokopedia error: {e}")
    return None, None

def scrape_shopee(url):
    # Try JSON-LD first, then fallback to HTML
    try:
        resp = session.get(url, timeout=15)
        soup = BeautifulSoup(resp.text, 'html.parser')
        # JSON-LD
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string)
                if data.get("@type") == "Product":
                    name = data.get("name", "Unknown")
                    offers = data.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0]
                    price = parse_price(offers.get("price", "0"))
                    return price, name
            except:
                continue
        # Fallback: __INITIAL_STATE__ (adapt if needed)
    except Exception as e:
        print(f"Shopee error: {e}")
    return None, None

def scrape_blibli(url):
    try:
        resp = session.get(url, timeout=15)
        soup = BeautifulSoup(resp.text, 'html.parser')
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string)
                if data.get("@type") == "Product":
                    name = data.get("name", "Unknown")
                    offers = data.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0]
                    price = parse_price(offers.get("price", "0"))
                    return price, name
            except:
                continue
    except Exception as e:
        print(f"Blibli error: {e}")
    return None, None

def fetch_price(url, platform):
    if platform == "tokopedia":
        return scrape_tokopedia(url)
    elif platform == "shopee":
        return scrape_shopee(url)
    elif platform == "blibli":
        return scrape_blibli(url)
    return None, None

# ---------- DATA MANAGEMENT ----------
def load_existing_data():
    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def save_data(data):
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ---------- MAIN SCRAPE JOB ----------
def main():
    data = load_existing_data()
    now = datetime.now().isoformat()

    for product in PRODUCTS:
        url = product["url"]
        platform = product["platform"]
        # Use URL as unique key
        key = url
        if key not in data:
            data[key] = {
                "url": url,
                "platform": platform,
                "name": "Unknown",
                "current_price": None,
                "last_updated": None,
                "history": []
            }

        print(f"Scraping {platform}: {url[:60]}...")
        price, name = fetch_price(url, platform)
        if price is not None and name:
            entry = {
                "price": price,
                "timestamp": now,
                "name": name
            }
            data[key]["current_price"] = price
            data[key]["name"] = name
            data[key]["last_updated"] = now
            data[key]["history"].append(entry)
            # Keep only last 200 history points to limit file size
            if len(data[key]["history"]) > 200:
                data[key]["history"] = data[key]["history"][-200:]
            print(f"  -> {name}: Rp{price:,.2f}")
        else:
            print("  -> Failed to fetch price")
        time.sleep(2)  # polite delay

    save_data(data)
    print("data.json updated.")

if __name__ == "__main__":
    main()