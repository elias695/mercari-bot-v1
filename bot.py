import os, time, json, logging, requests, re
from pathlib import Path
from datetime import datetime
from PIL import Image
import imagehash
from io import BytesIO
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import base64

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "8743642480:AAH5YC1X9042v80WZfNxcZhMqVWRxPJicxw")
CHAT_IDS = ["6886739401", "2126662016", "8041785716"]

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "120"))
REFERENCE_DIR = Path(os.getenv("REFERENCE_DIR", "reference_images"))
SEEN_FILE     = Path("seen_items.json")

def load_seen():
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()

def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(list(seen)))

def load_ref_images() -> list:
    imgs = []
    REFERENCE_DIR.mkdir(exist_ok=True)
    for f in list(REFERENCE_DIR.glob("*.jpg")) + list(REFERENCE_DIR.glob("*.jpeg")) + list(REFERENCE_DIR.glob("*.png")):
        imgs.append(f)
    log.info(f"{len(imgs)} images de reference chargees.")
    return imgs

def get_driver():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=ja-JP")
    options.add_argument("--disable-extensions")
    options.add_argument("--single-process")
    options.add_argument("--memory-pressure-off")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
    options.binary_location = "/usr/bin/chromium"
    service = Service("/usr/bin/chromedriver")
    return webdriver.Chrome(service=service, options=options)

def kill_driver(driver):
    try: driver.quit()
    except: pass

def search_by_image(driver, image_path: Path) -> list:
    """Upload image to Mercari JP image search and get results."""
    items = []
    try:
        # Step 1 — Upload image via Mercari's image search API
        with open(image_path, "rb") as f:
            img_data = f.read()

        # Get upload URL from Mercari
        upload_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Origin": "https://jp.mercari.com",
            "Referer": "https://jp.mercari.com/",
            "X-Platform": "web",
        }

        # Use Selenium to navigate to search page and trigger image upload
        driver.get("https://jp.mercari.com/")
        time.sleep(2)

        # Navigate to image search URL
        driver.get("https://jp.mercari.com/search?imageSearch=true")
        time.sleep(2)

        # Find the file input for image upload
        try:
            file_input = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='file'], [class*='imageSearch'] input, [data-testid*='image'] input"))
            )
            file_input.send_keys(str(image_path.absolute()))
            time.sleep(3)

            # Wait for results
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "[data-testid='item-cell'], mer-item-thumbnail, li[data-location]"))
            )
            time.sleep(2)

            # Extract items from page
            page_source = driver.page_source
            match = re.search(r'"items"\s*:\s*(\[[\s\S]*?\])\s*,\s*"[a-z]', page_source)
            if match:
                try:
                    raw = json.loads(match.group(1))
                    for item in raw[:20]:
                        if isinstance(item, dict) and item.get("id"):
                            items.append({
                                "id": str(item.get("id", "")),
                                "name": item.get("name", ""),
                                "price": item.get("price", 0),
                                "image_url": (item.get("thumbnails") or [None])[0] or "",
                                "url": f"https://jp.mercari.com/item/{item.get('id', '')}",
                                "ref_image": image_path.name,
                            })
                except: pass

            if not items:
                cards = driver.find_elements(By.CSS_SELECTOR, "li[data-location], [data-testid='item-cell']")
                for card in cards[:20]:
                    try:
                        link = card.find_element(By.TAG_NAME, "a")
                        href = link.get_attribute("href") or ""
                        item_id = href.split("/item/")[-1].split("?")[0] if "/item/" in href else ""
                        name_el = card.find_elements(By.CSS_SELECTOR, "p, [class*='name']")
                        name = name_el[0].text if name_el else ""
                        price_el = card.find_elements(By.CSS_SELECTOR, "[class*='price'], mer-price")
                        price_text = price_el[0].text if price_el else "0"
                        price = int("".join(filter(str.isdigit, price_text))) if price_text else 0
                        img_el = card.find_elements(By.TAG_NAME, "img")
                        img_url = img_el[0].get_attribute("src") or "" if img_el else ""
                        if item_id:
                            items.append({"id": item_id, "name": name, "price": price, "image_url": img_url, "url": f"https://jp.mercari.com/item/{item_id}", "ref_image": image_path.name})
                    except: pass

        except Exception as e:
            log.warning(f"  Upload image echoue: {e}")

        log.info(f"  '{image_path.name}' -> {len(items)} articles")
    except Exception as e:
        log.warning(f"  Erreur recherche image '{image_path.name}': {e}")
    return items

def send_telegram(text, image_url=""):
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    for chat_id in CHAT_IDS:
        if image_url:
            try:
                r = requests.post(f"{base}/sendPhoto", data={"chat_id": chat_id, "photo": image_url, "caption": text, "parse_mode": "HTML"}, timeout=10)
                if r.ok: continue
            except: pass
        try:
            requests.post(f"{base}/sendMessage", data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=10)
        except: pass

def notify(item):
    send_telegram(
        f"🔥 <b>Match trouve !</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"👕 <b>{item['name']}</b>\n"
        f"💴 <b>{item['price']:,} ¥</b>\n"
        f"🖼 Ref : <code>{item.get('ref_image', '')}</code>\n"
        f"🛒 <a href=\"{item['url']}\">Voir l'article</a>",
        image_url=item["image_url"]
    )

def run():
    log.info("=== Mercari JP Bot (Recherche par Image) ===")
    ref_images = load_ref_images()
    seen = load_seen()

    send_telegram(
        f"✅ <b>Bot demarre !</b>\n"
        f"🖼 Mode : Recherche par IMAGE\n"
        f"📸 {len(ref_images)} images de reference\n"
        f"👥 3 destinataires\n"
        f"⏱ Scan toutes les {SCAN_INTERVAL//60} min"
    )

    scan_count = 0
    while True:
        scan_count += 1
        log.info(f"--- Scan #{scan_count} {datetime.now().strftime('%H:%M:%S')} ---")
        ref_images = load_ref_images()
        total = 0

        driver = get_driver()
        try:
            for ref_img in ref_images:
                items = search_by_image(driver, ref_img)
                total += len(items)
                for info in items:
                    if not info["id"] or info["id"] in seen: continue
                    seen.add(info["id"])
                    notify(info)
                time.sleep(3)
        finally:
            kill_driver(driver)

        save_seen(seen)
        log.info(f"Total: {total} | Prochain scan dans {SCAN_INTERVAL}s...")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    run()
