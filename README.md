# 🤖 Mercari JP Bot — Surveillance Running

Bot qui surveille Mercari Japon et t'envoie une notification Telegram quand un article ressemble à tes photos de référence.

---

## 📋 Fonctionnement

1. Interroge l'API Mercari JP toutes les **2 minutes** avec 4 mots-clés :
   - `ナイキ ランニング` (Nike Running)
   - `nike running`
   - `アンダーアーマー ランニング` (Under Armour Running)
   - `under armour running`
2. Pour chaque nouvel article, télécharge sa photo et calcule un **pHash** (perceptual hash)
3. Compare ce hash avec toutes tes **images de référence** — distance de Hamming
4. Si la similarité ≥ au seuil (80% par défaut) → **notification Telegram** avec photo + lien

---

## 🚀 Déploiement sur Railway

### Étape 1 — Bot Telegram

1. Ouvre Telegram → cherche **@BotFather** → `/newbot`
2. Copie le **token** (ex: `123456:ABCdef...`)
3. Cherche **@userinfobot** → envoie-lui un message → copie ton **Chat ID**

### Étape 2 — Structure des fichiers

```
mercari-bot/
├── bot.py
├── requirements.txt
├── Dockerfile
├── railway.toml
└── reference_images/
    ├── nike_running_1.jpg
    ├── nike_running_2.jpg
    ├── under_armour_1.jpg
    └── ...
```

### Étape 3 — Variables d'environnement sur Railway

| Variable | Exemple | Description |
|---|---|---|
| `TELEGRAM_TOKEN` | `123456:ABCdef...` | **Obligatoire** — Token BotFather |
| `TELEGRAM_CHAT_IDS` | `111222,333444,555666` | **Obligatoire** — Chat IDs séparés par virgule |
| `SIMILARITY_THRESHOLD` | `0.80` | Seuil de similarité (défaut : 0.80) |
| `SCAN_INTERVAL` | `120` | Secondes entre chaque scan (défaut : 120) |
| `REFERENCE_DIR` | `reference_images` | Dossier des images (défaut : reference_images) |
| `MAX_PRICE` | `15000` | Prix max en ¥ — 0 = pas de limite |
| `MIN_PRICE` | `1000` | Prix min en ¥ — 0 = pas de limite |

⚠️ **Ne jamais mettre le token dans le code source** — toujours via les variables Railway.

### Étape 4 — Images de référence

Les images sont incluses dans le Docker image via le dépôt Git.
Mets tes photos dans `reference_images/` avant de pusher sur GitHub.

- Formats : `.jpg`, `.jpeg`, `.png`, `.webp`
- Plus tu en mets, meilleure est la détection
- Les images sont rechargées à chaque scan (ajout sans redémarrage)

---

## 📱 Exemple de notification

```
🔥 Match trouvé !
━━━━━━━━━━━━━━━━━━
👕 ナイキ ランニング ジャケット M
💴 8,500 ¥
🔍 Mot-clé : ナイキ ランニング
📊 Similarité : 84.3%
🖼 Référence : nike_running_division_1.jpg
🛒 Voir l'article
```

---

## 🔧 Lancer en local

```bash
pip install -r requirements.txt

mkdir reference_images
cp tes_photos/*.jpg reference_images/

export TELEGRAM_TOKEN="123456:ABCdef..."
export TELEGRAM_CHAT_IDS="111222,333444"
python bot.py
```

---

## ⚙️ Réglage du seuil

| Seuil | Comportement |
|---|---|
| `0.70` | Large — beaucoup de résultats, quelques faux positifs |
| `0.80` | Équilibré — recommandé pour démarrer |
| `0.85` | Strict — peu de faux positifs, peut rater des articles |

---

## 💡 Comment fonctionne le pHash

Le **perceptual hash** (pHash) réduit chaque image à une empreinte de 64 bits qui capture ses formes et contrastes principaux. Deux images similaires ont des empreintes proches. La **distance de Hamming** mesure le nombre de bits différents entre deux empreintes :

- Distance 0 = images identiques
- Distance 64 = images complètement différentes
- Seuil 80% ≈ distance ≤ 12 bits
