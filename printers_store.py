"""
Stockage des imprimantes configurées, dans un simple fichier JSON local
(printers.json). Pas besoin d'une vraie base pour ça, c'est juste de la
config qui change rarement.
"""
import json
import threading
import uuid
from pathlib import Path

PRINTERS_PATH = Path(__file__).parent / "printers.json"
_lock = threading.Lock()


def _read_all() -> list:
    if not PRINTERS_PATH.exists():
        return []
    with open(PRINTERS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_all(printers: list):
    with open(PRINTERS_PATH, "w", encoding="utf-8") as f:
        json.dump(printers, f, indent=2, ensure_ascii=False)


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
            "has_chamber_override": has_chamber_override,  # None = utiliser le défaut du modèle
        }
        printers.append(new_printer)
        _write_all(printers)
        return new_printer


def update_printer(printer_id: str, **fields) -> dict | None:
    with _lock:
        printers = _read_all()
        for p in printers:
            if p["id"] == printer_id:
                p.update(fields)
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
