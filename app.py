"""
YT Downloader - servidor para uso local o hosting público.

Uso local:
    pip install -r requirements.txt
    python app.py
Luego abrí http://localhost:5000 en el navegador.

Hosting (Render, Railway, VPS, etc.):
    python app.py   (usa waitress si está instalado)

Variables de entorno opcionales:
    PORT              puerto (default 5000)
    HOST              interfaz a escuchar (default 127.0.0.1; en hosting usar 0.0.0.0)
    YTDLP_COOKIES     opcional: contenido de un cookies.txt (Netscape) para
                      autenticar ante YouTube. Mejora la tasa de éxito en
                      hosting, pero no es obligatorio.
    YTDLP_PLAYER_CLIENTS  lista personalizada de clientes de yt-dlp.
    RATE_LIMIT        descargas máximas por IP por minuto (default 5)
    MAX_CONCURRENT    descargas simultáneas en todo el servidor (default 3)
    FILE_TTL_HOURS    horas que un archivo descargado queda disponible (default 6)
    MAX_HEIGHT        tope de resolución para MP4 (default 1080)
"""
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

# ------------------------------------------------------------------ seguridad --
ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}
JOB_ID_RE = re.compile(r"^[a-f0-9]{12}$")

RATE_LIMIT_MAX = int(os.environ.get("RATE_LIMIT", "5"))  # descargas por IP...
RATE_WINDOW = 60                                         # ...en esta ventana (seg)
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "3"))  # simultáneas totales
FILE_TTL_HOURS = float(os.environ.get("FILE_TTL_HOURS", "6"))

try:
    MAX_HEIGHT = int(os.environ.get("MAX_HEIGHT", "1080"))
except ValueError:
    MAX_HEIGHT = 1080


def validate_youtube_url(url: str) -> bool:
    """Solo acepta enlaces http(s) de dominios oficiales de YouTube."""
    if not url or len(url) > 2048:
        return False
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = (p.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        return False
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host):  # IP directa -> fuera
        return False
    return True


class RateLimiter:
    """Limita peticiones por IP en una ventana deslizante (en memoria)."""

    def __init__(self, max_requests: int, window_seconds: float):
        self.max = max_requests
        self.window = window_seconds
        self.hits: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def allow(self, ip: str) -> bool:
        now = time.time()
        with self.lock:
            ts = [t for t in self.hits.get(ip, []) if now - t < self.window]
            if len(ts) >= self.max:
                self.hits[ip] = ts
                return False
            ts.append(now)
            self.hits[ip] = ts
            if len(self.hits) > 20000:  # limpieza ocasional de IPs viejas
                stale = [
                    k for k, v in self.hits.items()
                    if not v or now - v[-1] >= self.window
                ]
                for k in stale:
                    del self.hits[k]
            return True


rate_limiter = RateLimiter(RATE_LIMIT_MAX, RATE_WINDOW)
download_slots = threading.Semaphore(MAX_CONCURRENT)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024  # body chico: solo JSON con la URL


def client_ip() -> str:
    """IP real del cliente (respeta X-Forwarded-For detrás de proxy propio)."""
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        first = fwd.split(",")[0].strip()
        if first:
            return first
    return request.remote_addr or "unknown"


@app.after_request
def security_headers(resp):
    """Headers de seguridad básicos + CSP que ya permite AdSense."""
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://www.googletagservices.com "
        "https://pagead2.googlesyndication.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "img-src 'self' data: https://*.google.com https://*.googlesyndication.com "
        "https://*.doubleclick.net; "
        "frame-src https://www.google.com https://*.googlesyndication.com; "
        "connect-src 'self' https://www.googleadservices.com; "
        "font-src 'self' https://fonts.gstatic.com; base-uri 'self'; frame-ancestors 'none'",
    )
    return resp


# ------------------------------------------------------------------ limpieza --
def cleanup_loop() -> None:
    """Borra archivos descargados hace más de FILE_TTL_HOURS horas."""
    while True:
        time.sleep(300)
        cutoff = time.time() - FILE_TTL_HOURS * 3600
        try:
            for f in DOWNLOADS_DIR.iterdir():
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
        except OSError:
            pass


threading.Thread(target=cleanup_loop, daemon=True).start()

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


def find_ffmpeg():
    """Usa el ffmpeg del sistema si existe; si no, el que trae imageio-ffmpeg."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG_EXE = find_ffmpeg()


def _load_cookiefile():
    """Devuelve la ruta de un cookies.txt para yt-dlp, o None.

    Fuentes (en orden):
      1. Variable de entorno YTDLP_COOKIES con el contenido completo del
         archivo (usado en Render como *secret*; se escribe a disco al arrancar).
      2. Archivo cookies.txt junto a app.py (uso local o repo privado).
    """
    env = os.environ.get("YTDLP_COOKIES", "").strip()
    if env:
        p = BASE_DIR / ".cookies_env.txt"
        try:
            p.write_text(env, encoding="utf-8")
            return str(p)
        except OSError:
            pass
    local = BASE_DIR / "cookies.txt"
    if local.is_file():
        return str(local)
    return None


COOKIEFILE = _load_cookiefile()

# Conjuntos de clientes de YouTube que yt-dlp intentará. El muro anti-bot de
# las IPs de datacenter es intermitente: si el primer conjunto falla (muro o
# formatos limitados por el experimento SABR), se reintenta con el siguiente.
_env_clients = os.environ.get("YTDLP_PLAYER_CLIENTS", "").strip()
if _env_clients:
    CLIENT_SETS = [_env_clients]
else:
    # Rotación de clientes: el muro anti-bot de las IPs de datacenter es
    # intermitente y no siempre se evita con cookies de sesión, así que si el
    # primer conjunto falla (muro o formatos limitados por SABR), se reintenta
    # con el siguiente. Con cookies cargadas, los clientes no-web (tv/android)
    # suelen pasar ese muro mejor que "default".
    CLIENT_SETS = [
        "android,ios,tv,default",            # principal (alta calidad)
        "mweb,web_embedded,tv,android,ios",  # respaldo (más permisivos primero)
    ]


def build_opts(fmt: str, hooks, clients: str | None = None) -> dict:
    opts = {
        "outtmpl": str(DOWNLOADS_DIR / "%(id)s.%(ext)s"),
        "quiet": True,
        "noplaylist": True,
        "progress_hooks": hooks,
        "socket_timeout": 30,
        "retries": 2,
        "extractor_args": {"youtube:player_client": clients or CLIENT_SETS[0]},
    }
    if FFMPEG_EXE:
        opts["ffmpeg_location"] = FFMPEG_EXE
    if COOKIEFILE:
        opts["cookiefile"] = COOKIEFILE
    if fmt == "mp3":
        opts.update(
            {
                "format": "bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
            }
        )
    else:
        opts.update(
            {
                # Tope de resolución para no saturar disco/banda ancha en hosting.
                # Sin exigir ext=mp4/m4a: algunos clientes (android/tv) solo dan
                # streams combinados; merge_output_format se encarga del remux.
                "format": (
                    f"bestvideo[height<={MAX_HEIGHT}]+bestaudio"
                    f"/best[height<={MAX_HEIGHT}]/best"
                ),
                "merge_output_format": "mp4",
            }
        )
    return opts


def human_size(num_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.0f} {unit}" if unit == "B" else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} GB"


def process_job(job_id: str, url: str, fmt: str) -> None:
    import yt_dlp

    def hook(d):
        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                return
            info_dict = d.get("info_dict") or {}
            title = info_dict.get("title")
            if title and not job["title"]:
                job["title"] = title
            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes", 0)
                pct = (done / total * 100.0) if total else 0.0
                job["progress"] = min(99.0, max(job["progress"], pct))
                speed = d.get("speed")
                if speed:
                    job["speed"] = f"{speed / (1024 * 1024):.2f} MB/s"
            elif status == "finished":
                job["progress"] = max(job["progress"], 99.0)

    def _retryable(msg_low: str) -> bool:
        # Errores que valen la pena reintentar con otro conjunto de clientes:
        # muro anti-bot (intermitente) o formatos limitados (experimento SABR).
        return (
            "requested format is not available" in msg_low
            or "sign in to confirm" in msg_low
            or "not a bot" in msg_low
        )

    try:
        info = None
        for attempt, clients in enumerate(CLIENT_SETS):
            opts = build_opts(fmt, [hook], clients=clients)
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True) or {}
                break  # éxito
            except Exception as e1:
                if attempt + 1 < len(CLIENT_SETS) and _retryable(str(e1).lower()):
                    time.sleep(5)  # el muro es intermitente; esperar ayuda
                    continue
                raise

        title = info.get("title") or "video"
        fname = None
        for rd in info.get("requested_downloads") or []:
            fp = rd.get("filepath")
            if fp and Path(fp).exists():
                fname = Path(fp).name
                break
        if not fname:
            vid_id = info.get("id", "")
            exts = ["mp3"] if fmt == "mp3" else ["mp4"]
            for ext in exts:
                cand = DOWNLOADS_DIR / f"{vid_id}.{ext}"
                if cand.exists():
                    fname = cand.name
                    break

        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                return
            job["title"] = title or job["title"]
            if fname:
                fpath = DOWNLOADS_DIR / fname
                size = fpath.stat().st_size if fpath.exists() else 0
                job["filename"] = fname
                job["size"] = size
                job["size_human"] = human_size(size)
                job["status"] = "done"
                job["progress"] = 100.0
            else:
                job["status"] = "error"
                job["error"] = "La descarga terminó pero no se encontró el archivo."
    except Exception as e:
        msg = str(e).strip() or e.__class__.__name__
        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                return
            job["status"] = "error"
            low = msg.lower()
            if "unsupported url" in low:
                job["error"] = (
                    "URL no soportada. Verificá que sea un enlace de YouTube válido."
                )
            elif "video unavailable" in low or "private video" in low:
                job["error"] = "El video no está disponible (puede ser privado, eliminado o restringido por región)."
            elif "sign in to confirm" in low or "not a bot" in low:
                job["error"] = (
                    "YouTube bloqueó temporalmente la descarga desde el servidor. "
                    "Esperá unos segundos y volvé a intentarlo."
                )
            else:
                job["error"] = f"Error al descargar: {msg[:300]}"


def prune_jobs() -> None:
    with jobs_lock:
        if len(jobs) <= 100:
            return
        finished = [jid for jid, j in jobs.items() if j["status"] != "processing"]
        for jid in finished[: len(jobs) - 100]:
            jobs.pop(jid, None)


@app.route("/")
def index():
    return (BASE_DIR / "index.html").read_text(encoding="utf-8")


@app.post("/api/download")
def api_download():
    ip = client_ip()
    if not rate_limiter.allow(ip):
        return jsonify(error="Demasiadas descargas en poco tiempo. Probá de nuevo en unos segundos."), 429

    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    fmt = data.get("fmt", "mp4")
    if fmt not in ("mp3", "mp4"):
        return jsonify(error="Formato inválido."), 400
    if not validate_youtube_url(url):
        return jsonify(error="Ingresá un enlace válido de YouTube (youtube.com o youtu.be)."), 400

    if not download_slots.acquire(blocking=False):
        return jsonify(error="Hay muchas descargas en curso. Probá en unos segundos."), 503

    job_id = uuid.uuid4().hex[:12]
    with jobs_lock:
        jobs[job_id] = {
            "status": "processing",
            "progress": 0.0,
            "speed": None,
            "title": None,
            "filename": None,
            "size": None,
            "size_human": None,
            "error": None,
        }

    def run():
        try:
            process_job(job_id, url, fmt)
        finally:
            download_slots.release()

    threading.Thread(target=run, daemon=True).start()
    prune_jobs()
    return jsonify(job_id=job_id), 201


@app.get("/api/status/<job_id>")
def api_status(job_id):
    if not JOB_ID_RE.fullmatch(job_id):
        return jsonify(error="Tarea no encontrada."), 404
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify(error="Tarea no encontrada."), 404
        return jsonify(dict(job))


@app.get("/api/debug")
def api_debug():
    """Diagnóstico para el dueño del sitio: confirma si las cookies de sesión
    están cargadas en la instancia actual. No expone datos sensibles."""
    n_lines = 0
    if COOKIEFILE and Path(COOKIEFILE).is_file():
        try:
            n_lines = len(Path(COOKIEFILE).read_text(encoding="utf-8").splitlines())
        except OSError:
            n_lines = -1
    return jsonify(
        cookies_loaded=bool(COOKIEFILE),
        cookie_lines=n_lines,
        client_sets=CLIENT_SETS,
    )


@app.get("/file/<job_id>")
def serve_file(job_id):
    if not JOB_ID_RE.fullmatch(job_id):
        return jsonify(error="Archivo no encontrado."), 404
    with jobs_lock:
        job = jobs.get(job_id) or {}
        fname = job.get("filename")
        status = job.get("status")
    if status != "done" or not fname:
        return jsonify(error="El archivo aún no está disponible."), 409
    fpath = (DOWNLOADS_DIR / fname).resolve()
    # Doble verificación de que el archivo sigue dentro de downloads/ y existe.
    if DOWNLOADS_DIR.resolve() not in fpath.parents or not fpath.is_file():
        return jsonify(error="El archivo ya no está disponible en el servidor."), 410
    return send_from_directory(DOWNLOADS_DIR, fname, as_attachment=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    host = os.environ.get("HOST", "127.0.0.1")
    try:
        from waitress import serve

        print(f"YT Downloader listo (waitress): http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}")
        serve(app, host=host, port=port, threads=int(os.environ.get("THREADS", "8")))
    except ImportError:
        print(f"YT Downloader listo (servidor de desarrollo): http://localhost:{port}")
        app.run(host=host, port=port, debug=False)
