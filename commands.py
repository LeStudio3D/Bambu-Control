"""
Commandes envoyées aux imprimantes, avec confirmation.

Principe : on envoie la commande en MQTT, puis on surveille l'état remonté
par l'imprimante jusqu'à voir l'effet attendu (ex. état "PAUSE" après une
pause). On ne dit JAMAIS « succès » sans cette preuve.

Résultats possibles :
- confirmed   : l'imprimante a appliqué la commande
- unconfirmed : envoyée, mais aucun effet visible dans le délai
                (cas typique : mode développeur LAN désactivé, firmware 2025)
- rejected    : refusée (imprimante hors ligne, état incompatible, refus firmware)
- error       : la commande n'a pas pu partir
"""
from dataclasses import dataclass, field
from typing import Callable

CONFIRMED = "confirmed"
UNCONFIRMED = "unconfirmed"
REJECTED = "rejected"
ERROR = "error"

HTTP_CODES = {CONFIRMED: 200, UNCONFIRMED: 202, REJECTED: 409, ERROR: 502}

DEV_MODE_HINT = ("Si cela se répète : active le « mode développeur » dans Paramètres → "
                 "Réseau → Mode LAN de l'imprimante. Sans lui, le firmware récent ignore "
                 "les commandes venant d'applications tierces.")

ACTIVE_STATES = ("RUNNING", "PAUSE", "PREPARE")
SPEED_PERCENT = {1: 50, 2: 100, 3: 124, 4: 166}
SPEED_LABELS = {1: "Silencieux", 2: "Standard", 3: "Sport", 4: "Ludicrous"}


@dataclass
class CommandResult:
    status: str
    message: str

    @property
    def http_code(self) -> int:
        return HTTP_CODES[self.status]

    def to_json(self) -> dict:
        return {"status": self.status, "message": self.message,
                "success": self.status == CONFIRMED}


# ── Envoi MQTT bas niveau ─────────────────────────────────
def publish(printer, payload: dict) -> bool:
    """Publie un message brut sur le topic de commande de l'imprimante.

    bambulabs_api ne rend pas cette méthode publique (nom « privé ») : on
    l'appelle quand même, c'est le seul moyen d'envoyer des commandes que la
    librairie ne propose pas (ou propose mal).
    """
    return printer.mqtt_client._PrinterMQTTClient__publish_command(payload)


def send_gcode(printer, line: str) -> bool:
    return publish(printer, {"print": {"command": "gcode_line", "param": f"{line}\n",
                                       "sequence_id": "0"}})


def request_full_status(printer):
    """Demande à l'imprimante de renvoyer tout son état (confirmation plus rapide)."""
    try:
        printer.mqtt_client.pushall()
    except Exception:
        pass


# ── Définition des commandes ──────────────────────────────
@dataclass
class CommandSpec:
    label: str
    send: Callable                    # (printer, params) -> bool
    is_done: Callable                 # (status, params) -> bool
    timeout: float = 10.0
    reply_commands: tuple = ()        # noms de commande dans la réponse MQTT de l'imprimante
    precondition: Callable | None = None   # (status, params) -> message d'erreur ou None
    parse: Callable = field(default=lambda body: {})  # (body JSON) -> params, ValueError si invalide


def _state(status):
    return status.get("gcode_state")


def _close(a, b, tolerance):
    return a is not None and b is not None and abs(float(a) - float(b)) <= tolerance


def _require_active(status, _params):
    if _state(status) not in ACTIVE_STATES:
        return "Aucune impression en cours."
    return None


def _parse_int(body, key, low, high, label):
    try:
        value = int(body.get(key))
    except (TypeError, ValueError):
        raise ValueError(f"{label} invalide")
    if not low <= value <= high:
        raise ValueError(f"{label} hors limites ({low}-{high})")
    return value


def _parse_light(body):
    return {"on": bool(body.get("on", True))}


def _send_light(printer, params):
    mode = "on" if params["on"] else "off"
    return publish(printer, {"system": {
        "sequence_id": "0", "command": "ledctrl", "led_node": "chamber_light",
        "led_mode": mode, "led_on_time": 500, "led_off_time": 500,
        "loop_times": 0, "interval_time": 0,
    }})


def _parse_nozzle(body):
    return {"value": _parse_int(body, "value", 0, 320, "Température buse")}


def _parse_bed(body):
    return {"value": _parse_int(body, "value", 0, 120, "Température plateau")}


def _parse_speed(body):
    return {"level": _parse_int(body, "level", 1, 4, "Niveau de vitesse")}


FAN_GCODE_INDEX = {"part": 1, "aux": 2, "chamber": 3}
FAN_LABELS = {"part": "ventilateur pièce", "aux": "ventilateur auxiliaire",
              "chamber": "ventilateur de chambre"}


def _parse_fan(body):
    fan = body.get("fan")
    if fan not in FAN_GCODE_INDEX:
        raise ValueError("Ventilateur inconnu")
    return {"fan": fan, "percent": _parse_int(body, "percent", 0, 100, "Pourcentage")}


def _send_fan(printer, params):
    pwm = round(params["percent"] / 100 * 255)
    return send_gcode(printer, f"M106 P{FAN_GCODE_INDEX[params['fan']]} S{pwm}")


# ── Lancement d'une impression ──
# Valeurs possibles dans ams_mapping (une par filament du projet, dans l'ordre) :
#   0…15 : AMS classique / AMS 2 Pro / AMS lite (n° d'AMS × 4 + emplacement)
#   128…135 : AMS HT ; -1 : filament non utilisé par ce plateau
# Modèles qui attendent le chemin « file:///sdcard/… » (les autres : « ftp:///… »)
SDCARD_URL_MODELS = ("A1", "A1_MINI", "A2L", "P1P", "P1S", "OTHER")
START_OPTIONS = ("timelapse", "bed_leveling", "flow_cali", "vibration_cali")
START_DEFAULTS = {"timelapse": False, "bed_leveling": True, "flow_cali": True, "vibration_cali": True}


def _valid_tray(value) -> bool:
    return value == -1 or 0 <= value <= 15 or 128 <= value <= 135


def parse_start_options(body: dict) -> dict:
    """Options de lancement communes (lancement direct et file d'impression)."""
    use_ams = bool(body.get("use_ams", True))
    mapping = body.get("ams_mapping")
    if mapping in (None, ""):
        mapping = []
    if not isinstance(mapping, list) or len(mapping) > 32:
        raise ValueError("Correspondance filaments / bobines invalide")
    try:
        mapping = [int(v) for v in mapping]
    except (TypeError, ValueError):
        raise ValueError("Correspondance filaments / bobines invalide")
    if not all(_valid_tray(v) for v in mapping):
        raise ValueError("Emplacement AMS inconnu dans la correspondance des filaments")
    if use_ams and mapping and all(v == -1 for v in mapping):
        raise ValueError("Choisis au moins une bobine AMS, ou la bobine externe")
    options = {key: bool(body.get(key, default)) for key, default in START_DEFAULTS.items()}
    if options["timelapse"] and body.get("by_object"):
        raise ValueError("Timelapse impossible : ce plateau est imprimé « par objet » (il faut « par couche »)")
    return {"use_ams": use_ams, "ams_mapping": mapping if use_ams else [], **options}


def _parse_start(body):
    filename = str(body.get("filename") or "").strip()
    if not filename or "/" in filename or "\\" in filename:
        raise ValueError("Nom de fichier invalide")
    plate = _parse_int({"p": body.get("plate", 1)}, "p", 1, 99, "Numéro de plateau")
    return {"filename": filename, "plate": plate, **parse_start_options(body)}


def ams_mapping2(mapping: list) -> list:
    """Format récent (firmwares 2025+) : [{ams_id, slot_id}], envoyé en plus de l'ancien."""
    result = []
    for value in mapping:
        if value == -1:
            result.append({"ams_id": 255, "slot_id": 255})
        elif value >= 128:
            result.append({"ams_id": value, "slot_id": 0})
        else:
            result.append({"ams_id": value // 4, "slot_id": value % 4})
    return result


def start_payload(params: dict) -> dict:
    """Message « project_file » complet (même format que Bambu Studio)."""
    name = params["filename"]
    url = (f"file:///sdcard/{name}" if params.get("model", "OTHER") in SDCARD_URL_MODELS
           else f"ftp:///{name}")
    use_ams = bool(params.get("use_ams", True))
    mapping = list(params.get("ams_mapping") or []) if use_ams else []
    if use_ams and not mapping:
        mapping = [0]   # ancien comportement : filament 1 → premier emplacement
    title = name[:-len(".gcode.3mf")] if name.lower().endswith(".gcode.3mf") else name.rsplit(".", 1)[0]
    return {"print": {
        "sequence_id": "0",
        "command": "project_file",
        "param": f"Metadata/plate_{int(params['plate'])}.gcode",
        "url": url,
        "file": name,
        "md5": "",
        "subtask_name": title,
        "profile_id": "0", "project_id": "0", "subtask_id": "0", "task_id": "0",
        "bed_type": "auto",
        "timelapse": bool(params.get("timelapse", False)),
        "bed_leveling": bool(params.get("bed_leveling", True)),
        "flow_cali": bool(params.get("flow_cali", True)),
        "vibration_cali": bool(params.get("vibration_cali", True)),
        "layer_inspect": False,
        "use_ams": use_ams,
        "ams_mapping": mapping if use_ams else "",
        "ams_mapping2": ams_mapping2(mapping) if use_ams else "",
    }}


def _send_start(printer, params):
    name = params["filename"]
    if name.lower().endswith(".3mf"):
        return publish(printer, start_payload(params))
    # .gcode brut : commande firmware directe (pas de choix AMS ni d'options)
    return publish(printer, {"print": {"command": "gcode_file", "param": f"/sdcard/{name}",
                                       "sequence_id": "0"}})


# ── Séchage des bobines (AMS 2 Pro : 45-65 °C, AMS HT : 45-85 °C) ──
DRY_FILAMENTS = ("PLA", "PETG", "TPU", "ABS", "ASA", "PC", "PA", "PET", "PVA", "PPA", "PPS", "HIPS", "PLA-CF",
                 "PETG-CF", "PA-CF", "PAHT-CF", "PET-CF", "SUPPORT")
DRY_MIN_TEMP = 45


def _find_unit(status, ams_id):
    return next((u for u in status.get("ams_units") or [] if u.get("id") == ams_id), None)


def _parse_dry(body):
    filament = str(body.get("filament") or "").upper()
    if filament not in DRY_FILAMENTS:
        raise ValueError("Type de filament inconnu")
    return {
        "ams_id": _parse_int(body, "ams_id", 0, 255, "AMS"),
        "filament": filament,
        "temp": _parse_int(body, "temp", DRY_MIN_TEMP, 85, "Température de séchage"),
        "hours": _parse_int(body, "hours", 1, 24, "Durée de séchage"),
        "rotate": bool(body.get("rotate", False)),
    }


def _dry_precondition(status, params):
    unit = _find_unit(status, params["ams_id"])
    if not unit:
        return "Cet AMS n'est pas détecté par l'imprimante."
    if not unit.get("can_dry"):
        return f"{unit['name']} ({unit['model']}) ne sait pas sécher les bobines (AMS 2 Pro ou AMS HT seulement)."
    if not unit.get("remote_dry"):
        return ("Le firmware de cette imprimante ne permet pas de lancer le séchage à distance "
                "(même Bambu Studio ne le peut pas) : lance-le depuis l'écran de l'imprimante.")
    if unit.get("dry_blocked"):
        return "Séchage impossible pour l'instant : " + ", ".join(unit["dry_blocked"]) + "."
    if params["temp"] > unit["dry_max_temp"]:
        return f"{unit['model']} : {DRY_MIN_TEMP} à {unit['dry_max_temp']} °C maximum."
    return None


def _send_dry(printer, params):
    return publish(printer, {"print": {
        "sequence_id": "0", "command": "ams_filament_drying",
        "ams_id": params["ams_id"], "mode": 1,
        "filament": params["filament"], "temp": params["temp"], "cooling_temp": DRY_MIN_TEMP,
        "duration": params["hours"], "humidity": 0,
        "rotate_tray": params["rotate"], "close_power_conflict": False,
    }})


def _parse_dry_stop(body):
    return {"ams_id": _parse_int(body, "ams_id", 0, 255, "AMS")}


def _dry_stop_precondition(status, params):
    unit = _find_unit(status, params["ams_id"])
    if not unit:
        return "Cet AMS n'est pas détecté par l'imprimante."
    return None


def _send_dry_stop(printer, params):
    return publish(printer, {"print": {
        "sequence_id": "0", "command": "ams_filament_drying",
        "ams_id": params["ams_id"], "mode": 0,
        "filament": "", "temp": 0, "cooling_temp": DRY_MIN_TEMP,
        "duration": 0, "humidity": 0, "rotate_tray": False, "close_power_conflict": False,
    }})


def _is_drying(status, ams_id) -> bool:
    unit = _find_unit(status, ams_id)
    return bool(unit and unit.get("drying"))


def _start_precondition(status, _params):
    if _state(status) in ACTIVE_STATES:
        return "Une impression est déjà en cours."
    return None


COMMANDS: dict[str, CommandSpec] = {
    "pause": CommandSpec(
        label="Pause",
        send=lambda p, _: publish(p, {"print": {"command": "pause", "sequence_id": "0"}}),
        is_done=lambda s, _: _state(s) == "PAUSE",
        precondition=lambda s, _: None if _state(s) == "RUNNING" else "Aucune impression en cours à mettre en pause.",
        reply_commands=("pause",), timeout=20,
    ),
    "resume": CommandSpec(
        label="Reprise",
        send=lambda p, _: publish(p, {"print": {"command": "resume", "sequence_id": "0"}}),
        is_done=lambda s, _: _state(s) == "RUNNING",
        precondition=lambda s, _: None if _state(s) == "PAUSE" else "L'impression n'est pas en pause.",
        reply_commands=("resume",), timeout=20,
    ),
    "stop": CommandSpec(
        label="Arrêt",
        send=lambda p, _: publish(p, {"print": {"command": "stop", "sequence_id": "0"}}),
        is_done=lambda s, _: _state(s) in ("FAILED", "IDLE", "FINISH"),
        precondition=_require_active,
        reply_commands=("stop",), timeout=20,
    ),
    "light": CommandSpec(
        label="Lumière",
        parse=_parse_light, send=_send_light,
        is_done=lambda s, p: s.get("light_state") == ("on" if p["on"] else "off"),
        reply_commands=("ledctrl",),
    ),
    "nozzle_temp": CommandSpec(
        label="Consigne buse",
        parse=_parse_nozzle,
        send=lambda p, prm: send_gcode(p, f"M104 S{prm['value']}"),
        is_done=lambda s, prm: _close(s.get("nozzle_target"), prm["value"], 1),
        reply_commands=("gcode_line",),
    ),
    "bed_temp": CommandSpec(
        label="Consigne plateau",
        parse=_parse_bed,
        send=lambda p, prm: send_gcode(p, f"M140 S{prm['value']}"),
        is_done=lambda s, prm: _close(s.get("bed_target"), prm["value"], 1),
        reply_commands=("gcode_line",),
    ),
    "speed": CommandSpec(
        label="Vitesse",
        parse=_parse_speed,
        send=lambda p, prm: p.mqtt_client.set_print_speed_lvl(prm["level"]),
        is_done=lambda s, prm: s.get("print_speed") == SPEED_PERCENT[prm["level"]],
        precondition=_require_active,
        reply_commands=("print_speed",),
    ),
    "fan": CommandSpec(
        label="Ventilateur",
        parse=_parse_fan, send=_send_fan,
        # l'A1 remonte la vitesse par paliers de ~7 % : tolérance de 10 %
        is_done=lambda s, prm: _close(s.get(f"fan_{prm['fan']}_pct"), prm["percent"], 10),
        reply_commands=("gcode_line",),
    ),
    "start": CommandSpec(
        label="Lancement",
        parse=_parse_start, send=_send_start,
        is_done=lambda s, _: _state(s) in ACTIVE_STATES,
        precondition=_start_precondition,
        reply_commands=("project_file", "gcode_file"), timeout=30,
    ),
    "dry": CommandSpec(
        label="Séchage",
        parse=_parse_dry, send=_send_dry,
        is_done=lambda s, prm: _is_drying(s, prm["ams_id"]),
        precondition=_dry_precondition,
        reply_commands=("ams_filament_drying",), timeout=30,
    ),
    "dry_stop": CommandSpec(
        label="Arrêt du séchage",
        parse=_parse_dry_stop, send=_send_dry_stop,
        is_done=lambda s, prm: not _is_drying(s, prm["ams_id"]),
        precondition=_dry_stop_precondition,
        reply_commands=("ams_filament_drying",), timeout=30,
    ),
}


def describe(name: str, params: dict) -> str:
    """Phrase courte pour les messages : « Consigne buse 210 °C »."""
    spec = COMMANDS[name]
    if name == "light":
        return f"Lumière {'allumée' if params['on'] else 'éteinte'}"
    if name in ("nozzle_temp", "bed_temp"):
        return f"{spec.label} {params['value']} °C"
    if name == "speed":
        return f"Vitesse {SPEED_LABELS[params['level']]} ({SPEED_PERCENT[params['level']]} %)"
    if name == "fan":
        return f"{FAN_LABELS[params['fan']].capitalize()} {params['percent']} %"
    if name == "start":
        return f"Lancement de {params['filename']}"
    if name == "dry":
        return f"Séchage {params['filament']} {params['temp']} °C pendant {params['hours']} h"
    return spec.label
