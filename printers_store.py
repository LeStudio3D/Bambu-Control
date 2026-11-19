"""
Stockage des imprimantes configurées dans data/printers.json.
Pas besoin d'une vraie base : c'est de la config qui change rarement.
"""
import json
import os
import threading
import uuid

import config

_lock = threading.Lock()

EDITABLE_FIELDS = ("name", "model", "ip", "access_code", "serial", "has_chamber_override")


def _read_all() -> list:
    if not config.PRINTERS_PATH.exists():
        return []
    with open(config.PRINTERS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_all(printers: list):
    config.PRINTERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.PRINTERS_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(printers, f, indent=2, ensure_ascii=False)
    os.replace(tmp, config.PRINTERS_PATH)  # écriture atomique : jamais de fichier à moitié écrit


def public_view(printer: dict) -> dict:
    """Version sans secrets (access code, n° de série) pour les templates / l'API."""
    return {
        "id": printer["id"],
        "name": printer["name"],
        "model": printer.get("model", "OTHER"),
        "ip": printer.get("ip", ""),
        "has_chamber_override": printer.get("has_chamber_override"),
        "serial_hint": "…" + printer.get("serial", "")[-4:] if printer.get("serial") else "",
    }


def list_printers() -> list:
    with _lock:
        return _read_all()


def get_printer(printer_id: str) -> dict | None:
    with _lock:
        for p in _read_all():
            if p["id"] == printer_id:
                return p
    return None


def add_printer(name: str, model: str, ip: str, access_code: str,
                serial: str, has_chamber_override: bool | None = None) -> dict:
    with _lock:
        printers = _read_all()
        new_printer = {
            "id": uuid.uuid4().hex[:8],
            "name": name,
            "model": model,
            "ip": ip,
            "access_code": access_code,
            "serial": serial,
            "has_chamber_override": has_chamber_override,  # None = défaut du modèle
        }
        printers.append(new_printer)
        _write_all(printers)
        return new_printer


def update_printer(printer_id: str, **fields) -> dict | None:
    clean = {k: v for k, v in fields.items() if k in EDITABLE_FIELDS}
    with _lock:
        printers = _read_all()
        for p in printers:
            if p["id"] == printer_id:
                p.update(clean)
                _write_all(printers)
                return p
    return None


def delete_printer(printer_id: str) -> bool:
    with _lock:
        printers = _read_all()
        remaining = [p for p in printers if p["id"] != printer_id]
        if len(remaining) == len(printers):
            return False
        _write_all(remaining)
        return True
