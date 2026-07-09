"""
Registre des modèles d'imprimantes Bambu Lab.

has_chamber_sensor : indique si le modèle a un vrai capteur de température
de chambre qui remonte une valeur exploitable via MQTT.

- Open-frame (pas d'enceinte) : A1, A1 mini, A2L → pas de capteur.
- Enceinte passive (chauffée par le plateau, pas de chauffage actif) :
  P1P, P1S → pas de capteur d'origine (nécessite un capteur DIY).
  P2S → enceinte passive mais AVEC capteur de température de chambre
  d'origine (contrairement au P1S).
- Enceinte à chauffage actif : X1, X1 Carbon, X1E, X2D, H2D, H2D Pro,
  H2S, H2C → capteur de chambre présent et piloté activement.

Ces valeurs par défaut peuvent être surchargées manuellement par imprimante
depuis la page de configuration (utile si un capteur DIY a été ajouté sur
un modèle qui n'en a pas d'origine, comme un P1P/P1S/A1 modifié).

Sources : pages produit et spécifications techniques Bambu Lab (juillet 2026).
"""

PRINTER_MODELS = {
    # ── Série A (open-frame) ──────────────────────────────
    "A1": {"label": "Bambu Lab A1", "has_chamber_sensor": False},
    "A1_MINI": {"label": "Bambu Lab A1 mini", "has_chamber_sensor": False},
    "A2L": {"label": "Bambu Lab A2L", "has_chamber_sensor": False},

    # ── Série P (enceinte) ────────────────────────────────
    "P1P": {"label": "Bambu Lab P1P", "has_chamber_sensor": False},
    "P1S": {"label": "Bambu Lab P1S", "has_chamber_sensor": False},
    "P2S": {"label": "Bambu Lab P2S", "has_chamber_sensor": True},

    # ── Série X (enceinte + chauffage actif) ──────────────
    "X1": {"label": "Bambu Lab X1", "has_chamber_sensor": True},
    "X1_CARBON": {"label": "Bambu Lab X1 Carbon", "has_chamber_sensor": True},
    "X1E": {"label": "Bambu Lab X1E", "has_chamber_sensor": True},
    "X2D": {"label": "Bambu Lab X2D", "has_chamber_sensor": True},

    # ── Série H (enceinte + chauffage actif, multi-outils) ─
    "H2D": {"label": "Bambu Lab H2D", "has_chamber_sensor": True},
    "H2D_PRO": {"label": "Bambu Lab H2D Pro", "has_chamber_sensor": True},
    "H2S": {"label": "Bambu Lab H2S", "has_chamber_sensor": True},
    "H2C": {"label": "Bambu Lab H2C", "has_chamber_sensor": True},

    "OTHER": {"label": "Autre / non listé", "has_chamber_sensor": False},
}


def model_choices():
    """Liste (clé, label) pour peupler un <select> dans le formulaire."""
    return [(key, v["label"]) for key, v in PRINTER_MODELS.items()]


def default_has_chamber(model_key: str) -> bool:
    return PRINTER_MODELS.get(model_key, PRINTER_MODELS["OTHER"])["has_chamber_sensor"]
