"""
Stock de filament : les bobines, leur poids restant et leur emplacement.

Une bobine peut être rangée (« en stock ») ou posée sur une imprimante :
emplacement AMS (A1…D4, HT1…) ou support externe (EXT). À la fin de chaque
impression, le filament consommé (lu dans le fichier .3mf) est retiré des
bobines utilisées ; une alerte prévient quand il en reste peu.

Les bobines Bambu Lab à puce donnent aussi leur niveau (%) via l'AMS : le
stock reste utile pour toutes les autres marques, dont l'imprimante ne
connaît pas le niveau.
"""
# Annotations lues plus tard : la méthode list() ci-dessous masque le type
# list[...] sur Python 3.13 et plus ancien (le Pi) et ferait planter l'import.
from __future__ import annotations

from datetime import datetime

EXTERNAL_SLOT = "EXT"
MATERIALS = ("PLA", "PLA-CF", "PETG", "PETG-CF", "TPU", "ABS", "ASA", "PC", "PA", "PA-CF", "PAHT-CF",
             "PET-CF", "PVA", "HIPS", "SUPPORT", "AUTRE")
DEFAULT_ALERT_G = 100

FIELDS = ("name", "material", "color", "weight_g", "remaining_g", "price_kg", "printer_id", "slot", "alert_g")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def slot_label_for_tray(tray: int) -> str | None:
    """Valeur d'ams_mapping → libellé d'emplacement (5 → B2, 128 → HT1, 254/255/65280 → EXT)."""
    if tray in (254, 255, 65280):
        return EXTERNAL_SLOT
    if 128 <= tray <= 135:
        return f"HT{tray - 127}"
    if 0 <= tray <= 15:
        return f"{chr(ord('A') + tray // 4)}{tray % 4 + 1}"
    return None


class SpoolStore:
    """Table « spools » dans la base de l'historique (même fichier, même verrou)."""

    def __init__(self, db_provider):
        """db_provider : fonction qui renvoie la base de l'historique (ouverte à la première utilisation)."""
        self._provider = db_provider
        self._db = None

    @property
    def db(self):
        if self._db is None:
            self._db = self._provider()
            self._create_table(self._db)
        return self._db

    @staticmethod
    def _create_table(db):
        with db.lock:
            db.conn.execute("""
                CREATE TABLE IF NOT EXISTS spools (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,             -- marque / nom libre (« Bambu PLA Basic »)
                    material TEXT NOT NULL,
                    color TEXT,                     -- RRGGBB
                    weight_g REAL NOT NULL,         -- poids net de filament à l'achat
                    remaining_g REAL NOT NULL,
                    price_kg REAL,
                    printer_id TEXT,                -- NULL = rangée
                    slot TEXT,                      -- A1…D4, HT1…, EXT
                    alert_g REAL NOT NULL DEFAULT 100,
                    alerted INTEGER NOT NULL DEFAULT 0,
                    archived INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )""")
            db.conn.commit()

    def _rows(self, sql, params=()):
        return self.db._rows(sql, params)

    # ── Lecture ──
    def list(self, include_archived: bool = False) -> list[dict]:
        where = "" if include_archived else "WHERE archived = 0"
        rows = self._rows(f"SELECT * FROM spools {where} ORDER BY archived, printer_id IS NULL, slot, name")
        for row in rows:
            row["percent"] = round(100 * row["remaining_g"] / row["weight_g"]) if row["weight_g"] else None
            row["low"] = row["remaining_g"] <= row["alert_g"]
        return rows

    def get(self, spool_id: int) -> dict | None:
        rows = [s for s in self.list(include_archived=True) if s["id"] == spool_id]
        return rows[0] if rows else None

    def by_slot(self, printer_id: str) -> dict:
        """{libellé d'emplacement: bobine} pour une imprimante."""
        return {s["slot"]: s for s in self.list() if s["printer_id"] == printer_id and s["slot"]}

    # ── Écriture ──
    def save(self, data: dict, spool_id: int | None = None) -> int:
        values = {k: data[k] for k in FIELDS if k in data}
        with self.db.lock:
            if values.get("printer_id") and values.get("slot"):
                # Un emplacement ne porte qu'une bobine : l'ancienne retourne en stock
                self.db.conn.execute(
                    "UPDATE spools SET printer_id = NULL, slot = NULL, updated_at = ? "
                    "WHERE printer_id = ? AND slot = ? AND id != ?",
                    (_now(), values["printer_id"], values["slot"], spool_id or -1))
            if spool_id:
                sets = ", ".join(f"{k} = ?" for k in values)
                self.db.conn.execute(f"UPDATE spools SET {sets}, alerted = 0, updated_at = ? WHERE id = ?",
                                     list(values.values()) + [_now(), spool_id])
            else:
                columns = list(values) + ["created_at", "updated_at"]
                cur = self.db.conn.execute(
                    f"INSERT INTO spools ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                    list(values.values()) + [_now(), _now()])
                spool_id = cur.lastrowid
            self.db.conn.commit()
        return spool_id

    def archive(self, spool_id: int):
        """Bobine terminée ou jetée : elle disparaît de la liste (gardée en base)."""
        with self.db.lock:
            self.db.conn.execute("UPDATE spools SET archived = 1, printer_id = NULL, slot = NULL, "
                                 "updated_at = ? WHERE id = ?", (_now(), spool_id))
            self.db.conn.commit()

    def consume(self, printer_id: str, usage: dict[str, float]) -> list[dict]:
        """
        Retire les grammes consommés ({emplacement: g}) des bobines posées sur l'imprimante.
        Renvoie les bobines qui viennent de passer sous leur seuil d'alerte.
        """
        newly_low = []
        slots = self.by_slot(printer_id)
        with self.db.lock:
            for slot, grams in usage.items():
                spool = slots.get(slot)
                if not spool or grams <= 0:
                    continue
                remaining = max(0.0, round(spool["remaining_g"] - grams, 1))
                low_now = remaining <= spool["alert_g"]
                self.db.conn.execute(
                    "UPDATE spools SET remaining_g = ?, alerted = ?, updated_at = ? WHERE id = ?",
                    (remaining, 1 if low_now else 0, _now(), spool["id"]))
                if low_now and not spool["alerted"]:
                    newly_low.append({**spool, "remaining_g": remaining})
            self.db.conn.commit()
        return newly_low


def parse_spool(body: dict) -> dict:
    """Valide le formulaire d'une bobine. ValueError avec un message clair."""
    name = str(body.get("name") or "").strip()[:80]
    material = str(body.get("material") or "").upper().strip()
    if not name:
        raise ValueError("Donne un nom à la bobine (ex. « Bambu PLA Basic blanc »).")
    if material not in MATERIALS:
        raise ValueError("Matériau inconnu.")
    color = str(body.get("color") or "").lstrip("#").upper()[:6]
    if color and (len(color) != 6 or any(c not in "0123456789ABCDEF" for c in color)):
        raise ValueError("Couleur invalide.")

    def number(key, low, high, label, default=None):
        value = body.get(key)
        if value in (None, ""):
            if default is None:
                raise ValueError(f"{label} manquant.")
            return default
        try:
            value = float(str(value).replace(",", "."))
        except ValueError:
            raise ValueError(f"{label} invalide.")
        if not low <= value <= high:
            raise ValueError(f"{label} : entre {low:g} et {high:g}.")
        return value

    weight = number("weight_g", 50, 10000, "Poids de la bobine")
    data = {
        "name": name, "material": material, "color": color or None,
        "weight_g": weight,
        "remaining_g": number("remaining_g", 0, 10000, "Poids restant", default=weight),
        "price_kg": number("price_kg", 0, 1000, "Prix au kilo", default=0) or None,
        "alert_g": number("alert_g", 0, 5000, "Seuil d'alerte", default=DEFAULT_ALERT_G),
        "printer_id": str(body.get("printer_id") or "") or None,
        "slot": str(body.get("slot") or "").upper()[:4] or None,
    }
    if data["remaining_g"] > data["weight_g"]:
        raise ValueError("Le poids restant ne peut pas dépasser le poids de la bobine.")
    if bool(data["printer_id"]) != bool(data["slot"]):
        data["printer_id"] = data["slot"] = None      # emplacement incomplet → rangée
    return data
