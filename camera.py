"""
Caméra des imprimantes Bambu Lab.

Deux protocoles selon le modèle :
- "tcp"  : A1, A1 mini, P1P, P1S → flux JPEG sur TLS port 6000
- "rtsp" : X1, P2S, H2…         → RTSPS port 322, converti en JPEG par ffmpeg

Une seule connexion caméra par imprimante, ouverte seulement quand quelqu'un
regarde (CameraFeed.touch) et fermée après CAMERA_IDLE_SECONDS sans
spectateur : moins de charge pour le Pi et pour l'imprimante (qui n'accepte
souvent qu'un client caméra à la fois, ex. Bambu Handy).
"""
import logging
import shutil
import socket
import ssl
import struct
import subprocess
import threading
import time

log = logging.getLogger("camera")

LIVE = "live"
CONNECTING = "connecting"
UNAVAILABLE = "unavailable"
STOPPED = "stopped"

CAMERA_IDLE_SECONDS = 60      # plus de spectateur depuis → on coupe
FRAME_STALE_SECONDS = 10      # plus d'image depuis → considérée en panne
RETRY_DELAYS = (2, 5, 10, 30)  # attente entre deux tentatives (secondes)

JPEG_START = b"\xff\xd8"
JPEG_END = b"\xff\xd9"
MAX_FRAME_BYTES = 5 * 1024 * 1024


class CameraFeed:
    """Base commune : thread de lecture, dernière image, état lisible par l'interface."""

    protocol = "?"

    def __init__(self, ip: str, access_code: str):
        self.ip = ip
        self.access_code = access_code
        self._lock = threading.Lock()
        self._frame = None
        self._frame_id = 0
        self._frame_at = 0.0
        self._state = STOPPED
        self._message = ""
        self._last_viewer_at = 0.0
        self._stop_event = threading.Event()
        self._thread = None

    # ── Utilisé par le worker / Flask ──
    def touch(self):
        """Un spectateur regarde : démarre la caméra si besoin."""
        self._last_viewer_at = time.time()
        if self._thread is None or not self._thread.is_alive():
            self.start()

    def idle(self) -> bool:
        return time.time() - self._last_viewer_at > CAMERA_IDLE_SECONDS

    def start(self):
        # Nouvel Event à chaque démarrage : un ancien thread encore en train de
        # s'arrêter garde le sien (déjà levé) et ne repart jamais.
        self._stop_event = threading.Event()
        self._set_state(CONNECTING, "Connexion à la caméra…")
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"camera-{self.ip}")
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        self._close()
        self._set_state(STOPPED, "")

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop_event.is_set()

    def latest(self):
        """(jpeg, identifiant de l'image) ; (None, 0) si aucune image récente."""
        with self._lock:
            if self._frame is None or time.time() - self._frame_at > FRAME_STALE_SECONDS:
                return None, 0
            return self._frame, self._frame_id

    def status(self) -> dict:
        with self._lock:
            state, message = self._state, self._message
            if state == LIVE and time.time() - self._frame_at > FRAME_STALE_SECONDS:
                state, message = CONNECTING, "Flux interrompu, reconnexion…"
            return {"state": state, "message": message, "frame_id": self._frame_id,
                    "protocol": self.protocol}

    # ── Interne ──
    def _set_state(self, state: str, message: str):
        with self._lock:
            if state != self._state:
                log.info("[%s] caméra %s → %s %s", self.ip, self._state, state, message)
            self._state, self._message = state, message

    def _publish_frame(self, jpeg: bytes):
        with self._lock:
            self._frame = jpeg
            self._frame_id += 1
            self._frame_at = time.time()
            self._state, self._message = LIVE, ""

    def _run(self):
        stop_event = self._stop_event
        attempt = 0
        while not stop_event.is_set():
            try:
                self._stream()           # bloque tant que le flux fonctionne
                attempt = 0
            except CameraError as e:
                self._set_state(UNAVAILABLE, str(e))
            except Exception as e:  # erreur réseau imprévue : on retente
                self._set_state(UNAVAILABLE, f"Caméra indisponible ({e})")
            finally:
                self._close()
            delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
            attempt += 1
            stop_event.wait(delay)

    def _stream(self):
        raise NotImplementedError

    def _close(self):
        pass


class CameraError(Exception):
    """Erreur avec un message compréhensible pour l'utilisateur."""


# ── A1 / P1 : JPEG sur TLS port 6000 ──────────────────────
def build_auth_packet(access_code: str, username: str = "bblp") -> bytes:
    packet = struct.pack("<IIII", 0x40, 0x3000, 0, 0)
    packet += username.encode("ascii").ljust(32, b"\x00")
    packet += access_code.encode("ascii").ljust(32, b"\x00")
    return packet


def recv_exact(sock, size: int) -> bytes:
    """Lit exactement `size` octets (un recv peut en renvoyer moins)."""
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("connexion fermée par l'imprimante")
        data += chunk
    return bytes(data)


def read_tcp_frames(sock):
    """Génère les images JPEG : en-tête de 16 octets (taille en little-endian) puis le JPEG."""
    while True:
        header = recv_exact(sock, 16)
        size = int.from_bytes(header[0:4], "little")
        if not 0 < size <= MAX_FRAME_BYTES:
            raise ConnectionError(f"en-tête d'image invalide ({size} octets)")
        payload = recv_exact(sock, size)
        if payload.startswith(JPEG_START) and payload.endswith(JPEG_END):
            yield payload


class TcpJpegCamera(CameraFeed):
    protocol = "tcp"
    PORT = 6000

    def __init__(self, ip, access_code):
        super().__init__(ip, access_code)
        self._sock = None

    def _stream(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # certificat auto-signé de l'imprimante
        try:
            raw = socket.create_connection((self.ip, self.PORT), timeout=5)
        except OSError as e:
            raise CameraError(f"Caméra injoignable sur {self.ip}:{self.PORT} ({e}).")
        self._sock = context.wrap_socket(raw, server_hostname=self.ip)
        self._sock.settimeout(10)
        self._sock.sendall(build_auth_packet(self.access_code))
        try:
            for jpeg in read_tcp_frames(self._sock):
                if self._stop_event.is_set():
                    return
                self._publish_frame(jpeg)
        except ConnectionError as e:
            if self._frame_id == 0:
                raise CameraError("La caméra refuse la connexion : access code incorrect, ou un "
                                  "autre appareil (Bambu Handy/Studio) l'utilise déjà.")
            raise CameraError(f"Flux caméra coupé ({e}), reconnexion…")
        except socket.timeout:
            raise CameraError("La caméra ne répond plus, reconnexion…")

    def _close(self):
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


# ── X1 / H2 / P2S : RTSPS converti par ffmpeg ─────────────
def split_jpeg_stream(stream, chunk_size: int = 65536):
    """Découpe un flux MJPEG brut (sortie ffmpeg) en images JPEG."""
    buffer = bytearray()
    while True:
        chunk = stream.read1(chunk_size) if hasattr(stream, "read1") else stream.read(chunk_size)
        if not chunk:
            return
        buffer += chunk
        while True:
            start = buffer.find(JPEG_START)
            end = buffer.find(JPEG_END, start + 2) if start >= 0 else -1
            if start < 0 or end < 0:
                break
            yield bytes(buffer[start:end + 2])
            del buffer[:end + 2]
        if len(buffer) > MAX_FRAME_BYTES:
            buffer.clear()


class RtspCamera(CameraFeed):
    protocol = "rtsp"
    FPS = 2          # suffisant pour surveiller, léger pour un Raspberry Pi
    WIDTH = 1280

    def __init__(self, ip, access_code):
        super().__init__(ip, access_code)
        self._process = None

    def source_url(self) -> str:
        return f"rtsps://bblp:{self.access_code}@{self.ip}:322/streaming/live/1"

    def ffmpeg_command(self) -> list[str]:
        return ["ffmpeg", "-hide_banner", "-loglevel", "error",
                "-rtsp_transport", "tcp", "-i", self.source_url(),
                "-an", "-vf", f"fps={self.FPS},scale={self.WIDTH}:-2",
                "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "6", "-"]

    def _stream(self):
        if shutil.which("ffmpeg") is None:
            raise CameraError("Ce modèle utilise une caméra RTSP : installe ffmpeg "
                              "(Raspberry Pi/Linux : sudo apt install ffmpeg).")
        self._process = subprocess.Popen(self.ffmpeg_command(), stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        for jpeg in split_jpeg_stream(self._process.stdout):
            if self._stop_event.is_set():
                return
            self._publish_frame(jpeg)
        if self._frame_id == 0:
            raise CameraError("Flux RTSP inaccessible : vérifie que « LAN Mode liveview » est "
                              "activé sur l'imprimante et que l'access code est correct.")
        raise CameraError("Flux RTSP interrompu, reconnexion…")

    def _close(self):
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            process.kill()


def create_camera(protocol: str, ip: str, access_code: str) -> CameraFeed:
    if protocol == "rtsp":
        return RtspCamera(ip, access_code)
    return TcpJpegCamera(ip, access_code)
