"""
File d'impression : liste de fichiers de la carte SD à enchaîner, par imprimante.
Stockée dans data/queue.json. Rien n'est lancé tout seul : quand l'imprimante
est libre, la console propose le fichier suivant et l'utilisateur confirme.
"""
import json
import os
import threading

import config

QUEUE_PATH = config.DATA_DIR / "queue.json"
_lock = threading.Lock()


def _read() -> dict:
    if not QUEUE_PATH.exists():
        return {}
    with open(QUEUE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _write(data: dict):
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, QUEUE_PATH)


def items(printer_id: str) -> list[dict]:
    with _lock:
        return _read().get(printer_id, [])


def add(printer_id: str, name: str, plate: int = 1, options: dict | None = None,
        first: bool = False) -> list[dict]:
    """options : choix de lancement (use_ams, ams_mapping, timelapse, bed_leveling…)."""
    item = {"use_ams": True, **(options or {}), "name": name, "plate": int(plate)}
    with _lock:
        data = _read()
        queue = data.setdefault(printer_id, [])
        queue.insert(0, item) if first else queue.append(item)
        _write(data)
        return queue


def remove(printer_id: str, index: int) -> list[dict]:
    with _lock:
        data = _read()
        queue = data.get(printer_id, [])
        if 0 <= index < len(queue):
            queue.pop(index)
        data[printer_id] = queue
        _write(data)
        return queue


def move_up(printer_id: str, index: int) -> list[dict]:
    with _lock:
        data = _read()
        queue = data.get(printer_id, [])
        if 0 < index < len(queue):
            queue[index - 1], queue[index] = queue[index], queue[index - 1]
        _write(data)
        return queue


def pop_first(printer_id: str) -> dict | None:
    with _lock:
        data = _read()
        queue = data.get(printer_id, [])
        if not queue:
            return None
        first = queue.pop(0)
        _write(data)
        return first
