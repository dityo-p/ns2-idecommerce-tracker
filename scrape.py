import requests
import re
import json
import time
from datetime import datetime
from bs4 import BeautifulSoup

# ---------- CONFIGURATION ----------
PRODUCTS = [
    {"url": "https://www.tokopedia.com/teknotrend/nintendo-switch-2-ns2-ns-2-console-new-model-1731484030680598466", "platform": "tokopedia"},
    {"url": "https://www.tokopedia.com/psenterprise/nintendo-switch-2-bonus-game-mario-kart-world-fisik-singapore-set-switch-2-nintendo-2-nintendo-switch2-console-nintendo-switch-2-nintendo-2-switch-2-switch2-ns2-nsw2-layar-7-9-1080p-dock-4k-untuk-pengalaman-gaming-terbaik-1733000068976444710", "platform": "tokopedia"},
    {"url": "https://www.tokopedia.com/super-gameshop/nintendo-switch-2-console-switch-2-nintendo-swicth2-console-1733090951679083545", "platform": "tokopedia"},
    {"url": "https://shopee.co.id/Nintendo-Switch-2-Console-Bonus-Game-Mario-Kart-World-Cartridge-Bergaransi-switch-2-i.507067058.27123873650", "platform": "shopee"},
    {"url": "https://shopee.co.id/PROMO!-Nintendo-Switch2-Console-Nintendo-Switch-2-Nintendo-2-Switch-2-Switch2-i.12523743.24545210190", "platform": "shopee"},
    {"url": "https://www.blibli.com/p/nintendo-switch-2-console-bonus-game-mario-kart-world-switch-2-nintendo-2-nintendo-switch2-console-nintendo-switch-2-nintendo-2-switch-2/is--PSP-60021-07373-00008", "platform": "blibli"},
    {"url": "https://www.blibli.com/p/nintendo-switch-2-switch2-ns2-ns-2-nintendo-2-nintendo2-console-mesin/is--MYB-34264-01338-00002", "platform": "blibli"},
    {"url": "https://www.blibli.com/p/nintendo-switch2-console-nintendo-switch-2-nintendo-2-switch-2-switch2-ns2-nsw2/is--LIG-60027-03482-00004", "platform": "blibli"},
    {"url": "https://www.blibli.com/p/nintendo-switch-2-bundle-mario-kart-world/ps--SUS-34874-02516", "platform": "blibli"},
]

OUTPUT_FILE = "data.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}
session = requests.Session()
session.headers.update(HEADERS)

# ---------- PRICE PARSER ----------
def parse_price(price_str):
    """Convert 'Rp4.999.000' or '4,999,000' to float"""
    if not price_str:
        return None
    cleaned = re.sub(r'[^\d,\.]', '', str(price_str))
    if not cleaned:
        return None
    # Handle Indonesian thousand separators (dots) and decimal (comma)
    if ',' in cleaned and '.' in cleaned:
        cleaned = cleaned.replace('.', '').replace(',', '.')
    elif ',' in cleaned:
        parts = cleaned.split(',')
        if len(parts[-1]) <= 2:   # decimal
            cleaned = ''.join(parts[:-1]) + '.' + parts[-1]
        else:                     # thousand separators
            cleaned = cleaned.replace(',', '')
    try:
        return float(cleaned)
    except:
        return None

# ---------- SESSION WARMUP ----------
def warmup_session():
    """Visit homepages to obtain cookies and appear more human."""
    try:
        session.get("https://www.tokopedia.com/", timeout=10)
    except:
        pass
    try:
        session.get("https://shopee.co.id/", timeout=10)
    except:
        pass
    try:
        session.get("https://www.blibli.com/", timeout=10)
    except:
        pass
    time.sleep(2)

# ---------- TOKOPEDIA SCRAPER ----------
def scrape_tokopedia(url):
    """
    Tokopedia embeds product data inside window.__INITIAL_STATE__.
    We extract that JSON and navigate to the product's price.
    """
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code != 200:
            print(f"Tokopedia returned status {resp.status_code}")
            return None, None

        # Find the __INITIAL_STATE__ object
        match = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.*?});', resp.text, re.DOTALL)
        if not match:
            # fallback to JSON-LD
            return scrape_tokopedia_ld(resp.text)
        data = json.loads(match.group(1))

        # Navigate: usually data['pdp']['getPDPInfo']['basicInfo'] has price and name
        pdp_info = data.get("pdp", {}).get("getPDPInfo", {})
        basic_info = pdp_info.get("basicInfo", {})
        name = basic_info.get("productName") or basic_info.get("name") or "Unknown"
        price_obj = pdp_info.get("price", {})
        price_raw = price_obj.get("value") or price_obj.get("price") or pdp_info.get("price")
        if not price_raw:
            # alternative location: data['product']['price']
            product_data = data.get("product", {})
            price_raw = product_data.get("price") or product_data.get("priceValue")

        price = parse_price(price_raw) if price_raw else None
        if price is not None and name != "Unknown":
            return price, name
    except Exception as e:
        print(f"Tokopedia __INITIAL_STATE__ error: {e}")
    return None, None

def scrape_tokopedia_ld(html):
    """Fallback using JSON-LD."""
    soup = BeautifulSoup(html, 'html.parser')
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
    return None, None

# ---------- SHOPEE SCRAPER ----------
def scrape_shopee(url):
    """
    Shopee’s public API is the most reliable method.
    We first visit the product page to get cookies, then call the API.
    """
    # Extract shop_id and item_id from URL
    m = re.search(r'i\.(\d+)\.(\d+)', url)
    if not m:
        return None, None
    shop_id, item_id = m.group(1), m.group(2)

    # Step 1: visit product page to collect cookies (simulate real user)
    try:
        resp_page = session.get(url, timeout=15, headers={"Referer": "https://shopee.co.id/"})
    except:
        pass

    # Step 2: call public API
    api_url = f"https://shopee.co.id/api/v4/item/get?itemid={item_id}&shopid={shop_id}"
    api_headers = session.headers.copy()
    api_headers["X-Requested-With"] = "XMLHttpRequest"
    api_headers["Referer"] = url
    try:
        resp = session.get(api_url, headers=api_headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            item = data.get("data", {}).get("item", {})
            name = item.get("name", "Unknown")
            # Shopee API returns price in raw form: actual price * 100000
            price_raw = item.get("price", 0)
            if isinstance(price_raw, (int, float)) and price_raw > 0:
                price = price_raw / 100000.0
                return price, name
        else:
            print(f"Shopee API returned status {resp.status_code}")
            # fallback to JSON-LD
            return scrape_shopee_ld(resp_page.text if resp_page else "")
    except Exception as e:
        print(f"Shopee API error: {e}")
        return scrape_shopee_ld(resp_page.text if resp_page else "")
    return None, None

def scrape_shopee_ld(html):
    """Fallback using JSON-LD (rarely works due to JS rendering)."""
    soup = BeautifulSoup(html, 'html.parser')
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
    return None, None

# ---------- BLIBLI SCRAPER ----------
def scrape_blibli(url):
    """
    Blibli is a Next.js site, so __NEXT_DATA__ is always present.
    The product info is inside props->pageProps->productDetail.
    """
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code != 200:
            print(f"Blibli returned status {resp.status_code}")
            return None, None

        match = re.search(r'__NEXT_DATA__\s*=\s*({.*?});', resp.text, re.DOTALL)
        if not match:
            return scrape_blibli_ld(resp.text)
        data = json.loads(match.group(1))
        # Extract product details
        props = data.get("props", {}).get("pageProps", {})
        product = props.get("productDetail") or props.get("product") or {}
        name = product.get("name") or product.get("productName") or "Unknown"
        # Prices can be in different structures
        price_info = product.get("price") or product.get("offerPrice") or {}
        if isinstance(price_info, dict):
            price_raw = price_info.get("value") or price_info.get("price") or price_info.get("offerPrice")
        else:
            price_raw = price_info
        price = parse_price(price_raw) if price_raw else None
        if price is not None and name != "Unknown":
            return price, name
    except Exception as e:
        print(f"Blibli __NEXT_DATA__ error: {e}")
    return scrape_blibli_ld(resp.text if resp else "")

def scrape_blibli_ld(html):
    """Fallback JSON-LD."""
    soup = BeautifulSoup(html, 'html.parser')
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
    return None, None

# ---------- DISPATCHER ----------
def fetch_price(url, platform):
    if platform == "tokopedia":
        return scrape_tokopedia(url)
    elif platform == "shopee":
        return scrape_shopee(url)
    elif platform == "blibli":
        return scrape_blibli(url)
    return None, None

# ---------- DATA HANDLING ----------
def load_existing_data():
    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def save_data(data):
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ---------- MAIN ----------
def main():
    warmup_session()
    data = load_existing_data()
    now = datetime.now().isoformat()

    for product in PRODUCTS:
        url = product["url"]
        platform = product["platform"]
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
            data[key]["current_price"] = price
            data[key]["name"] = name
            data[key]["last_updated"] = now
            data[key]["history"].append({
                "price": price,
                "timestamp": now,
                "name": name
            })
            # Keep history size manageable
            if len(data[key]["history"]) > 200:
                data[key]["history"] = data[key]["history"][-200:]
            print(f"  -> {name}: Rp{price:,.2f}")
        else:
            print("  -> Failed to get price (will retry next cycle)")

        # Polite delay between requests (10-15 seconds)
        time.sleep(12)

    save_data(data)
    print("data.json updated.")

if __name__ == "__main__":
    main()
