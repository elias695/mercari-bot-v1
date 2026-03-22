import os, time, json, logging, requests, numpy as np, re
from pathlib import Path
from datetime import datetime
from PIL import Image, ImageFilter
from io import BytesIO
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import torch
import torchvision.transforms as transforms

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_IDS       = [c.strip() for c in os.environ["TELEGRAM_CHAT_IDS"].split(",")]

_raw = float(os.getenv("SIMILARITY_THRESHOLD", "0.80"))
SIMILARITY_THRESHOLD = _raw / 100.0 if _raw > 1.0 else _raw

SCAN_INTERVAL         = int(os.getenv("SCAN_INTERVAL", "120"))
IMAGE_SEARCH_INTERVAL = int(os.getenv("IMAGE_SEARCH_INTERVAL", "3600"))
REFERENCE_DIR         = Path(os.getenv("REFERENCE_DIR", "reference_images"))
SEEN_FILE             = Path("seen_items.json")
MAX_PRICE             = int(os.getenv("MAX_PRICE", "0"))
MIN_PRICE             = int(os.getenv("MIN_PRICE", "0"))

KEYWORDS = [
    "nike",
    "under armour",
    "ナイキ",
    "アンダーアーマー",
]

# ═══════════════════════════════════════════════════
#  DINOv2
# ═══════════════════════════════════════════════════

log.info("Chargement DINOv2...")

_transform = transforms.Compose([
    transforms.Resize((224, 224)),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406],
                         std=[0.229, 0.224, 0.225]),
])

_model = torch.hub.load("facebookresearch/dinov2", "dinov2_vits14")
_model.eval()
log.info("DINOv2 chargé ✅")


def extract_features(img: Image.Image) -> np.ndarray:
    tensor = _transform(img).unsqueeze(0)
    with torch.no_grad():
        feat = _model(tensor).squeeze().numpy()
    norm = np.linalg.norm(feat)
    return feat / norm if norm > 0 else feat

# ═══════════════════════════════════════════════════
#  ROGNER LE VÊTEMENT (supprime interfaces et fonds)
# ═══════════════════════════════════════════════════

def crop_garment(img: Image.Image) -> Image.Image:
    """
    Détecte et rogne automatiquement le vêtement dans l'image.
    Supprime les barres d'interface iPhone, notifications, texte parasite.
    
    Stratégie :
    1. Convertit en niveaux de gris
    2. Détecte les bandes noires/blanches uniformes en haut/bas (interface)
    3. Rogne ces bandes
    4. Centre sur la zone non-uniforme (le vêtement)
    """
    w, h = img.size

    # Convertit en numpy pour analyse
    img_array = np.array(img.convert("RGB"))

    # ── Étape 1 : supprime les barres d'interface en haut et en bas ──
    # Détecte les lignes très sombres (barres noires iPhone) ou très claires
    gray = np.mean(img_array, axis=2)  # moyenne RGB → niveaux de gris

    top_crop = 0
    bottom_crop = h

    # Cherche les bandes noires en haut (barre de statut iPhone = ~100px noirs)
    for i in range(min(200, h)):
        row = gray[i, :]
        # Si la ligne est très sombre (barre noire) ou très uniforme
        if np.mean(row) < 30 or np.std(row) < 5:
            top_crop = i + 1
        else:
            break

    # Cherche les bandes noires en bas
    for i in range(h - 1, max(h - 200, 0), -1):
        row = gray[i, :]
        if np.mean(row) < 30 or np.std(row) < 5:
            bottom_crop = i
        else:
            break

    # Sécurité : si on a rognié trop, annule
    if bottom_crop - top_crop < h * 0.5:
        top_crop = 0
        bottom_crop = h

    img_cropped = img.crop((0, top_crop, w, bottom_crop))

    # ── Étape 2 : rogne les bords blancs/uniformes (fonds unis) ──
    img_array2 = np.array(img_cropped.convert("RGB"))
    h2, w2 = img_array2.shape[:2]
    gray2 = np.mean(img_array2, axis=2)

    # Trouve les bords avec du contenu (std > 10 = pas uniforme)
    col_std = np.std(img_array2, axis=0).mean(axis=1)
    row_std = np.std(img_array2, axis=1).mean(axis=1)

    threshold = 8
    left   = next((i for i in range(w2) if col_std[i] > threshold), 0)
    right  = next((i for i in range(w2-1, 0, -1) if col_std[i] > threshold), w2)
    top2   = next((i for i in range(h2) if row_std[i] > threshold), 0)
    bottom2 = next((i for i in range(h2-1, 0, -1) if row_std[i] > threshold), h2)

    # Ajoute un petit padding
    pad = 10
    left   = max(0, left - pad)
    right  = min(w2, right + pad)
    top2   = max(0, top2 - pad)
    bottom2 = min(h2, bottom2 + pad)

    # Sécurité : la zone rognée doit faire au moins 30% de l'image
    if (right - left) < w2 * 0.3 or (bottom2 - top2) < h2 * 0.3:
        return img_cropped

    return img_cropped.crop((left, top2, right, bottom2))


def compare(img_url: str, ref_features: list) -> tuple:
    """Compare l'image d'un article (rognée) avec toutes les références (rognées)."""
    if not img_url or not ref_features:
        return 0.0, ""
    try:
        r = requests.get(img_url, timeout=10,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        img = Image.open(BytesIO(r.content)).convert("RGB")
        img_cropped = crop_garment(img)
        feat = extract_features(img_cropped)

        best_sim, best_ref = 0.0, ""
        for name, ref_f in ref_features:
            sim = float(np.dot(feat, ref_f))
            sim = (sim + 1.0) / 2.0
            if sim > best_sim:
                best_sim, best_ref = sim, name
        return best_sim, best_ref
    except Exception:
        return 0.0, ""

# ═══════════════════════════════════════════════════
#  PERSISTANCE
# ═══════════════════════════════════════════════════

def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            return set()
    return set()

def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(list(seen)))

# ═══════════════════════════════════════════════════
#  IMAGES DE RÉFÉRENCE — rognées + encodées
# ═══════════════════════════════════════════════════

def load_ref_features() -> list:
    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
        files.extend(REFERENCE_DIR.glob(ext))
    log.info(f"{len(files)} images de référence")

    result = []
    for f in sorted(files):
        try:
            img = Image.open(f).convert("RGB")
            img_cropped = crop_garment(img)
            feat = extract_features(img_cropped)
            result.append((f.name, feat))
        except Exception as e:
            log.warning(f"Ignorée {f.name}: {e}")

    log.info(f"{len(result)} features calculées (avec rognage) ✅")
    return result

# ═══════════════════════════════════════════════════
#  CHROME
# ═══════════════════════════════════════════════════

def make_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--lang=ja-JP")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    )
    opts.binary_location = "/usr/bin/chromium"
    service = Service("/usr/bin/chromedriver")
    driver = webdriver.Chrome(service=service, options=opts)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'languages', {get: () => ['ja-JP', 'ja', 'en']});
            Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
        """
    })
    return driver


def fetch_by_keyword(driver, keyword: str) -> list:
    import urllib.parse, json as _json
    items = []
    params = {"keyword": keyword, "status": "on_sale",
               "sort": "created_time", "order": "desc"}
    if MIN_PRICE > 0: params["price_min"] = MIN_PRICE
    if MAX_PRICE > 0: params["price_max"] = MAX_PRICE
    url = "https://jp.mercari.com/search?" + urllib.parse.urlencode(params)
    try:
        driver.get(url)
        try:
            WebDriverWait(driver, 15).until(EC.presence_of_element_located(
                (By.CSS_SELECTOR,
                 "[data-testid='item-cell'], li[data-location], mer-item-thumbnail")))
        except Exception:
            pass
        time.sleep(2)
        html = driver.page_source
        m = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>',
            html, re.DOTALL)
        if m:
            try:
                nd = _json.loads(m.group(1))
                raw = (
                    _dig(nd,"props","pageProps","initialState","search","items") or
                    _dig(nd,"props","pageProps","searchResult","items") or
                    _dig(nd,"props","pageProps","items") or []
                )
                for it in raw:
                    p = _norm(it, keyword)
                    if p: items.append(p)
                if items:
                    log.info(f"  [NEXT] '{keyword}' → {len(items)}")
                    return items
            except Exception:
                pass
        cards = driver.find_elements(By.CSS_SELECTOR,
            "li[data-location], [data-testid='item-cell'], mer-item-thumbnail")
        for card in cards[:30]:
            try:
                link = card.find_element(By.TAG_NAME, "a")
                href = link.get_attribute("href") or ""
                iid = href.split("/item/")[-1].split("?")[0] if "/item/" in href else ""
                if not iid: continue
                ne = card.find_elements(By.CSS_SELECTOR, "[class*='itemName'],[class*='name'],p")
                name = ne[0].text.strip() if ne else ""
                pe = card.find_elements(By.CSS_SELECTOR, "[class*='price'],mer-price")
                price = int("".join(c for c in (pe[0].text if pe else "0") if c.isdigit()) or "0")
                ie = card.find_elements(By.TAG_NAME, "img")
                img = ie[0].get_attribute("src") or "" if ie else ""
                items.append({"id": iid, "name": name, "price": price,
                              "image_url": img,
                              "url": f"https://jp.mercari.com/item/{iid}",
                              "keyword": keyword})
            except Exception:
                continue
        if items: log.info(f"  [DOM] '{keyword}' → {len(items)}")
        else: log.warning(f"  '{keyword}' → 0 articles")
    except Exception as e:
        log.warning(f"  Chrome '{keyword}': {e}")
    return items


def fetch_by_image(driver, image_path: Path) -> list:
    items = []
    try:
        driver.get("https://jp.mercari.com/")
        time.sleep(2)
        try:
            file_input = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR,
                    "input[type='file'][accept*='image'], input[accept='image/*']")))
            file_input.send_keys(str(image_path.absolute()))
            time.sleep(4)
            try:
                WebDriverWait(driver, 15).until(EC.presence_of_element_located(
                    (By.CSS_SELECTOR,
                     "[data-testid='item-cell'], li[data-location], mer-item-thumbnail")))
            except Exception:
                pass
            time.sleep(2)
        except Exception:
            return []

        import json as _json
        html = driver.page_source
        m = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>',
            html, re.DOTALL)
        if m:
            try:
                nd = _json.loads(m.group(1))
                raw = (
                    _dig(nd,"props","pageProps","initialState","search","items") or
                    _dig(nd,"props","pageProps","searchResult","items") or
                    _dig(nd,"props","pageProps","items") or []
                )
                for it in raw:
                    p = _norm(it, f"img:{image_path.name}")
                    if p: items.append(p)
            except Exception:
                pass

        if not items:
            cards = driver.find_elements(By.CSS_SELECTOR,
                "li[data-location], [data-testid='item-cell'], mer-item-thumbnail")
            for card in cards[:20]:
                try:
                    link = card.find_element(By.TAG_NAME, "a")
                    href = link.get_attribute("href") or ""
                    iid = href.split("/item/")[-1].split("?")[0] if "/item/" in href else ""
                    if not iid: continue
                    ne = card.find_elements(By.CSS_SELECTOR, "[class*='itemName'],[class*='name'],p")
                    name = ne[0].text.strip() if ne else ""
                    pe = card.find_elements(By.CSS_SELECTOR, "[class*='price'],mer-price")
                    price = int("".join(c for c in (pe[0].text if pe else "0") if c.isdigit()) or "0")
                    ie = card.find_elements(By.TAG_NAME, "img")
                    img_url = ie[0].get_attribute("src") or "" if ie else ""
                    items.append({"id": iid, "name": name, "price": price,
                                  "image_url": img_url,
                                  "url": f"https://jp.mercari.com/item/{iid}",
                                  "keyword": f"img:{image_path.name}"})
                except Exception:
                    continue

        if items: log.info(f"  [IMG] '{image_path.name}' → {len(items)}")
    except Exception as e:
        log.warning(f"  Recherche image '{image_path.name}': {e}")
    return items


def run_image_search(seen: set, ref_features: list) -> int:
    log.info("=== Recherche par IMAGE ===")
    ref_files = []
    for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
        ref_files.extend(REFERENCE_DIR.glob(ext))

    new_matches = 0
    seen_this = set()
    driver = None
    try:
        driver = make_driver()
        driver.get("https://jp.mercari.com/")
        time.sleep(3)
        for ref_path in sorted(ref_files):
            items = fetch_by_image(driver, ref_path)
            time.sleep(2)
            for item in items:
                iid = item["id"]
                if iid in seen or iid in seen_this: continue
                seen_this.add(iid)
                seen.add(iid)
                if not ref_features: continue
                sim, ref = compare(item["image_url"], ref_features)
                if sim >= SIMILARITY_THRESHOLD:
                    notify(item, sim, ref)
                    new_matches += 1
    except Exception as e:
        log.error(f"Erreur recherche image: {e}")
    finally:
        if driver:
            try: driver.quit()
            except Exception: pass
    log.info(f"Recherche image terminée — {new_matches} match(s)")
    return new_matches


def _dig(obj, *keys):
    for k in keys:
        if obj is None: return None
        obj = obj.get(k) if isinstance(obj, dict) else None
    return obj


def _norm(raw, keyword):
    iid = str(raw.get("id") or raw.get("itemId") or "").strip()
    if not iid: return None
    if not iid.startswith("m"): iid = "m" + iid
    name = raw.get("name") or raw.get("itemName") or ""
    price = 0
    for pk in ("price","itemPrice","sellingPrice"):
        v = raw.get(pk)
        if v is not None:
            try: price = int(str(v).replace(",","").replace("¥","").strip()); break
            except Exception: pass
    img = ""
    for ik in ("thumbnails","photos","images"):
        v = raw.get(ik)
        if isinstance(v, list) and v:
            img = v[0] if isinstance(v[0], str) else (v[0].get("imageUrl") or "")
            break
    if not img:
        for ik in ("thumbnail","imageUrl","image_url","thumbnailUrl"):
            img = raw.get(ik, "")
            if img: break
    return {"id": iid, "name": name, "price": price, "image_url": img,
            "url": f"https://jp.mercari.com/item/{iid}", "keyword": keyword}

# ═══════════════════════════════════════════════════
#  TELEGRAM
# ═══════════════════════════════════════════════════

def send_telegram(text: str, image_url: str = ""):
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    for chat_id in CHAT_IDS:
        sent = False
        if image_url:
            try:
                r = requests.post(f"{base}/sendPhoto",
                    data={"chat_id": chat_id, "photo": image_url,
                          "caption": text, "parse_mode": "HTML"}, timeout=10)
                sent = r.ok
            except Exception: pass
        if not sent:
            try:
                requests.post(f"{base}/sendMessage",
                    data={"chat_id": chat_id, "text": text,
                          "parse_mode": "HTML"}, timeout=10)
            except Exception as ex:
                log.warning(f"Telegram {chat_id}: {ex}")


def notify(item, sim, ref):
    pct = f"{sim*100:.1f}%"
    price_str = f"{item['price']:,}" if item['price'] else "—"
    source = item.get('keyword', '')
    mode = "🖼 Recherche image" if source.startswith("img:") else f"🔍 <i>{source}</i>"
    msg = (
        f"🔥 <b>Match trouvé !</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👕 <b>{item['name']}</b>\n"
        f"💴 <b>{price_str} ¥</b>\n"
        f"{mode}\n"
        f"📊 Similarité : <b>{pct}</b>\n"
        f"🖼 Réf : <code>{ref}</code>\n"
        f"🛒 <a href=\"{item['url']}\">Voir l'article</a>"
    )
    send_telegram(msg, image_url=item["image_url"])
    log.info(f"  ✅ {item['name'][:50]} ({pct})")

# ═══════════════════════════════════════════════════
#  BOUCLE PRINCIPALE
# ═══════════════════════════════════════════════════

def run():
    log.info("=== Mercari Bot v2 (DINOv2 + Rognage + Chrome) ===")
    log.info(f"Seuil : {SIMILARITY_THRESHOLD*100:.0f}% | Scan : {SCAN_INTERVAL}s | Img search : {IMAGE_SEARCH_INTERVAL//3600}h")

    seen = load_seen()
    ref_features = load_ref_features()

    send_telegram(
        f"✅ <b>Bot v2 démarré !</b>\n"
        f"🧠 <b>DINOv2 + Rognage auto</b>\n"
        f"📸 <b>{len(ref_features)}</b> images de référence\n"
        f"🔍 <b>{len(KEYWORDS)}</b> mots-clés\n"
        f"📊 Seuil : <b>{SIMILARITY_THRESHOLD*100:.0f}%</b>\n"
        f"👥 <b>{len(CHAT_IDS)}</b> destinataire(s)\n"
        f"⏱ Scan : <b>{SCAN_INTERVAL}s</b> | Image : <b>{IMAGE_SEARCH_INTERVAL//3600}h</b>"
    )

    scan_count = 0
    last_image_search = 0

    while True:
        scan_count += 1
        now = time.time()
        log.info(f"─── Scan #{scan_count} · {datetime.now().strftime('%d/%m %H:%M:%S')} ───")

        # Recherche par image toutes les heures
        if now - last_image_search >= IMAGE_SEARCH_INTERVAL:
            run_image_search(seen, ref_features)
            save_seen(seen)
            last_image_search = time.time()

        # Scan par mots-clés
        new_matches = 0
        seen_this = set()
        driver = None
        try:
            driver = make_driver()
            driver.get("https://jp.mercari.com/")
            time.sleep(3)
            for keyword in KEYWORDS:
                items = fetch_by_keyword(driver, keyword)
                time.sleep(3)
                for item in items:
                    iid = item["id"]
                    if iid in seen or iid in seen_this: continue
                    seen_this.add(iid)
                    seen.add(iid)
                    if not ref_features: continue
                    sim, ref = compare(item["image_url"], ref_features)
                    if sim >= SIMILARITY_THRESHOLD:
                        notify(item, sim, ref)
                        new_matches += 1
        except Exception as e:
            log.error(f"Erreur scan: {e}")
        finally:
            if driver:
                try: driver.quit()
                except Exception: pass

        save_seen(seen)
        log.info(f"Scan #{scan_count} — {new_matches} match(s) — prochain dans {SCAN_INTERVAL}s")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info("Arrêt.")
    except Exception as e:
        log.critical(f"Fatal: {e}", exc_info=True)
        try: send_telegram(f"🚨 <b>Bot crashé !</b>\n<code>{str(e)[:300]}</code>")
        except Exception: pass
        raise
