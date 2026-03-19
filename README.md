# 🤖 Mercari JP Bot — Surveillance Running

Bot qui surveille Mercari Japon et t'envoie une notification Telegram quand un article ressemble à tes photos de référence.

---

## 📋 Fonctionnement

1. Scrape Mercari JP toutes les **5 minutes** avec 4 mots-clés :
   - `ナイキ ランニング` (Nike Running en japonais)
   - `nike running`
   - `アンダーアーマー` (Under Armour en japonais)
   - `under armour running`
2. Compare chaque photo d'article avec tes **images de référence** via IA (CLIP)
3. Envoie une **notification Telegram** avec photo + lien si match ≥ 80%

---

## 🚀 Installation & Déploiement sur Railway (gratuit)

### Étape 1 — Créer ton bot Telegram

1. Ouvre Telegram, cherche **@BotFather**
2. Envoie `/newbot` → suis les instructions
3. Copie le **token** (ex: `123456:ABCdef...`)
4. Cherche **@userinfobot** et envoie-lui un message → copie ton **Chat ID**

### Étape 2 — Préparer les fichiers

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
    └── ... (toutes tes photos)
```

➡️ Mets toutes tes photos de running dans le dossier `reference_images/`

### Étape 3 — Déployer sur Railway

1. Va sur [railway.app](https://railway.app) → **New Project → Deploy from GitHub**
2. Upload le dossier `mercari-bot` sur GitHub (ou utilise Railway CLI)
3. Dans Railway → **Variables** → ajoute :

| Variable | Valeur |
|---|---|
| `TELEGRAM_TOKEN` | `ton_token_botfather` |
| `TELEGRAM_CHAT_ID` | `ton_chat_id` |
| `SIMILARITY_THRESHOLD` | `0.80` (80% de similarité) |
| `SCAN_INTERVAL` | `300` (scan toutes les 5 min) |

4. **Deploy** → le bot tourne 24/7 !

---

## ⚙️ Variables d'environnement

| Variable | Défaut | Description |
|---|---|---|
| `TELEGRAM_TOKEN` | — | **Obligatoire** — Token du bot Telegram |
| `TELEGRAM_CHAT_ID` | — | **Obligatoire** — Ton Chat ID Telegram |
| `SIMILARITY_THRESHOLD` | `0.80` | Seuil de similarité (0.75 = large, 0.85 = strict) |
| `SCAN_INTERVAL` | `300` | Délai entre scans en secondes |
| `REFERENCE_DIR` | `reference_images` | Dossier des images de référence |

---

## 📸 Images de référence

- Mets **toutes tes photos** de running Nike/Under Armour dans `reference_images/`
- Formats acceptés : `.jpg`, `.jpeg`, `.png`, `.webp`
- Plus tu en mets, meilleure est la détection
- Le bot recharge les images à chaque scan → tu peux en ajouter sans redémarrer

---

## 📱 Exemple de notification Telegram

```
🔥 Match trouvé !
━━━━━━━━━━━━━━━━━━
👕 ナイキ ランニング ジャケット M
💴 8,500 ¥
🔍 Mot-clé : ナイキ ランニング
📊 Similarité : 84.3%
🖼 Référence : nike_running_division_1.jpg
🛒 [Voir l'article]
```

---

## 🔧 Lancer en local (test)

```bash
pip install -r requirements.txt

# Crée le dossier et ajoute tes photos
mkdir reference_images
cp tes_photos/*.jpg reference_images/

# Lance le bot
TELEGRAM_TOKEN="xxx" TELEGRAM_CHAT_ID="yyy" python bot.py
```

---

## 💡 Conseils

- **Seuil 0.75** = détection large (plus de faux positifs)
- **Seuil 0.85** = détection stricte (moins de notifications)
- Commence avec **0.80** et ajuste selon tes résultats
- Les articles déjà vus sont mémorisés dans `seen_items.json`
