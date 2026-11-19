"""
Fonctions d'intelligence artificielle de Bambu Control.

Chaque fonction prépare une consigne (prompt) en français, appelle le modèle
choisi dans les réglages et renvoie un résultat simple à afficher.

Garde-fou : l'IA ne déclenche jamais seule une action risquée. Elle peut
proposer des commandes (l'utilisateur clique pour confirmer) ; la seule
action automatique possible est la pause en cas d'échec détecté, et
uniquement si elle est activée dans les réglages.
"""
import json
from datetime import datetime

import ai_client
import settings_store

DEFAULTS = {
    "enabled": False,
    # Service pour le TEXTE (assistant, résumés, erreurs) — ex. Ollama sur le Pi
    "provider": "ollama",
    "base_url": "",
    "api_key": "",
    "text_model": "",
    # Service pour les IMAGES (caméra, plateau, vignettes) — ex. Ollama Cloud
    "vision_same": True,        # True = même service que pour le texte
    "vision_provider": "ollama_cloud",
    "vision_base_url": "",
    "vision_api_key": "",
    "vision_model": "",
    "timeout": 120,
    # Surveillance par caméra
    "watch_enabled": True,
    "watch_interval_min": 5,
    "watch_log": True,          # chaque contrôle (même sans problème) va dans les alertes
    "auto_pause": False,
    "pause_confidence": 80,
    # Automatisations
    "plate_check": True,
    "finish_photo": True,
    "finish_light_off": False,
    "filament_alert": True,
    "daily_summary": False,
    "daily_summary_time": "08:00",
}

# Commandes que l'assistant peut PROPOSER (jamais « stop » ni « start »)
ASSISTANT_COMMANDS = {
    "pause": "mettre en pause (aucun paramètre)",
    "resume": "reprendre (aucun paramètre)",
    "light": 'lumière, paramètres {"on": true|false}',
    "speed": 'vitesse, paramètres {"level": 1|2|3|4} (1=silencieux 50 %, 2=standard, 3=sport, 4=ludicrous)',
    "fan": 'ventilateur, paramètres {"fan": "part"|"aux"|"chamber", "percent": 0-100}',
    "nozzle_temp": 'consigne buse, paramètres {"value": 0-300}',
    "bed_temp": 'consigne plateau, paramètres {"value": 0-110}',
}


# ── Réglages ──────────────────────────────────────────────
def get_config() -> dict:
    config = dict(DEFAULTS)
    config.update(settings_store.load().get("ai") or {})
    return config


def save_config(updates: dict) -> dict:
    config = get_config()
    config.update(updates)
    settings_store.save({"ai": config})
    return config


def public_config(config: dict | None = None) -> dict:
    """Réglages affichables dans le navigateur : la clé API est masquée."""
    config = dict(config or get_config())
    for field in ("api_key", "vision_api_key"):
        key = config.pop(field, "")
        config[f"{field}_hint"] = f"••••{key[-4:]}" if len(key) >= 8 else ("••••" if key else "")
    return config


def connection(config: dict, kind: str = "text") -> tuple[str, str, str]:
    """(fournisseur, adresse, clé) du service à utiliser pour « text » ou « vision »."""
    if kind == "vision" and not config["vision_same"]:
        return config["vision_provider"], config["vision_base_url"], config["vision_api_key"]
    return config["provider"], config["base_url"], config["api_key"]


def client(config: dict | None = None, kind: str = "text") -> ai_client.AiClient:
    config = config or get_config()
    if not config["enabled"]:
        raise ai_client.AiError("L'intelligence artificielle est désactivée (Paramètres → Intelligence artificielle).")
    provider, base_url, api_key = connection(config, kind)
    return ai_client.AiClient(provider, base_url, api_key, timeout=float(config["timeout"]))


def vision_ready(config: dict | None = None) -> bool:
    config = config or get_config()
    return bool(config["enabled"] and config["vision_model"])


def text_model(config: dict) -> str:
    # Sans modèle texte, on peut reprendre le modèle vision… seulement s'il est sur le même service
    return config["text_model"] or (config["vision_model"] if config["vision_same"] else "")


def _ask_json(config: dict, prompt: str, image: bytes | None = None, vision: bool = False) -> dict:
    model = config["vision_model"] if vision else text_model(config)
    images = [ai_client.shrink_image(image)] if image else None
    answer = client(config, "vision" if vision else "text").chat(
        model, [{"role": "user", "content": prompt}], images=images, json_mode=True)
    return ai_client.extract_json(answer)


def _confidence(value) -> int:
    try:
        return max(0, min(100, int(float(value))))
    except (TypeError, ValueError):
        return 0


# ── Caméra ────────────────────────────────────────────────
FAILURE_PROMPT = """Tu surveilles une imprimante 3D Bambu Lab pendant une impression.
Regarde l'image de la caméra. Cherche UNIQUEMENT des signes clairs d'échec :
amas de fils emmêlés (« spaghetti »), pièce décollée ou renversée sur le plateau,
plastique enroulé autour de la buse, impression dans le vide.
Une impression normale, même peu visible ou sombre, n'est PAS un échec.
Réponds uniquement en JSON :
{"failure": true ou false, "confidence": 0 à 100, "issue": "spaghetti|decollement|buse|vide|aucun", "description": "une phrase en français"}"""

PLATE_PROMPT = """Image de la caméra d'une imprimante 3D Bambu Lab, juste avant de lancer une impression.
Le plateau doit être vide : aucune pièce imprimée, aucun objet, aucun reste de filament.
Réponds uniquement en JSON :
{"clear": true ou false, "confidence": 0 à 100, "description": "une phrase en français"}"""

FINISH_PROMPT = """Image d'une impression 3D qui vient de se terminer (fichier : {file}).
Donne un avis bref sur le résultat visible : réussite apparente, défauts visibles
(fils, décollement, surface abîmée), ou image peu exploitable.
Réponds uniquement en JSON :
{{"success": true ou false, "comment": "une ou deux phrases en français"}}"""


def analyze_failure(image: bytes, config: dict | None = None) -> dict:
    config = config or get_config()
    data = _ask_json(config, FAILURE_PROMPT, image, vision=True)
    return {"failure": bool(data.get("failure")), "confidence": _confidence(data.get("confidence")),
            "issue": str(data.get("issue") or "aucun"), "description": str(data.get("description") or "")}


def check_plate(image: bytes, config: dict | None = None) -> dict:
    config = config or get_config()
    data = _ask_json(config, PLATE_PROMPT, image, vision=True)
    return {"clear": bool(data.get("clear")), "confidence": _confidence(data.get("confidence")),
            "description": str(data.get("description") or "")}


def comment_finished(image: bytes, file_name: str, config: dict | None = None) -> dict:
    config = config or get_config()
    data = _ask_json(config, FINISH_PROMPT.format(file=file_name or "inconnu"), image, vision=True)
    return {"success": bool(data.get("success")), "comment": str(data.get("comment") or "")}


def describe_thumbnail(image: bytes, info: dict, config: dict | None = None) -> str:
    config = config or get_config()
    prompt = ("Voici la vignette d'un fichier d'impression 3D tranché. Informations : "
              f"{json.dumps(info, ensure_ascii=False)}. Décris en 2 phrases en français ce que représente "
              "la pièce et un point d'attention pour l'imprimer (supports, adhérence, durée…). "
              'Réponds uniquement en JSON : {"description": "…"}')
    return str(_ask_json(config, prompt, image, vision=True).get("description") or "")


# ── Texte ─────────────────────────────────────────────────
def explain_error(code: str, printer_model: str = "", config: dict | None = None) -> str:
    config = config or get_config()
    prompt = (f"Une imprimante 3D Bambu Lab {printer_model} affiche le code d'erreur {code} "
              "(format HMS / print_error de Bambu Lab). Explique en français simple, en 3 à 5 phrases : "
              "ce que signifie probablement ce code, les causes les plus courantes et quoi vérifier. "
              "Si tu n'es pas sûr de la signification exacte, dis-le clairement et conseille de vérifier "
              "sur le wiki Bambu Lab. Réponds uniquement en JSON : {\"explanation\": \"…\"}")
    return str(_ask_json(config, prompt).get("explanation") or "")


def summarize_history(stats: dict, jobs: list, errors: list, days: int, config: dict | None = None) -> str:
    config = config or get_config()
    compact_jobs = [{k: j.get(k) for k in ("file_name", "started_at", "duration_s", "status", "last_percent",
                                           "error_code", "filament", "ai_comment")} for j in jobs[:40]]
    prompt = (f"Tu analyses l'historique d'une imprimante 3D Bambu Lab sur les {days} derniers jours.\n"
              f"Statistiques : {json.dumps(stats, ensure_ascii=False)}\n"
              f"Impressions : {json.dumps(compact_jobs, ensure_ascii=False)}\n"
              f"Erreurs : {json.dumps(errors[:20], ensure_ascii=False, default=str)}\n"
              "Écris en français un résumé clair (5 à 8 phrases) : activité, taux de réussite, temps machine, "
              "échecs récurrents et tendances (matériaux, moments, fichiers), puis 1 à 3 conseils concrets. "
              "N'invente rien qui ne soit pas dans les données. "
              'Réponds uniquement en JSON : {"summary": "…"}')
    return str(_ask_json(config, prompt).get("summary") or "")


def assistant_reply(messages: list[dict], context: dict, config: dict | None = None) -> dict:
    """
    Conversation avec l'assistant. Renvoie {"reply": str, "actions": [{"command", "params", "label"}]}.
    Les actions ne sont que des PROPOSITIONS : l'interface demande à l'utilisateur de cliquer.
    """
    config = config or get_config()
    allowed = "\n".join(f"- {name} : {desc}" for name, desc in ASSISTANT_COMMANDS.items())
    system = (
        "Tu es l'assistant de Bambu Control, une console qui surveille une imprimante 3D Bambu Lab. "
        "Réponds toujours en français, de façon courte et concrète. "
        f"Nous sommes le {datetime.now().strftime('%d/%m/%Y %H:%M')}.\n"
        f"État actuel (données réelles) : {json.dumps(context, ensure_ascii=False, default=str, separators=(',', ':'))}\n"
        "RÈGLE IMPORTANTE : tu ne peux PAS agir toi-même sur l'imprimante. N'écris JAMAIS qu'une action est "
        "faite (« la lumière est allumée », « c'est en pause »…). Quand l'utilisateur demande une action, "
        "mets-la dans « actions » et écris « Je te propose de… : clique sur le bouton pour confirmer. » "
        "Commandes possibles :\n"
        f"{allowed}\n"
        "Tu ne peux ni arrêter ni lancer une impression : explique où le faire dans l'interface. "
        "N'invente pas de valeurs absentes de l'état.\n"
        "Pour un temps restant, utilise uniquement « reste_min » de l'état ; si « etat » n'est pas "
        "RUNNING ou PAUSE, dis qu'aucune impression n'est en cours.\n"
        "Exemple — question : « Allume la lumière. » → "
        '{"reply": "Je te propose d\'allumer la lumière : clique sur le bouton pour confirmer.", '
        '"actions": [{"command": "light", "params": {"on": true}, "label": "Allumer la lumière"}]}\n'
        'Réponds uniquement en JSON : {"reply": "texte", "actions": [...]}  (actions = [] si aucune).'
    )
    history = [{"role": m["role"], "content": str(m["content"])[:2000]}
               for m in messages[-12:] if m.get("role") in ("user", "assistant")]
    answer = client(config).chat(text_model(config), [{"role": "system", "content": system}] + history,
                                 json_mode=True)
    try:
        data = ai_client.extract_json(answer)
    except ai_client.AiError:
        return {"reply": answer, "actions": []}
    actions = []
    for action in data.get("actions") or []:
        if isinstance(action, dict) and action.get("command") in ASSISTANT_COMMANDS:
            actions.append({"command": action["command"], "params": action.get("params") or {},
                            "label": str(action.get("label") or action["command"])})
    return {"reply": str(data.get("reply") or ""), "actions": actions}
