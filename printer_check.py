"""
Test de connexion à une imprimante, étape par étape, pour dire précisément
quel réglage ne va pas (IP, access code, numéro de série, carte SD, caméra).

Utilisé avant d'ajouter une imprimante et depuis la page Paramètres.
Dure au plus une quinzaine de secondes.
"""
import socket
import time

import bambulabs_api as bl

import models
import sd_card

MQTT_PORT = 8883
CAMERA_PORTS = {"tcp": 6000, "rtsp": 322}
PORT_TIMEOUT = 3
MQTT_CONNECT_TIMEOUT = 6
FIRST_MESSAGE_TIMEOUT = 8


def port_open(ip: str, port: int, timeout: float = PORT_TIMEOUT) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_until(predicate, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def _check(name: str, ok: bool, detail: str, critical: bool = True) -> dict:
    return {"name": name, "ok": ok, "detail": detail, "critical": critical}


def _check_mqtt(ip: str, access_code: str, serial: str) -> list[dict]:
    """Access code (connexion acceptée) puis numéro de série (données reçues)."""
    received = {"count": 0}
    printer = bl.Printer(ip, access_code, serial)
    printer.mqtt_client.on_message_handler = lambda *_a: received.__setitem__("count", received["count"] + 1)
    checks = []
    try:
        printer.mqtt_start()
        if not _wait_until(printer.mqtt_client_connected, MQTT_CONNECT_TIMEOUT):
            return [_check("Access code", False,
                           "Connexion refusée : access code incorrect ou « Mode LAN » désactivé sur l'imprimante.")]
        checks.append(_check("Access code", True, "Connexion MQTT acceptée."))
        if _wait_until(lambda: received["count"] > 0, FIRST_MESSAGE_TIMEOUT):
            checks.append(_check("Numéro de série", True, "L'imprimante envoie bien son état."))
        else:
            checks.append(_check("Numéro de série", False,
                                 "Connecté mais aucune donnée reçue : le numéro de série est probablement faux."))
        return checks
    finally:
        try:
            printer.mqtt_client._client.disconnect()
            printer.mqtt_stop()
        except Exception:
            pass


def check_printer(ip: str, access_code: str, serial: str, model: str = "OTHER") -> dict:
    checks = []
    if not port_open(ip, MQTT_PORT):
        checks.append(_check("Réseau", False,
                             f"Rien ne répond sur {ip} : imprimante éteinte, mauvaise IP, ou appareil "
                             "sur un autre réseau Wi-Fi."))
        return {"ok": False, "checks": checks}
    checks.append(_check("Réseau", True, f"L'imprimante répond sur {ip}."))

    checks += _check_mqtt(ip, access_code, serial)
    if not all(c["ok"] for c in checks):
        return {"ok": False, "checks": checks}

    try:
        count = len(sd_card.SdCard(ip, access_code, timeout=8).list_files())
        checks.append(_check("Carte SD", True, f"{count} fichier(s) imprimable(s) trouvé(s).", critical=False))
    except sd_card.SdCardError as e:
        checks.append(_check("Carte SD", False, f"{e} — l'envoi de fichiers ne marchera pas.", critical=False))

    protocol = models.camera_protocol(model)
    port = CAMERA_PORTS[protocol]
    if port_open(ip, port):
        checks.append(_check("Caméra", True, f"Flux caméra joignable (port {port}).", critical=False))
    else:
        hint = "active « LAN Mode Liveview » sur l'imprimante" if protocol == "rtsp" else "caméra désactivée ?"
        checks.append(_check("Caméra", False, f"Port caméra {port} fermé ({hint}).", critical=False))

    return {"ok": all(c["ok"] for c in checks if c["critical"]), "checks": checks}
