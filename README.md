# Bambu Control — Dashboard

Poste de surveillance web pour imprimante Bambu Lab A1 : caméra en direct,
températures, progression, AMS, historique, et envoi de G-code vers la carte SD.

## Installation

```bash
git clone https://github.com/LeStudio3D/Bambu-Control.git
cd Bambu-Control
pip install -r requirements.txt
```

## Configuration (obligatoire avant le premier lancement)

1. Ouvre `config.py` et remplis :
   - `PRINTER_IP` (dans les réglages Wi-Fi de l'imprimante)
   - `ACCESS_CODE` (écran imprimante → Paramètres → LAN only)
   - `SERIAL` (dans les réglage imprimante -> N° de série Printer)

2. Crée ton compte admin (identifiant + mot de passe) en une seule commande :
   ```bash
   python3 setup_admin.py
   ```
   Le script te demande un identifiant et un mot de passe, puis les écrit
   automatiquement dans `config.py` (aucun copier-coller nécessaire). Il
   régénère aussi une `SECRET_KEY` aléatoire si elle est encore à sa valeur
   par défaut.

3. Lance `python3 app.py`, connecte-toi, puis va dans **⚙ Imprimantes** pour
   ajouter ta/tes imprimante(s) (nom, modèle, IP, access code, numéro de
   série). Tout se fait depuis l'interface web, plus besoin d'éditer de
   fichier pour ça.

## Lancement

```bash
python3 app.py
```

Puis ouvre **http://localhost:5000** (ou `http://<IP-de-ce-PC>:5000` depuis ton
téléphone ou un autre appareil du même réseau local).

## Fonctionnalités

- **Caméra en direct** — encadrée façon moniteur avec indicateur REC pendant l'impression
- **Températures** — buse / plateau / chambre en jauges circulaires
- **Progression** — %, temps restant, couche actuelle/totale, nom du fichier
- **AMS** — couleur et niveau restant (si bobine BambuLab si non 100%) de chaque bobine (si AMS branché)
- **Upload G-code** — glisser-déposer un `.gcode.3mf` déjà tranché avec Bambu Studio,
  envoyé directement sur la carte SD via FTP. Case à cocher pour lancer
  l'impression immédiatement après l'envoi (beta test).
- **Historique** — graphique des dernières valeurs (température, progression),
  toutes les données sont aussi stockées dans `bambu_history.db` (SQLite) pour
  analyse ultérieure.
- **Multi-imprimantes** — ajoute autant de machines que tu veux depuis la page
  ⚙ Imprimantes, bascule entre elles via le sélecteur en haut du dashboard.
  Chaque imprimante a son propre thread de fond, son historique, et son
  réglage de capteur de chambre.
- **Température de chambre intelligente** — la jauge chambre ne s'affiche que
  pour les modèles qui ont un vrai capteur (gamme X, P2 et H2). Sur A1/A1 mini/P1P/P1S
  (pas de capteur natif), elle reste masquée automatiquement. Tu peux forcer
  ce comportement par imprimante si tu as ajouté un capteur DIY.
- **Contrôle en direct** — panneau pour piloter l'imprimante depuis le
  dashboard : éclairage, consignes buse/plateau, vitesse d'impression
  (50/100/124/166%), ventilateurs. **Important, lis la section ci-dessous.**
- **Connexion** — page de login simple, un seul utilisateur admin, mot de passe
  hashé (pas de mot de passe en clair stocké).

## ⚠️ Limite importante : contrôle (buse/plateau/vitesse/ventilateurs)

Depuis **janvier 2025**, Bambu Lab a modifié le firmware de toutes ses
imprimantes pour exiger qu'une commande MQTT locale soit **signée
cryptographiquement** (certificat X.509, RSA-SHA256) pour être exécutée.
Sans cette signature, la commande est envoyée normalement (aucune erreur côté
script) mais **silencieusement ignorée par l'imprimante**.

La librairie utilisée ici (`bambulabs_api`, dernière version au moment de
l'écriture) n'implémente pas cette signature. Résultat concret :

- **Lecture seule (monitoring, caméra, historique, AMS)** → fonctionne
  normalement, ce n'est pas concerné par ce verrou.
- **Upload de G-code par FTP** → fonctionne (le FTP n'est pas concerné).
- **Écriture (lumière, températures, vitesse, ventilateurs)** → la commande
  est envoyée mais son exécution réelle **n'est pas garantie**. C'est
  probablement ce qui se passe avec ton plateau/ventilateurs : la commande
  part, mais l'imprimante l'ignore poliment.

🎉🚨Mais vous pouvez contournez se problème en activant le mode développeur : dans Mode LAN activez le puis un autre bouton mode développeur apparait et vous pouvez le cocher. Cela permet de contourné tout les problème lié a notre logiciel !
## Notes importantes

- **Mode développeur** requis sur l'imprimante (Paramètres → LAN only → Developer
  Mode) pour que la caméra et les commandes fonctionnent sans erreur de
  vérification MQTT.
- Le fichier `bambu_history.db` se crée automatiquement au premier lancement.
- Un seul client à la fois peut streamer la caméra de l'imprimante — ferme
  Bambu Studio / Bambu Handy pendant que le dashboard tourne si la caméra ne
  s'affiche pas.

## Structure du projet

```
bambu_dashboard/
├── app.py                     # Serveur Flask, routes, login
├── auth.py                    # Authentification par session
├── bambu_client.py            # Thread de fond : MQTT + caméra + historique
├── config.py                  # Config à remplir (IP, access code, serial, mdp)
├── setup_admin.py             # Crée le compte admin (identifiant + mot de passe)
├── requirements.txt
├── templates/
│   ├── login.html
│   └── index.html
└── static/
    ├── css/style.css
    └── js/dashboard.js
```

## Prochaines étapes possibles

- Notifications Discord webhook à la fin d'une impression.
- Historique des impressions passées avec miniatures caméra.
- Vraie authentification multi-utilisateurs si besoin de partager avec la
  communauté (Abrège, MakerZ ou Bricolage c'est cool)?
