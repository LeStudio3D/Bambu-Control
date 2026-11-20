# Bambu Control

Console de supervision pour imprimantes **Bambu Lab**, en réseau local.
Pensée pour un écran dédié (Raspberry Pi 5 + écran 15,6"), utilisable aussi
depuis un PC, une tablette ou un téléphone.

- Caméra en direct, plein écran
- Progression, temps restant / écoulé, couches
- Températures buse / plateau / chambre (actuelle + consigne)
- AMS : tous les AMS (AMS, AMS lite, AMS 2 Pro, AMS HT), couleur, type, niveau
  restant, bobine en cours, humidité
- **Séchage à distance** des AMS 2 Pro (45-65 °C) et AMS HT (45-85 °C) : filament,
  température, durée (1 à 24 h), rotation des bobines
- Pause, reprise, arrêt, lumière, vitesse, températures, ventilateurs —
  **avec confirmation réelle de l'imprimante** (jamais de faux « succès »)
- Fichiers de la carte SD : envoi avec progression, suppression, et lancement
  avec choix du plateau, **AMS (une bobine par filament) ou bobine externe**,
  nivellement du plateau, calibration du flux dynamique, timelapse (bloqué si
  le plateau est imprimé « par objet »)
- Historique : impressions, durées, taux de réussite, erreurs, filament, courbes
- **Coût de chaque impression** (filament au prix de Bambu Studio + électricité)
  et **total par mois**
- **Galerie** : vignette, photo de fin et **vidéo timelapse** de chaque impression
- **Stock de filament** : bobines, poids restant décompté à chaque impression,
  alerte avant de manquer, vérification au lancement
- Plusieurs imprimantes, test de connexion pas à pas
- Mode kiosk : le Pi démarre directement sur la console
- **Intelligence artificielle** (Ollama local, Ollama Cloud ou API compatible OpenAI) :
  détection d'échec par la caméra, vérification du plateau, assistant, résumés,
  explication des erreurs, file d'impression, automatisations

Fonctionne sous **Linux (dont Raspberry Pi OS 64 bits), Windows et macOS**,
avec Python 3.12 ou plus récent.

---

## Installation sur Raspberry Pi : une seule commande

Sur un Raspberry Pi avec **Raspberry Pi OS (64 bits, avec bureau)**, ouvre un
terminal et colle :

```bash
curl -fsSL https://raw.githubusercontent.com/LeStudio3D/Bambu-Control/main/deploy/install.sh | bash
```

Le script installe tout (Python, ffmpeg pour les timelapses, Chromium),
télécharge Bambu Control dans `~/Bambu-Control`, active le démarrage
automatique et le plein écran. Redémarre le Pi : la console s'ouvre toute
seule. **Pour mettre à jour plus tard, relance la même commande** (tes réglages
ne sont pas touchés).

## Installation manuelle (Linux, Windows, macOS)

```bash
git clone https://github.com/LeStudio3D/Bambu-Control.git
cd Bambu-Control
python3 -m venv .venv
source .venv/bin/activate          # Windows : .venv\Scripts\activate
pip install -r requirements.txt
```

## Lancement et configuration

```bash
python3 app.py
```

Ouvre **http://localhost:5000** (ou `http://<ip-de-la-machine>:5000` depuis
un autre appareil du réseau). **Toute la configuration se fait dans l'interface** :

1. Au premier lancement, la page propose de **créer le compte administrateur**.
   Depuis un autre appareil que la machine elle-même, elle demande aussi un
   code à 6 caractères affiché dans le terminal du serveur (pour qu'un voisin
   de réseau ne puisse pas créer le compte à ta place).
2. **Paramètres → Ajouter une imprimante**.
3. **Paramètres → Compte / Serveur / Démarrage** : mot de passe, port,
   démarrage automatique et plein écran (Linux).

Mot de passe oublié : `python3 app.py --reset-admin`, puis relance
`python3 app.py` et recrée le compte depuis la page (imprimantes et
historique conservés).

Dans **Paramètres → Ajouter une imprimante**, le bouton **Tester la connexion** vérifie séparément le réseau,
l'access code, le numéro de série, la carte SD et la caméra.

Où trouver les informations sur l'écran de l'imprimante :

| Champ | Emplacement |
|---|---|
| Adresse IP | Paramètres → Réseau (WLAN) |
| Access code | Paramètres → Réseau → Mode LAN |
| Numéro de série | Paramètres → Appareil |

## Console Raspberry Pi (démarrage automatique + plein écran)

Guide complet pas à pas : **[docs/RASPBERRY_PI.md](docs/RASPBERRY_PI.md)**.

En résumé : dans **Paramètres → Démarrage**, coche « Démarrer avec la machine »
(service systemd utilisateur, sans sudo) et « Plein écran au démarrage »
(Chromium en mode kiosk). Pour la maintenance :

```bash
bambu-kiosk stop
```

(`start`, `stop`, `enable`, `disable`, `status` — ou **Alt+F4** pour fermer.)

### Démarrage automatique sous Windows / macOS

- **Windows** : Planificateur de tâches → Créer une tâche → Déclencheur
  « À l'ouverture de session » → Action « Démarrer un programme » :
  `C:\chemin\Bambu-Control\.venv\Scripts\pythonw.exe`, argument `app.py`,
  « Commencer dans » `C:\chemin\Bambu-Control`.
- **macOS** : créer `~/Library/LaunchAgents/bambu-control.plist` :

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>bambu-control</string>
  <key>ProgramArguments</key><array>
    <string>/Users/MOI/Bambu-Control/.venv/bin/python</string>
    <string>/Users/MOI/Bambu-Control/app.py</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/MOI/Bambu-Control</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
```

puis `launchctl load ~/Library/LaunchAgents/bambu-control.plist`.

---

## Coûts, galerie et stock de filament

- **Coût** : au début de chaque impression, Bambu Control lit sur la carte SD le
  fichier lancé (même depuis Bambu Studio ou l'écran de l'imprimante) : plateau,
  bobines utilisées, grammes et **prix au kilo de chaque filament réglés dans
  Bambu Studio**. À la fin, il ajoute l'électricité (durée × puissance moyenne ×
  prix du kWh, réglables dans **Paramètres → Coût des impressions**). Une
  impression arrêtée en route est comptée au prorata. Totaux par mois dans
  l'**Historique**.
- **Galerie** (Historique) : vignette du fichier, photo de fin d'impression
  (caméra, même sans IA) et **vidéo timelapse** si elle a été activée au
  lancement. Les vidéos sont copiées depuis la carte SD de l'imprimante (dossier
  `timelapse`) et converties en MP4 si **ffmpeg** est installé (indispensable
  pour les A1 / P1 qui enregistrent en AVI). Bouton **Récupérer les timelapses**
  pour les vidéos plus anciennes.
- **Stock de filament** (menu **Filament**) : ajoute tes bobines (poids, prix,
  seuil d'alerte) et pose-les sur un emplacement AMS ou le support externe. Le
  filament de chaque impression est décompté automatiquement ; une alerte
  prévient quand une bobine passe sous son seuil, et la fenêtre de lancement
  signale une bobine qui n'a pas assez de filament pour le plateau choisi.

Les médias sont rangés dans `data/media/` (une sous-dossier par impression).

## Accès depuis l'extérieur (hors de la maison)

Bambu Control a son propre compte (identifiant + mot de passe), donc il peut
être ouvert sur Internet par une **redirection de port** de la box :

1. Donne une adresse IP fixe à la machine qui fait tourner Bambu Control
   (box → « Baux DHCP statiques » / « IP fixe »).
2. Box → **Redirection de ports** : port externe au choix (ex. `8443`, évite
   `5000` qui est très scanné) → IP de la machine, port `5000`, protocole TCP.
   Sur une **Livebox** : *Configuration avancée → Réseau → NAT/PAT*.
3. Depuis l'extérieur : `http://<ton-ip-publique>:8443` (ou un nom DynDNS).

**À savoir avant d'ouvrir le port :**

- En `http://`, le mot de passe circule **en clair** sur Internet (un Wi-Fi
  public peut le lire). Le mieux est de passer par **HTTPS** avec un *reverse
  proxy* : celui de ton NAS (Synology : *Portail de connexion → Proxy inversé*,
  avec un certificat Let's Encrypt gratuit), Nginx Proxy Manager ou Caddy. La
  box redirige alors le port 443 vers le proxy, qui transmet à Bambu Control.
- Choisis un **mot de passe long** (12 caractères ou plus) : il protège la
  caméra et les commandes de l'imprimante. Les essais répétés sont ralentis
  automatiquement.
- Garde le compte administrateur **créé avant** d'ouvrir le port (la page de
  création de compte ne doit jamais être visible depuis Internet).
- Alternative sans port ouvert : un VPN comme **Tailscale** ou le **VPN de la
  Livebox / du NAS**, puis l'adresse locale comme à la maison.

## Intelligence artificielle

Tout se règle dans **Paramètres → Intelligence artificielle**, avec deux
services indépendants : un pour le **texte** (assistant, erreurs, résumés) et
un pour les **images** (caméra, plateau, vignettes). On peut donc mélanger
local et cloud, par exemple :

- Texte : Ollama sur le Raspberry Pi (`http://localhost:11434`), modèle `qwen3.5:2b`
- Images : Ollama Cloud (`https://ollama.com` + clé API), modèle `gemma4:31b`

La case « Utiliser le même service que pour le texte » réutilise la connexion
du bloc texte pour les images. Chaque bloc a ses propres réglages :

| Réglage | Exemple |
|---|---|
| Fournisseur | Ollama (local / réseau), Ollama Cloud, ou API compatible OpenAI |
| Adresse | `http://localhost:11434` (Ollama local), `https://ollama.com` (cloud) |
| Clé API | nécessaire pour le cloud — stockée sur la machine, jamais renvoyée au navigateur |
| Modèle texte / vision | le bouton **Tester et charger les modèles** propose la liste |

Un modèle « vision » (qui comprend les images, ex. `qwen3.5:2b`, `llava`) est
nécessaire pour tout ce qui utilise la caméra. Un Raspberry Pi est trop peu
puissant pour faire tourner ces modèles : utilise Ollama sur un PC du réseau
(avec `OLLAMA_HOST=0.0.0.0` sur ce PC) ou Ollama Cloud.

| Fonction | Où | IA ? |
|---|---|---|
| Détection d'échec (spaghetti, décollement…) toutes les N minutes, alerte + photo | automatique | vision |
| Pause automatique si échec détecté (seuil de confiance réglable) | automatique, désactivée par défaut | vision |
| Vérification que le plateau est vide avant chaque lancement | Impressions, file | vision |
| Photo + commentaire en fin d'impression | Historique | vision |
| Description de la vignette d'un fichier `.3mf` | Impressions → Infos | vision |
| Assistant (questions, actions proposées à confirmer) | bouton **Assistant** | texte |
| Explication d'un code d'erreur | Accueil, Historique | texte |
| Résumé, tendances et conseils | Historique, résumé quotidien | texte |
| Durée, poids, filaments d'un `.3mf` + bobine AMS conseillée | Impressions → Infos | non |
| Alerte si une bobine risque de manquer | automatique | non |
| Éteindre la lumière en fin d'impression | automatique | non |
| File d'impression (le suivant est proposé, tu confirmes) | Impressions, Accueil | non (plateau : vision) |

**Garde-fous** : l'IA ne lance ni n'arrête jamais une impression et ne change
rien sans ton clic. Les actions proposées par l'assistant passent par la même
confirmation que les boutons. Seule la pause automatique est possible, et
seulement si tu l'actives.

## Mode LAN et mode développeur

Bambu Control parle directement à l'imprimante sur le réseau local (MQTT,
FTPS, caméra). Depuis les firmwares de 2025, l'imprimante **ignore les
commandes** des applications tierces tant que le **mode développeur** n'est
pas activé (Paramètres → Réseau → Mode LAN → Mode développeur).

| Fonction | Mode LAN seul | Mode LAN + développeur |
|---|---|---|
| Surveillance, caméra, AMS, historique | ✅ | ✅ |
| Envoi / suppression de fichiers (carte SD) | ✅ | ✅ |
| Pause, reprise, arrêt, lumière, températures, vitesse, ventilateurs | ❌ ignorées | ✅ |
| Lancer une impression à distance | ❌ ignoré | ✅ |

Chaque commande attend la preuve que l'imprimante l'a appliquée. Si rien ne
change, l'interface affiche « non confirmée » et rappelle comment activer le
mode développeur. Le mode cloud (passer par le compte Bambu) n'est pas géré.

## Compatibilité des modèles

| Modèles | Caméra | Capteur de chambre | Ventilateurs pilotables |
|---|---|---|---|
| A1, A1 mini, A2L | JPEG (port 6000) | non | pièce |
| P1P | JPEG (port 6000) | non | pièce |
| P1S | JPEG (port 6000) | non | pièce, auxiliaire, chambre |
| P2S, X1, X1 Carbon, X1E, X2D, H2D, H2D Pro, H2S, H2C, H3C | RTSP (port 322) — **ffmpeg requis** | oui | pièce, auxiliaire, chambre |

Le capteur de chambre peut être forcé par imprimante (capteur ajouté soi-même).
Caméra RTSP : `sudo apt install ffmpeg` (Linux) et activer « LAN Mode Liveview ».
Une seule application à la fois peut souvent utiliser la caméra : la caméra se
met en veille quand personne ne regarde, pour laisser la place à Bambu Handy.

## Sécurité

- Un compte admin créé depuis l'interface (code de sécurité si ce n'est pas depuis la machine elle-même),
  mot de passe hashé, sessions de 30 jours, 5 essais ratés → blocage 5 minutes
- Protection CSRF sur toutes les actions, redirections limitées au site lui-même
- Access code et numéro de série **jamais envoyés au navigateur**
- Noms de fichiers nettoyés, extensions et adresses IP vérifiées, taille d'envoi limitée (512 Mo)
- Toutes les données sensibles sont dans `data/` (ignoré par git)

Bambu Control est fait pour un **réseau local**. Ne l'expose pas directement
sur Internet ; pour un accès à distance, passe par un VPN (WireGuard, Tailscale…).

## Projet sur un NAS (dossier partagé)

Le dossier Bambu Control peut être rangé sur un NAS (ex. `\\mon-nas\partage`
monté en lecteur réseau) et lancé depuis un PC. Deux règles :

- **Une seule copie de Bambu Control à la fois** doit utiliser un même dossier
  `data/` (la base de l'historique s'abîmerait si deux serveurs écrivaient
  dedans en même temps). Le Raspberry Pi garde donc son propre dossier `data/`.
- Les écritures dans la base sont plus lentes sur un partage réseau (sans
  conséquence ici : au plus une toutes les 30 secondes).

## Données et mise à jour

| Fichier | Contenu |
|---|---|
| `data/settings.json` | compte admin (hash), clé secrète, port, options |
| `data/printers.json` | imprimantes (IP, access code, numéro de série) |
| `data/bambu_history.db` | historique SQLite (relevés conservés 90 jours), alertes IA |
| `data/photos/` | photos des alertes et de fin d'impression |
| `data/queue.json` | file d'impression |

```bash
git pull
pip install -r requirements.txt
```

puis redémarrer (`systemctl --user restart bambu-control` en mode console).
Le dossier `data/` n'est jamais touché par une mise à jour.

## Structure du projet

```
app.py              Serveur Flask : pages et API
auth.py             Connexion, CSRF, limitation des essais
settings_store.py   Réglages (data/settings.json)
printers_store.py   Imprimantes (data/printers.json)
models.py           Modèles Bambu : caméra, chambre, ventilateurs
bambu_client.py     Une connexion par imprimante : MQTT, état, hors ligne, reconnexion
commands.py         Commandes + attente de confirmation
camera.py           Caméra (JPEG port 6000 ou RTSP via ffmpeg), à la demande
history.py          Historique SQLite : impressions, erreurs, relevés
sd_card.py          Carte SD (FTPS) : liste, envoi, suppression
printer_check.py    Test de connexion pas à pas
appliance.py        Démarrage automatique (systemd utilisateur) + mode kiosk
ai_client.py        Connexion aux services IA (Ollama, Ollama Cloud, API OpenAI)
ai_features.py      Fonctions IA : consignes, analyse caméra, assistant, résumés
ai_monitor.py       Surveillance automatique et automatisations (thread de fond)
ai_routes.py        Routes web de l'IA, de la file et des infos de fichiers
print_queue.py      File d'impression (data/queue.json)
threemf.py          Lecture des fichiers .3mf (durée, poids, filaments, prix, vignette)
job_extras.py       Coût, stock et timelapse de chaque impression (thread de fond)
spools.py           Stock de filament (bobines, décompte, alertes)
extras_routes.py    Routes web du stock, des coûts et des médias
deploy/             Installation en une commande, service systemd, script bambu-kiosk
templates/          Pages HTML (base.html + 6 pages + connexion)
static/             CSS console, JavaScript par page
docs/               Guide Raspberry Pi
```

## Limites connues

- Le niveau restant des bobines n'est connu que pour les bobines Bambu Lab ;
  le filament consommé est une estimation en % de bobine.
- L'envoi vers la carte SD n'affiche pas de progression pour sa seconde moitié
  (machine → imprimante) : la librairie ne la fournit pas.
- Mode cloud non géré (LAN uniquement).
