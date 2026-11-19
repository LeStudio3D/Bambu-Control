"""
Accès à la carte SD de l'imprimante (FTPS implicite, port 990).

On réutilise la connexion TLS de bambulabs_api mais pas ses fonctions
d'envoi/liste : elles cachent toutes les erreurs (renvoient None) et n'ont
pas de délai maximum (une imprimante injoignable bloquait la requête).
Ici chaque opération a un délai et lève SdCardError avec un message clair.
"""
import re
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from bambulabs_api.ftp_client import ImplicitFTP_TLS

PRINTABLE_SUFFIXES = (".3mf", ".gcode")
FTP_PORT = 990
TIMEOUT_SECONDS = 15

# Ligne de « LIST » au format Unix :
# -rw-rw-rw- 1 user group 1234567 Nov  7 10:02 benchy.gcode.3mf
LIST_LINE = re.compile(
    r"^(?P<type>[-dl])\S*\s+\d+\s+\S+\s+\S+\s+(?P<size>\d+)\s+"
    r"(?P<month>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+(?P<time_or_year>\d{1,2}:\d{2}|\d{4})\s+(?P<name>.+)$"
)
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


class SdCardError(Exception):
    """Erreur avec un message compréhensible pour l'utilisateur."""


def parse_list_line(line: str, today: datetime | None = None) -> dict | None:
    """Transforme une ligne de LIST en {name, size, modified} ; None pour un dossier ou une ligne inconnue."""
    match = LIST_LINE.match(line.strip())
    if not match or match["type"] != "-":
        return None
    today = today or datetime.now()
    month = MONTHS.get(match["month"].lower())
    modified = None
    if month:
        try:
            if ":" in match["time_or_year"]:   # année courante (ou précédente si date future)
                hour, minute = map(int, match["time_or_year"].split(":"))
                modified = datetime(today.year, month, int(match["day"]), hour, minute)
                if modified > today:
                    modified = modified.replace(year=today.year - 1)
            else:
                modified = datetime(int(match["time_or_year"]), month, int(match["day"]))
        except ValueError:
            modified = None
    return {"name": match["name"], "size": int(match["size"]),
            "modified": modified.isoformat(timespec="minutes") if modified else None}


def is_safe_name(name: str) -> bool:
    """Un simple nom de fichier : pas de dossier, pas de « .. »."""
    return bool(name) and "/" not in name and "\\" not in name and name not in (".", "..") \
        and not name.startswith(".") and len(name) <= 255


class SdCard:
    def __init__(self, ip: str, access_code: str, timeout: float = TIMEOUT_SECONDS):
        self.ip = ip
        self.access_code = access_code
        self.timeout = timeout

    @contextmanager
    def session(self):
        ftp = ImplicitFTP_TLS(timeout=self.timeout)
        try:
            try:
                ftp.connect(host=self.ip, port=FTP_PORT)
            except OSError as e:
                raise SdCardError(f"Carte SD injoignable ({self.ip}:{FTP_PORT}) : {e}")
            try:
                ftp.login("bblp", self.access_code)
                ftp.prot_p()
            except Exception as e:
                raise SdCardError(f"Accès à la carte SD refusé (access code ?) : {e}")
            yield ftp
        finally:
            try:
                ftp.close()
            except Exception:
                pass

    def list_files(self) -> list[dict]:
        lines: list[str] = []
        with self.session() as ftp:
            try:
                ftp.retrlines("LIST", lines.append)
            except Exception as e:
                raise SdCardError(f"Lecture de la carte SD impossible (carte absente ?) : {e}")
        files = [f for f in (parse_list_line(line) for line in lines) if f]
        printable = [f for f in files if f["name"].lower().endswith(PRINTABLE_SUFFIXES)]
        return sorted(printable, key=lambda f: f["modified"] or "", reverse=True)

    def upload(self, local_path: Path) -> str:
        with self.session() as ftp, open(local_path, "rb") as f:
            try:
                response = ftp.storbinary(f"STOR {local_path.name}", f, blocksize=32768)
            except Exception as e:
                raise SdCardError(f"Envoi sur la carte SD interrompu : {e}")
        if not str(response).startswith("2"):
            raise SdCardError(f"L'imprimante a refusé le fichier : {response}")
        return str(response)

    def download(self, name: str, destination: Path) -> None:
        if not is_safe_name(name):
            raise SdCardError("Nom de fichier invalide.")
        with self.session() as ftp, open(destination, "wb") as f:
            try:
                ftp.retrbinary(f"RETR {name}", f.write, blocksize=65536)
            except Exception as e:
                raise SdCardError(f"Lecture du fichier impossible : {e}")

    def delete(self, name: str) -> None:
        if not is_safe_name(name):
            raise SdCardError("Nom de fichier invalide.")
        with self.session() as ftp:
            try:
                ftp.delete(name)
            except Exception as e:
                raise SdCardError(f"Suppression impossible : {e}")
