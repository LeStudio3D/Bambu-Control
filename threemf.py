"""
Lecture des fichiers .3mf tranchés par Bambu Studio / OrcaSlicer.

Un .3mf est une archive zip. On y lit :
- Metadata/slice_info.config     : durée estimée, poids, filaments par plateau
- Metadata/plate_N.json          : impression « par objet » (is_seq_print), type de plateau
- Metadata/project_settings.config : imprimante cible, nombre de filaments du projet
- Metadata/plate_N.png           : la vignette de chaque plateau
"""
import json
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

INFO_VERSION = 2   # à augmenter quand on lit de nouvelles infos (invalide le cache)


def _float(value):
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _json(archive, name: str, names: set) -> dict:
    if name not in names:
        return {}
    try:
        data = json.loads(archive.read(name))
        return data if isinstance(data, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}


def parse(path: Path) -> dict:
    """
    {printer_model, filament_count, plates: [{index, prediction_s, weight_g, filaments,
    has_thumbnail, by_object, bed_type}]}
    """
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        project = _json(archive, "Metadata/project_settings.config", names)
        project_by_object = project.get("print_sequence") == "by object"
        plates = []
        if "Metadata/slice_info.config" in names:
            root = ET.fromstring(archive.read("Metadata/slice_info.config"))
            for plate in root.iter("plate"):
                meta = {m.get("key"): m.get("value") for m in plate.findall("metadata")}
                index = int(meta.get("index") or len(plates) + 1)
                plates.append({
                    "index": index,
                    "prediction_s": int(float(meta.get("prediction") or 0)) or None,
                    "weight_g": _float(meta.get("weight")),
                    "filaments": [{"id": f.get("id"), "type": f.get("type"), "color": f.get("color"),
                                   "used_g": _float(f.get("used_g")), "used_m": _float(f.get("used_m"))}
                                  for f in plate.findall("filament")],
                    "has_thumbnail": f"Metadata/plate_{index}.png" in names,
                })
        if not plates:   # fichier sans infos de tranchage : on garde au moins la vignette
            plates.append({"index": 1, "prediction_s": None, "weight_g": None, "filaments": [],
                           "has_thumbnail": "Metadata/plate_1.png" in names})
        for plate in plates:
            plate_json = _json(archive, f"Metadata/plate_{plate['index']}.json", names)
            seq = plate_json.get("is_seq_print")
            # « Par objet » : le timelapse est impossible (la buse va d'un objet à l'autre)
            plate["by_object"] = bool(seq) if seq is not None else project_by_object
            plate["bed_type"] = str(plate_json.get("bed_type") or "")
        filament_types = project.get("filament_type")
        return {
            "version": INFO_VERSION,
            "printer_model": str(project.get("printer_model") or ""),
            "filament_count": len(filament_types) if isinstance(filament_types, list) else 0,
            "plates": plates,
        }


def thumbnail(path: Path, plate_index: int = 1) -> bytes | None:
    with zipfile.ZipFile(path) as archive:
        name = f"Metadata/plate_{int(plate_index)}.png"
        return archive.read(name) if name in archive.namelist() else None


def _rgb(color: str | None):
    color = (color or "").lstrip("#")
    try:
        return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def suggest_trays(filaments: list[dict], ams_slots: list[dict]) -> list[dict]:
    """Pour chaque filament du fichier, la bobine AMS la plus proche (même matériau, couleur voisine)."""
    suggestions = []
    for filament in filaments:
        wanted_type = (filament.get("type") or "").upper()
        wanted_rgb = _rgb(filament.get("color"))
        best, best_score = None, None
        for slot in ams_slots or []:
            if slot.get("empty") or (slot.get("type") or "").upper() != wanted_type:
                continue
            slot_rgb = _rgb(slot.get("color"))
            score = sum((a - b) ** 2 for a, b in zip(wanted_rgb, slot_rgb)) if wanted_rgb and slot_rgb else 10 ** 6
            if best_score is None or score < best_score:
                best, best_score = slot, score
        suggestions.append({
            "filament": filament,
            "slot": best["label"] if best else None,
            "tray": best["global_index"] if best else None,   # valeur pour ams_mapping
            "note": ("même matériau, couleur proche" if best and best_score < 60 ** 2 * 3
                     else "même matériau, couleur différente" if best
                     else f"aucune bobine {wanted_type or '?'} dans l'AMS"),
        })
    return suggestions
