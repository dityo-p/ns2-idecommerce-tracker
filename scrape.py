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

# Real browser‑like headers
BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Cache-Control": "max-age=0",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
}

# ---------- RETRY + SESSION ----------
def create_session():
    s = requests.Session()
    s.headers.update(BASE_HEADERS)
    return s

def robust_get(session, url, referer=None, max_retries=3):
    """GET with retries and exponential backoff."""
    headers = {}
    if referer:
        headers["Referer"] = referer
    for attempt in range(max_retries):
        try:
            resp = session.get(url, headers=headers, timeout=30)
            return resp
        except requests.exceptions.Timeout:
            print(f"Timeout on attempt {attempt+1}/{max_retries} for {url[:60]}...")
            time.sleep(5 * (attempt+1))
        except Exception as e:
            print(f"Request error: {e}")
            time.sleep(5)
    return None

# ---------- PRICE PARSER ----------
def parse_price(price_str):
    if not price_str:
        return None
    cleaned = re.sub(r'[^\d,\.]', '', str(price_str))
    if not cleaned:
        return None
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

# ---------- TOKOPEDIA ----------
def scrape_tokopedia(session, url):
    resp = robust_get(session, url, referer="https://www.tokopedia.com/")
    if not resp or resp.status_code != 200:
        print(f"Tokopedia: failed to load page (status {resp.status_code if resp else 'None'})")
        return None, None

    # Try __INITIAL_STATE__
    match = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.*?});', resp.text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            pdp = data.get("pdp", {}).get("getPDPInfo", {})
            basic = pdp.get("basicInfo", {})
            name = basic.get("productName") or basic.get("name") or "Unknown"
            price_obj = pdp.get("price", {})
            price_raw = price_obj.get("value") or price_obj.get("price") or pdp.get("price")
            price = parse_price(price_raw) if price_raw else None
            if price and name != "Unknown":
                return price, name
        except Exception as e:
            print(f"Tokopedia __INITIAL_STATE__ parse error: {e}")

    # Fallback: JSON‑LD
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
    return None, None

# ---------- SHOPEE ----------
def scrape_shopee(session, url):
    # Extract shop_id, item_id
    m = re.search(r'i\.(\d+)\.(\d+)', url)
    if not m:
        return None, None
    shop_id, item_id = m.group(1), m.group(2)

    # Step 1: load product page to get cookies + CSRF token
    resp_page = robust_get(session, url, referer="https://shopee.co.id/")
    if not resp_page or resp_page.status_code != 200:
        print(f"Shopee: product page failed (status {resp_page.status_code if resp_page else 'None'})")
        return None, None

    # Extract CSRF token from cookies (Shopee sets 'csrftoken')
    csrftoken = None
    for cookie in session.cookies:
        if cookie.name == "csrftoken":
            csrftoken = cookie.value
            break

    if not csrftoken:
        print("Shopee: no csrftoken cookie found")
        # Try to parse from HTML meta as fallback
        soup = BeautifulSoup(resp_page.text, 'html.parser')
        meta = soup.find("meta", attrs={"name": "csrf-token"})
        if meta:
            csrftoken = meta.get("content")
        if not csrftoken:
            # Last resort: use regex on __INITIAL_STATE__ or inline script
            match = re.search(r'"csrfToken":"([^"]+)"', resp_page.text)
            if match:
                csrftoken = match.group(1)
            else:
                return None, None

    # Step 2: call API with CSRF token
    api_url = f"https://shopee.co.id/api/v4/item/get?itemid={item_id}&shopid={shop_id}"
    api_headers = {
        "Referer": url,
        "X-Requested-With": "XMLHttpRequest",
        "x-csrftoken": csrftoken,
        "Accept": "application/json",
    }
    for attempt in range(3):
        try:
            resp_api = session.get(api_url, headers=api_headers, timeout=30)
            if resp_api.status_code == 200:
                data = resp_api.json()
                item = data.get("data", {}).get("item", {})
                name = item.get("name", "Unknown")
                price_raw = item.get("price", 0)
                price = price_raw / 100000.0 if isinstance(price_raw, (int, float)) and price_raw > 0 else None
                if price and name != "Unknown":
                    return price, name
            else:
                print(f"Shopee API attempt {attempt+1}: status {resp_api.status_code}")
        except Exception as e:
            print(f"Shopee API error: {e}")
        time.sleep(3)
    return None, None

# ---------- BLIBLI ----------
def scrape_blibli(session, url):
    resp = robust_get(session, url, referer="https://www.blibli.com/")
    if not resp or resp.status_code != 200:
        print(f"Blibli: failed to load page (status {resp.status_code if resp else 'None'})")
        return None, None

    # Try __NEXT_DATA__
    match = re.search(r'__NEXT_DATA__\s*=\s*({.*?});', resp.text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            props = data.get("props", {}).get("pageProps", {})
            product = props.get("productDetail") or props.get("product") or {}
            name = product.get("name") or product.get("productName") or "Unknown"
            price_obj = product.get("price") or product.get("offerPrice") or {}
            price_raw = price_obj.get("value") or price_obj.get("price") or price_obj.get("offerPrice") if isinstance(price_obj, dict) else price_obj
            price = parse_price(price_raw) if price_raw else None
            if price and name != "Unknown":
                return price, name
        except Exception as e:
            print(f"Blibli __NEXT_DATA__ parse error: {e}")

    # Fallback JSON‑LD
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
    return None, None

# ---------- DISPATCHER ----------
def fetch_price(session, url, platform):
    if platform == "tokopedia":
        return scrape_tokopedia(session, url)
    elif platform == "shopee":
        return scrape_shopee(session, url)
    elif platform == "blibli":
        return scrape_blibli(session, url)
    return None, None

# ---------- DATA ----------
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
    session = create_session()
    # Initial warm‑up (skip if it fails)
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
        price, name = fetch_price(session, url, platform)

        if price is not None and name:
            data[key]["current_price"] = price
            data[key]["name"] = name
            data[key]["last_updated"] = now
            data[key]["history"].append({
                "price": price,
                "timestamp": now,
                "name": name
            })
            if len(data[key]["history"]) > 200:
                data[key]["history"] = data[key]["history"][-200:]
            print(f"  -> {name}: Rp{price:,.2f}")
        else:
            print("  -> Failed to get price")

        # Polite delay (15–20s between each product)
        time.sleep(18)

    save_data(data)
    print("data.json updated.")

if __name__ == "__main__":
    main()
