"""
Client IA minimal, sans dépendance supplémentaire (urllib).

Trois fournisseurs, réglés dans Paramètres → Intelligence artificielle :
- "ollama"       : Ollama sur cette machine ou un PC du réseau (http://…:11434)
- "ollama_cloud" : Ollama Cloud (https://ollama.com) avec une clé API
- "openai"       : toute API compatible OpenAI (/v1/chat/completions)

La clé API reste côté serveur (data/settings.json) et n'est jamais envoyée
au navigateur.
"""
import base64
import json
import logging
import re
import urllib.error
import urllib.request

log = logging.getLogger("ai")

PROVIDERS = {
    "ollama": {"label": "Ollama (local / réseau)", "url": "http://localhost:11434"},
    "ollama_cloud": {"label": "Ollama Cloud", "url": "https://ollama.com"},
    "openai": {"label": "API compatible OpenAI", "url": "https://api.openai.com/v1"},
}
DEFAULT_TIMEOUT = 90


class AiError(Exception):
    """Erreur avec un message compréhensible pour l'utilisateur."""


class AiClient:
    def __init__(self, provider: str, base_url: str, api_key: str = "", timeout: float = DEFAULT_TIMEOUT):
        if provider not in PROVIDERS:
            raise AiError(f"Fournisseur inconnu : {provider}")
        self.provider = provider
        self.base_url = (base_url or PROVIDERS[provider]["url"]).rstrip("/")
        self.api_key = api_key or ""
        self.timeout = timeout

    @property
    def is_ollama(self) -> bool:
        return self.provider in ("ollama", "ollama_cloud")

    # ── HTTP ──
    def _request(self, path: str, payload: dict | None = None) -> dict:
        headers = {"Content-Type": "application/json", "User-Agent": "BambuControl"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers,
                                         method="POST" if payload is not None else "GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code in (401, 403):
                raise AiError("Clé API refusée : vérifie la clé dans les réglages.")
            if e.code == 404:
                raise AiError(f"Introuvable (404) : vérifie l'adresse et le nom du modèle. {detail}")
            raise AiError(f"Erreur du service IA ({e.code}) : {detail}")
        except urllib.error.URLError as e:
            raise AiError(f"Service IA injoignable ({self.base_url}) : {e.reason}")
        except TimeoutError:
            raise AiError(f"Le service IA n'a pas répondu en {int(self.timeout)} s.")
        except json.JSONDecodeError:
            raise AiError("Réponse illisible du service IA.")

    # ── Modèles disponibles (sert aussi de test de connexion) ──
    def list_models(self) -> list[str]:
        if self.is_ollama:
            return sorted(m["name"] for m in self._request("/api/tags").get("models", []))
        return sorted(m["id"] for m in self._request("/models").get("data", []))

    # ── Conversation ──
    def chat(self, model: str, messages: list[dict], images: list[bytes] | None = None,
             json_mode: bool = False) -> str:
        """
        messages : [{"role": "system"|"user"|"assistant", "content": "..."}]
        images   : images JPEG/PNG jointes au dernier message (modèles « vision »)
        """
        if not model:
            raise AiError("Aucun modèle choisi dans les réglages IA.")
        if self.is_ollama:
            return self._chat_ollama(model, messages, images, json_mode)
        return self._chat_openai(model, messages, images, json_mode)

    def _chat_ollama(self, model, messages, images, json_mode) -> str:
        messages = [dict(m) for m in messages]
        if images:
            messages[-1]["images"] = [base64.b64encode(img).decode() for img in images]
        payload = {"model": model, "messages": messages, "stream": False,
                   "think": False,  # modèles « qui réfléchissent » : réponse directe, bien plus rapide
                   "keep_alive": "30m",  # garde le modèle en mémoire : pas de rechargement entre deux questions
                   "options": {"temperature": 0.2}}
        if json_mode:
            payload["format"] = "json"
        try:
            response = self._request("/api/chat", payload)
        except AiError as e:
            if "think" not in str(e):
                raise
            payload.pop("think")         # modèle qui ne connaît pas ce réglage
            response = self._request("/api/chat", payload)
        return (response.get("message") or {}).get("content", "").strip()

    def _chat_openai(self, model, messages, images, json_mode) -> str:
        messages = [dict(m) for m in messages]
        if images:
            parts = [{"type": "text", "text": messages[-1]["content"]}]
            parts += [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," +
                                                          base64.b64encode(img).decode()}} for img in images]
            messages[-1]["content"] = parts
        payload = {"model": model, "messages": messages, "temperature": 0.2}
        response = self._request("/chat/completions", payload)
        choices = response.get("choices") or [{}]
        return ((choices[0].get("message") or {}).get("content") or "").strip()


def shrink_image(image: bytes, max_width: int = 640) -> bytes:
    """Réduit une image avant envoi : bien plus rapide, et suffisant pour juger une impression."""
    try:
        from io import BytesIO
        from PIL import Image   # déjà installé avec bambulabs_api
        picture = Image.open(BytesIO(image)).convert("RGB")
        if picture.width > max_width:
            picture = picture.resize((max_width, round(picture.height * max_width / picture.width)))
        out = BytesIO()
        picture.save(out, "JPEG", quality=80)
        return out.getvalue()
    except Exception:
        return image


def extract_json(text: str) -> dict:
    """Récupère l'objet JSON d'une réponse (même entouré de texte ou de ```json)."""
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        raise AiError("L'IA n'a pas renvoyé de réponse structurée.")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        raise AiError("Réponse structurée de l'IA illisible.")
