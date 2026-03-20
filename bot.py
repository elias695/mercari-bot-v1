import os, time, json, logging, requests, re
from pathlib import Path
from datetime import datetime
from PIL import Image
import imagehash
from io import BytesIO

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_IDS       = [c.strip() for c in os.environ["TELEGRAM_CHAT_IDS"].split(",")]

_raw = float(os.getenv("SIMILARITY_THRESHOLD", "0.80"))
SIMILARITY_THRESHOLD = _raw / 100.0 if _raw > 1.0 else _raw

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "120"))
REFERENCE_DIR = Path(os.getenv("REFERENCE_DIR", "reference_images"))
SEEN_FILE     = Path("seen_items.json")
MAX_PRICE     = int(os.getenv("MAX_PRICE", "0"))
MIN_PRICE     = int(os.getenv("MIN_PRICE", "0"))

KEYWORDS = [
    "ナイキ ランニング",
    "nike running",
    "アンダーアーマー ランニング",
    "under armour running",
]

# Session persistante avec cookies — imite un vrai navigateur
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})

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
#  IMAGES DE RÉFÉRENCE
# ═══════════════════════════════════════════════════

def load_ref_hashes() -> list:
    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
        files.extend(REFERENCE_DIR.glob(ext))
    log.info(f"{len(files)} images de référence dans '{REFERENCE_DIR.absolute()}'")
    hashes = []
    for f in sorted(files):
        try:
            img = Image.open(f).convert("RGB")
            hashes.append((f.name, imagehash.phash(img)))
        except Exception as e:
            log.warning(f"Image ignorée {f.name}: {e}")
    return hashes

def compare(img_url: str, ref_hashes: list) -> tuple:
    if not img_url or not ref_hashes:
        return 0.0, ""
    try:
        r = SESSION.get(img_url, timeout=10)
        r.raise_for_status()
        h = imagehash.phash(Image.open(BytesIO(r.content)).convert("RGB"))
        best_sim, best_ref = 0.0, ""
        for name, ref_h in ref_hashes:
            sim = 1.0 - (h - ref_h) / 64.0
            if sim > best_sim:
                best_sim, best_ref = sim, name
        return best_sim, best_ref
    except Exception:
        return 0.0, ""

# ═══════════════════════════════════════════════════
#  SCRAPING MERCARI — page HTML + JSON embarqué
# ═══════════════════════════════════════════════════

def init_session():
    """Visite la page d'accueil pour obtenir les cookies nécessaires."""
    try:
        SESSION.get("https://jp.mercari.com/", timeout=15)
        log.info("Session initialisée (cookies ok)")
    except Exception as e:
        log.warning(f"Init session: {e}")


def fetch_items(keyword: str) -> list:
    """
    Scrape la page de recherche Mercari et extrait les articles
    depuis le JSON __NEXT_DATA__ embarqué dans la page HTML.
    """
    import urllib.parse
    params = {"keyword": keyword, "status": "on_sale", "sort": "created_time", "order": "desc"}
    if MIN_PRICE > 0:
        params["price_min"] = MIN_PRICE
    if MAX_PRICE > 0:
        params["price_max"] = MAX_PRICE

    url = "https://jp.mercari.com/search?" + urllib.parse.urlencode(params)
    items = []

    try:
        resp = SESSION.get(url, timeout=20)
        resp.raise_for_status()
        html = resp.text

        # Méthode 1 : extraction depuis __NEXT_DATA__ (JSON complet dans la page)
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', html, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                # Cherche les items dans la structure Next.js
                items_raw = _dig(data, "props", "pageProps", "initialState", "search", "items") or \
                            _dig(data, "props", "pageProps", "items") or \
                            _dig(data, "props", "pageProps", "searchResult", "items") or []

                for item in items_raw:
                    parsed = _parse_item(item, keyword)
                    if parsed:
                        items.append(parsed)
            except Exception as e:
                log.debug(f"  __NEXT_DATA__ parse error: {e}")

        # Méthode 2 : fallback — extraction depuis les balises JSON-LD ou data-item
        if not items:
            # Cherche des patterns JSON d'items dans le HTML
            for pattern in [
                r'"id"\s*:\s*"(m\d+)"[^}]*"name"\s*:\s*"([^"]+)"[^}]*"price"\s*:\s*(\d+)',
                r'"itemId"\s*:\s*"(m?\d+)"[^}]*"itemName"\s*:\s*"([^"]+)"[^}]*"price"\s*:\s*(\d+)',
            ]:
                for m2 in re.finditer(pattern, html):
                    item_id = m2.group(1)
                    if not item_id.startswith("m"):
                        item_id = "m" + item_id
                    items.append({
                        "id": item_id,
                        "name": m2.group(2),
                        "price": int(m2.group(3)),
                        "image_url": "",
                        "url": f"https://jp.mercari.com/item/{item_id}",
                        "keyword": keyword,
                    })
                if items:
                    break

    except requests.HTTPError as e:
        log.warning(f"HTTP {e.response.status_code} pour '{keyword}'")
        if e.response.status_code in (403, 429):
            log.info("Rate limit détecté — pause 30s")
            time.sleep(30)
            init_session()
    except Exception as e:
        log.warning(f"Erreur fetch '{keyword}': {e}")

    # Déduplique par id
    seen_ids = set()
    unique = []
    for it in items:
        if it["id"] not in seen_ids:
            seen_ids.add(it["id"])
            unique.append(it)

    log.info(f"  '{keyword}' → {len(unique)} articles")
    return unique


def _dig(obj, *keys):
    """Navigation sûre dans un dict imbriqué."""
    for k in keys:
        if isinstance(obj, dict):
            obj = obj.get(k)
        elif isinstance(obj, list) and isinstance(k, int):
            obj = obj[k] if k < len(obj) else None
        else:
            return None
        if obj is None:
            return None
    return obj


def _parse_item(raw: dict, keyword: str) -> dict | None:
    """Normalise un item brut depuis __NEXT_DATA__."""
    # Plusieurs structures possibles selon la version de Mercari
    item_id = (
        raw.get("id") or raw.get("itemId") or raw.get("item_id") or ""
    )
    if not item_id:
        return None
    item_id = str(item_id)
    if not item_id.startswith("m"):
        item_id = "m" + item_id

    name = raw.get("name") or raw.get("itemName") or raw.get("item_name") or ""

    price = 0
    for pk in ("price", "itemPrice", "item_price", "sellingPrice"):
        v = raw.get(pk)
        if v is not None:
            try:
                price = int(str(v).replace(",", "").replace("¥", "").strip())
                break
            except (ValueError, TypeError):
                pass

    # Image — plusieurs champs possibles
    img = ""
    for ik in ("thumbnails", "photos", "images"):
        v = raw.get(ik)
        if isinstance(v, list) and v:
            img = v[0] if isinstance(v[0], str) else (v[0].get("imageUrl") or v[0].get("url") or "")
            break
    if not img:
        for ik in ("thumbnail", "imageUrl", "image_url", "photo", "thumbnailUrl"):
            img = raw.get(ik, "")
            if img:
                break

    return {
        "id":        item_id,
        "name":      name,
        "price":     price,
        "image_url": img,
        "url":       f"https://jp.mercari.com/item/{item_id}",
        "keyword":   keyword,
    }

# ═══════════════════════════════════════════════════
#  TELEGRAM
# ═══════════════════════════════════════════════════

def send_telegram(text: str, image_url: str = ""):
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    for chat_id in CHAT_IDS:
        sent = False
        if image_url:
            try:
                r = requests.post(
                    f"{base}/sendPhoto",
                    data={"chat_id": chat_id, "photo": image_url,
                          "caption": text, "parse_mode": "HTML"},
                    timeout=10,
                )
                sent = r.ok
            except Exception:
                pass
        if not sent:
            try:
                requests.post(
                    f"{base}/sendMessage",
                    data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                    timeout=10,
                )
            except Exception as ex:
                log.warning(f"Telegram erreur {chat_id}: {ex}")

def notify(item: dict, sim: float, ref: str):
    pct = f"{sim * 100:.1f}%"
    price_str = f"{item['price']:,}" if item['price'] else "—"
    msg = (
        f"🔥 <b>Match trouvé !</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👕 <b>{item['name']}</b>\n"
        f"💴 <b>{price_str} ¥</b>\n"
        f"🔍 Mot-clé : <i>{item['keyword']}</i>\n"
        f"📊 Similarité : <b>{pct}</b>\n"
        f"🖼 Référence : <code>{ref}</code>\n"
        f"🛒 <a href=\"{item['url']}\">Voir l'article</a>"
    )
    send_telegram(msg, image_url=item["image_url"])
    log.info(f"  ✅ {item['name'][:50]} ({pct})")

# ═══════════════════════════════════════════════════
#  BOUCLE PRINCIPALE
# ═══════════════════════════════════════════════════

def run():
    log.info("=== Mercari JP Bot ===")
    log.info(f"REFERENCE_DIR      : {REFERENCE_DIR.absolute()}")
    log.info(f"SIMILARITY_THRESHOLD: {SIMILARITY_THRESHOLD:.2f} ({SIMILARITY_THRESHOLD*100:.0f}%)")
    log.info(f"SCAN_INTERVAL      : {SCAN_INTERVAL}s")

    init_session()
    seen = load_seen()
    ref_hashes = load_ref_hashes()

    send_telegram(
        f"✅ <b>Bot démarré !</b>\n"
        f"📸 <b>{len(ref_hashes)}</b> images de référence\n"
        f"🔍 <b>{len(KEYWORDS)}</b> mots-clés surveillés\n"
        f"📊 Seuil : <b>{SIMILARITY_THRESHOLD * 100:.0f}%</b>\n"
        f"👥 <b>{len(CHAT_IDS)}</b> destinataire(s)\n"
        f"⏱ Scan toutes les <b>{SCAN_INTERVAL // 60}min{SCAN_INTERVAL % 60:02d}s</b>"
    )

    scan_count = 0
    while True:
        scan_count += 1
        log.info(f"─── Scan #{scan_count} · {datetime.now().strftime('%d/%m %H:%M:%S')} ───")

        ref_hashes = load_ref_hashes()
        new_matches = 0
        seen_this = set()

        for keyword in KEYWORDS:
            items = fetch_items(keyword)
            time.sleep(2)

            for item in items:
                iid = item["id"]
                if iid in seen or iid in seen_this:
                    continue
                seen_this.add(iid)
                seen.add(iid)

                if not ref_hashes:
                    continue

                sim, ref = compare(item["image_url"], ref_hashes)
                log.debug(f"    {item['name'][:40]} sim={sim:.2f}")

                if sim >= SIMILARITY_THRESHOLD:
                    notify(item, sim, ref)
                    new_matches += 1

        save_seen(seen)
        log.info(f"Scan #{scan_count} — {new_matches} match(s) — prochain dans {SCAN_INTERVAL}s")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info("Arrêt manuel.")
    except Exception as e:
        log.critical(f"Erreur fatale: {e}", exc_info=True)
        try:
            send_telegram(f"🚨 <b>Bot crashé !</b>\n<code>{str(e)[:300]}</code>")
        except Exception:
            pass
        raise
