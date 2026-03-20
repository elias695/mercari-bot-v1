import os, time, json, logging, requests, uuid, base64, hmac, hashlib, re
from pathlib import Path
from datetime import datetime, timezone
from PIL import Image
import imagehash
from io import BytesIO

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════════════

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_IDS       = [c.strip() for c in os.environ["TELEGRAM_CHAT_IDS"].split(",")]

# Accepte "0.80" ou "80" — Railway peut envoyer l'un ou l'autre
_raw_thresh = float(os.getenv("SIMILARITY_THRESHOLD", "0.80"))
SIMILARITY_THRESHOLD = _raw_thresh / 100.0 if _raw_thresh > 1.0 else _raw_thresh

SCAN_INTERVAL  = int(os.getenv("SCAN_INTERVAL", "120"))
REFERENCE_DIR  = Path(os.getenv("REFERENCE_DIR", "reference_images"))
SEEN_FILE      = Path("seen_items.json")
MAX_PRICE      = int(os.getenv("MAX_PRICE", "0"))
MIN_PRICE      = int(os.getenv("MIN_PRICE", "0"))

KEYWORDS = [
    "ナイキ ランニング",
    "nike running",
    "アンダーアーマー ランニング",
    "under armour running",
]

_SEARCH_URL     = "https://api.mercari.jp/v2/entities:search"
_IMG_SEARCH_URL = "https://api.mercari.jp/entities:searchByImage"

UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

# ═══════════════════════════════════════════════════════════════════
#  DPoP JWT — clé extraite dynamiquement depuis le JS de Mercari
# ═══════════════════════════════════════════════════════════════════

_dpop_key_cache: bytes | None = None


def _fetch_dpop_key() -> bytes:
    global _dpop_key_cache
    if _dpop_key_cache:
        return _dpop_key_cache

    try:
        r = requests.get(
            "https://jp.mercari.com/",
            headers={"User-Agent": UA, "Accept-Language": "ja-JP"},
            timeout=15,
        )
        js_urls = re.findall(r'src="(/_next/static/chunks/[^"]+\.js)"', r.text)

        for js_url in js_urls[:25]:
            try:
                js = requests.get(
                    "https://jp.mercari.com" + js_url,
                    headers={"User-Agent": UA},
                    timeout=10,
                ).text
                # La clé est une chaîne base64 de 44 chars (= 32 bytes décodés)
                for m in re.finditer(r'"([A-Za-z0-9+/]{43}=)"', js):
                    candidate = m.group(1)
                    try:
                        decoded = base64.b64decode(candidate)
                        if len(decoded) == 32:
                            log.info(f"Clé DPoP extraite depuis {js_url}")
                            _dpop_key_cache = decoded
                            return decoded
                    except Exception:
                        continue
            except Exception:
                continue
    except Exception as e:
        log.warning(f"Extraction clé DPoP échouée: {e}")

    log.warning("Clé DPoP de fallback utilisée")
    fallback = base64.b64decode("2Vuvzl5oVXEMgADSSSHBmO33lSDa0dJK6CzKlxEE/5Y=")
    _dpop_key_cache = fallback
    return fallback


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _make_dpop(url: str, method: str = "POST") -> str:
    key     = _fetch_dpop_key()
    header  = {"typ": "dpop+jwt", "alg": "HS256"}
    now     = int(datetime.now(timezone.utc).timestamp())
    payload = {"jti": str(uuid.uuid4()), "htm": method.upper(),
               "htu": url, "iat": now, "exp": now + 60}
    h   = _b64url(json.dumps(header,  separators=(",", ":")).encode())
    p   = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(key, f"{h}.{p}".encode(), hashlib.sha256).digest()
    return f"{h}.{p}.{_b64url(sig)}"


def _api_headers(url: str, method: str = "POST") -> dict:
    return {
        "User-Agent":      UA,
        "Accept":          "application/json",
        "Accept-Language": "ja-JP,ja;q=0.9",
        "Content-Type":    "application/json",
        "Origin":          "https://jp.mercari.com",
        "Referer":         "https://jp.mercari.com/",
        "X-Platform":      "web",
        "DPoP":            _make_dpop(url, method),
    }


def _reset_dpop_key():
    global _dpop_key_cache
    _dpop_key_cache = None

# ═══════════════════════════════════════════════════════════════════
#  PERSISTANCE
# ═══════════════════════════════════════════════════════════════════

def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            return set()
    return set()


def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(list(seen)))

# ═══════════════════════════════════════════════════════════════════
#  IMAGES DE RÉFÉRENCE
# ═══════════════════════════════════════════════════════════════════

def load_ref_images() -> list[tuple[str, Path]]:
    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
        files.extend(REFERENCE_DIR.glob(ext))

    log.info(f"{len(files)} images dans '{REFERENCE_DIR.absolute()}'")
    if not files:
        contents = [f.name for f in REFERENCE_DIR.iterdir()] if REFERENCE_DIR.exists() else []
        log.warning(f"Contenu du dossier: {contents}")

    return [(f.name, f) for f in sorted(files)]


def load_ref_hashes(ref_images: list) -> list[tuple[str, imagehash.ImageHash]]:
    hashes = []
    for name, path in ref_images:
        try:
            img = Image.open(path).convert("RGB")
            hashes.append((name, imagehash.phash(img)))
        except Exception as e:
            log.warning(f"Image ignorée {name}: {e}")
    return hashes


def local_similarity(img_url: str, ref_hashes: list) -> tuple[float, str]:
    if not img_url or not ref_hashes:
        return 0.0, ""
    try:
        r = requests.get(img_url, timeout=10, headers={"User-Agent": UA})
        r.raise_for_status()
        item_hash = imagehash.phash(Image.open(BytesIO(r.content)).convert("RGB"))
    except Exception:
        return 0.0, ""

    best_sim, best_ref = 0.0, ""
    for ref_name, ref_hash in ref_hashes:
        sim = 1.0 - (item_hash - ref_hash) / 64.0
        if sim > best_sim:
            best_sim, best_ref = sim, ref_name
    return best_sim, best_ref

# ═══════════════════════════════════════════════════════════════════
#  RECHERCHE PAR MOT-CLÉ
# ═══════════════════════════════════════════════════════════════════

def fetch_by_keyword(keyword: str, limit: int = 30) -> list[dict]:
    payload = {
        "pageSize": limit, "pageToken": "",
        "searchSessionId": str(uuid.uuid4()),
        "indexRouting": "INDEX_ROUTING_UNSPECIFIED",
        "thumbnailTypes": [],
        "searchCondition": {
            "keyword": keyword, "excludeKeyword": "",
            "sort": "SORT_CREATED_TIME", "order": "ORDER_DESC",
            "status": ["STATUS_ON_SALE"],
            "sizeId": [], "categoryId": [], "brandId": [], "sellerId": [],
            "priceMin": MIN_PRICE if MIN_PRICE > 0 else 0,
            "priceMax": MAX_PRICE if MAX_PRICE > 0 else 0,
            "itemConditionId": [], "shippingPayerId": [], "shippingFromArea": [],
            "shippingMethod": [], "colorId": [], "hasCoupon": False,
            "attributes": [], "itemTypes": [], "skuIds": [],
        },
        "serviceFrom": "suruga",
        "withItemBrand": True, "withItemSize": False,
        "withItemPromotions": True, "withItemSizes": True, "withShopname": False,
        "userId": "", "userSessionId": "", "fromPage": "",
    }
    items = []
    try:
        resp = requests.post(
            _SEARCH_URL, json=payload,
            headers=_api_headers(_SEARCH_URL), timeout=15,
        )
        resp.raise_for_status()
        for item in resp.json().get("items", []):
            item_id = str(item.get("id", "")).strip()
            if not item_id:
                continue
            th = item.get("thumbnails") or []
            items.append({
                "id": item_id, "name": item.get("name", ""),
                "price": int(item.get("price", 0) or 0),
                "image_url": th[0] if th else "",
                "url": f"https://jp.mercari.com/item/{item_id}",
                "source": f"mot-clé: {keyword}",
            })
    except requests.HTTPError as e:
        st = getattr(e.response, "status_code", "?")
        log.warning(f"HTTP {st} pour '{keyword}': {getattr(e.response,'text','')[:200]}")
        if st == 401:
            _reset_dpop_key()
    except Exception as e:
        log.warning(f"Erreur '{keyword}': {e}")
    log.info(f"  kw '{keyword}' → {len(items)} articles")
    return items

# ═══════════════════════════════════════════════════════════════════
#  RECHERCHE PAR IMAGE (searchByImage)
# ═══════════════════════════════════════════════════════════════════

def fetch_by_image(ref_name: str, ref_path: Path) -> list[dict]:
    items = []
    try:
        ext  = ref_path.suffix.lower().lstrip(".")
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                "png": "image/png", "webp": "image/webp"}.get(ext, "image/jpeg")

        with open(ref_path, "rb") as f:
            img_bytes = f.read()

        # Headers sans Content-Type (multipart géré par requests)
        headers = {
            "User-Agent":      UA,
            "Accept":          "application/json",
            "Accept-Language": "ja-JP,ja;q=0.9",
            "Origin":          "https://jp.mercari.com",
            "Referer":         "https://jp.mercari.com/",
            "X-Platform":      "web",
            "DPoP":            _make_dpop(_IMG_SEARCH_URL, "POST"),
        }

        resp = requests.post(
            _IMG_SEARCH_URL,
            files={"image": (ref_path.name, img_bytes, mime)},
            headers=headers,
            timeout=20,
        )
        resp.raise_for_status()

        for item in resp.json().get("items", []):
            item_id = str(item.get("id", "")).strip()
            if not item_id:
                continue
            th = item.get("thumbnails") or []
            items.append({
                "id": item_id, "name": item.get("name", ""),
                "price": int(item.get("price", 0) or 0),
                "image_url": th[0] if th else "",
                "url": f"https://jp.mercari.com/item/{item_id}",
                "source": f"image: {ref_name}",
                "_img_ref": ref_name,
                "_img_sim": 0.90,  # Mercari a déjà filtré, on suppose ~90%
            })

    except requests.HTTPError as e:
        st = getattr(e.response, "status_code", "?")
        log.warning(f"  searchByImage HTTP {st} '{ref_name}': {getattr(e.response,'text','')[:150]}")
        if st == 401:
            _reset_dpop_key()
    except Exception as e:
        log.warning(f"  searchByImage erreur '{ref_name}': {e}")

    log.info(f"  img '{ref_name}' → {len(items)} articles")
    return items

# ═══════════════════════════════════════════════════════════════════
#  TELEGRAM
# ═══════════════════════════════════════════════════════════════════

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
                log.warning(f"Telegram erreur chat {chat_id}: {ex}")


def notify(item: dict, similarity: float, ref_name: str):
    pct = f"{similarity * 100:.1f}%"
    msg = (
        f"🔥 <b>Match trouvé !</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👕 <b>{item['name']}</b>\n"
        f"💴 <b>{item['price']:,} ¥</b>\n"
        f"🔍 Source : <i>{item.get('source','')}</i>\n"
        f"📊 Similarité : <b>{pct}</b>\n"
        f"🖼 Référence : <code>{ref_name}</code>\n"
        f"🛒 <a href=\"{item['url']}\">Voir l'article</a>"
    )
    send_telegram(msg, image_url=item["image_url"])
    log.info(f"  ✅ Notifié : {item['name'][:50]} ({pct})")

# ═══════════════════════════════════════════════════════════════════
#  BOUCLE PRINCIPALE
# ═══════════════════════════════════════════════════════════════════

def run():
    log.info("=== Mercari JP Bot ===")
    log.info(f"REFERENCE_DIR absolu : {REFERENCE_DIR.absolute()}")
    log.info(f"SIMILARITY_THRESHOLD : {SIMILARITY_THRESHOLD:.2f} ({SIMILARITY_THRESHOLD*100:.0f}%)")
    log.info(f"SCAN_INTERVAL        : {SCAN_INTERVAL}s")

    seen = load_seen()
    ref_images = load_ref_images()
    ref_hashes = load_ref_hashes(ref_images)

    send_telegram(
        f"✅ <b>Bot démarré !</b>\n"
        f"📸 {len(ref_hashes)} images de référence\n"
        f"🔍 {len(KEYWORDS)} mots-clés\n"
        f"🖼 Recherche par image : <b>{'✅ activée' if ref_images else '⚠️ 0 images trouvées'}</b>\n"
        f"📊 Seuil : <b>{SIMILARITY_THRESHOLD * 100:.0f}%</b>\n"
        f"👥 {len(CHAT_IDS)} destinataire(s)\n"
        f"⏱ Scan toutes les {SCAN_INTERVAL // 60}min{SCAN_INTERVAL % 60:02d}s"
    )

    scan_count = 0
    while True:
        scan_count += 1
        log.info(f"─── Scan #{scan_count} · {datetime.now().strftime('%d/%m %H:%M:%S')} ───")

        ref_images = load_ref_images()
        ref_hashes = load_ref_hashes(ref_images)

        all_items: list[dict] = []

        # Mode 1 : mots-clés
        for keyword in KEYWORDS:
            all_items.extend(fetch_by_keyword(keyword))
            time.sleep(1.5)

        # Mode 2 : recherche par image (une requête par image de référence)
        for ref_name, ref_path in ref_images:
            all_items.extend(fetch_by_image(ref_name, ref_path))
            time.sleep(1.0)

        # Déduplication et notification
        new_matches  = 0
        seen_now     = set()

        for item in all_items:
            item_id = item["id"]
            if item_id in seen or item_id in seen_now:
                continue
            seen_now.add(item_id)
            seen.add(item_id)

            # Vient de searchByImage → notifie directement
            if "_img_ref" in item:
                notify(item, item["_img_sim"], item["_img_ref"])
                new_matches += 1
                continue

            # Vient d'un mot-clé → compare localement
            if not ref_hashes:
                continue
            sim, ref_name = local_similarity(item["image_url"], ref_hashes)
            log.debug(f"    {item['name'][:40]} sim={sim:.2f}")
            if sim >= SIMILARITY_THRESHOLD:
                notify(item, sim, ref_name)
                new_matches += 1

        save_seen(seen)
        log.info(f"Scan #{scan_count} fini — {new_matches} match(s) — prochain dans {SCAN_INTERVAL}s")
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
