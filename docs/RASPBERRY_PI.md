# Installer Bambu Control sur un Raspberry Pi 5 (console dédiée)

Résultat final : le Pi démarre, se connecte au réseau, lance Bambu Control
puis ouvre l'interface en plein écran, sans aucune intervention.

Testé pour : **Raspberry Pi OS 64 bits (Bookworm) « with desktop »**.
Fonctionne aussi sur d'autres Linux avec systemd (Debian, Ubuntu…) ;
le mode kiosk demande un bureau graphique.

---

## 1. Préparer le Raspberry Pi

1. Avec **Raspberry Pi Imager**, installe *Raspberry Pi OS (64-bit)* — la version
   **avec bureau** si tu veux l'écran en plein écran.
   Dans les options de l'Imager, configure dès maintenant le Wi-Fi, un nom
   d'utilisateur et active SSH (pratique pour la maintenance).
2. Démarre le Pi, puis mets-le à jour :

```bash
sudo apt update && sudo apt full-upgrade -y
```

3. Installe les outils nécessaires (souvent déjà présents) :

```bash
sudo apt install -y git python3-venv chromium-browser curl
```

> Sur les versions les plus récentes de Raspberry Pi OS, le paquet s'appelle
> `chromium` : si `chromium-browser` est introuvable, remplace-le par `chromium`.
> Bambu Control détecte les deux noms automatiquement.

> Imprimante **X1 / P2S / H2** (caméra RTSP) : installe aussi `sudo apt install -y ffmpeg`.
> A1, A1 mini, P1P, P1S : pas besoin.

## 2. Installer Bambu Control

```bash
cd ~
git clone <adresse-du-depot> Bambu-Control
cd Bambu-Control
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Raspberry Pi OS refuse les `pip install` hors environnement virtuel :
c'est pour ça qu'on crée `.venv`. Pense à refaire `source .venv/bin/activate`
à chaque nouveau terminal avant de lancer une commande Python du projet.

## 3. Premier lancement et compte administrateur

```bash
cd ~/Bambu-Control
source .venv/bin/activate
python3 app.py
```

Sur le Pi, ouvre Chromium à l'adresse **http://localhost:5000** : la page
propose de **créer le compte administrateur** (identifiant + mot de passe
d'au moins 8 caractères).

> Depuis un autre appareil (PC, téléphone) : `http://<ip-du-pi>:5000`.
> La page demande alors en plus un **code à 6 caractères**, affiché dans le
> terminal où tourne `python3 app.py`.

## 4. Ajouter l'imprimante

**Paramètres → Ajouter une imprimante**. Il te faut, sur l'écran de l'imprimante :

- **Adresse IP** : Paramètres → Réseau (WLAN)
- **Access code** : Paramètres → Réseau → Mode LAN
- **Numéro de série** : Paramètres → Appareil

Clique sur **Tester la connexion** avant d'ajouter : chaque point (réseau,
access code, numéro de série, carte SD, caméra) est vérifié séparément.

> Pour piloter l'imprimante (pause, lumière, températures…), active le
> **mode développeur** : Paramètres → Réseau → Mode LAN → Mode développeur.
> Sans lui, la surveillance fonctionne mais les commandes sont ignorées par
> le firmware — l'interface l'indique clairement.

## 5. Démarrage automatique et plein écran

Toujours dans **Paramètres → Démarrage** :

| Case | Effet |
|---|---|
| Démarrer Bambu Control avec la machine | service systemd *utilisateur*, aucun mot de passe sudo demandé |
| Ouvrir en plein écran au démarrage | Chromium en mode kiosk à l'ouverture de la session |

Clique sur **Appliquer**. La page affiche ensuite deux commandes propres au
Raspberry Pi, à lancer **une seule fois** dans un terminal (elles demandent
le mot de passe administrateur du Pi, l'appli ne peut donc pas le faire seule) :

```bash
sudo raspi-config nonint do_boot_behaviour B4
```

(démarrer directement sur le bureau, nécessaire au plein écran)

```bash
sudo raspi-config nonint do_blanking 1
```

(l'écran ne se met plus en veille)

Arrête ensuite le `python3 app.py` lancé à la main (Ctrl+C) et redémarre :

```bash
sudo reboot
```

Le Pi démarre → la session s'ouvre → le service lance Bambu Control →
Chromium affiche la console en plein écran.

> Si la page indique que le service démarrera « à l'ouverture de ta
> session » seulement, c'est suffisant avec le démarrage sur le bureau.
> Pour qu'il tourne même sans session ouverte (Pi sans écran) :
> `sudo loginctl enable-linger $USER`.

---

## Maintenance

| Je veux… | Commande |
|---|---|
| Fermer le plein écran | `bambu-kiosk stop` (ou **Alt+F4**) |
| Le rouvrir | `bambu-kiosk start` |
| Ne plus l'ouvrir au démarrage | `bambu-kiosk disable` |
| Le réactiver | `bambu-kiosk enable` |
| Voir l'état du kiosk | `bambu-kiosk status` |
| État du serveur | `systemctl --user status bambu-control` |
| Journaux en direct | `journalctl --user -u bambu-control -f` |
| Redémarrer le serveur | `systemctl --user restart bambu-control` |
| Arrêter le démarrage auto | Paramètres → Démarrage, décocher puis Appliquer |
| Mot de passe oublié | `systemctl --user stop bambu-control`, `python3 app.py --reset-admin`, puis `systemctl --user start bambu-control` et recréer le compte depuis la page |

Sans clavier branché : connecte-toi en SSH depuis un PC (`ssh utilisateur@ip-du-pi`)
puis utilise les mêmes commandes.

> Si `bambu-kiosk` n'est pas trouvé, ferme et rouvre le terminal (le dossier
> `~/.local/bin` est ajouté au PATH à l'ouverture de session), ou utilise
> `~/.local/bin/bambu-kiosk`.

### Mettre à jour Bambu Control

```bash
cd ~/Bambu-Control
git pull
source .venv/bin/activate
pip install -r requirements.txt
systemctl --user restart bambu-control
```

Tes réglages (`data/`) ne sont pas touchés par la mise à jour.

## En cas de problème

| Symptôme | Piste |
|---|---|
| Écran noir / « page inaccessible » au démarrage | `systemctl --user status bambu-control` puis les journaux |
| Le kiosk ne s'ouvre pas | `bambu-kiosk status` ; vérifier la connexion automatique au bureau (`sudo raspi-config` → System Options → Boot / Auto Login → Desktop Autologin) |
| Imprimante « Hors ligne » | Paramètres → **Tester** : le test dit quel réglage est faux |
| Caméra indisponible | fermer Bambu Handy / Studio (une seule caméra à la fois) ; X1/H2/P2S : `ffmpeg` installé et « LAN Mode Liveview » activé |
| Demande de connexion après un mois | la session dure 30 jours : reconnecte-toi une fois sur l'écran |
