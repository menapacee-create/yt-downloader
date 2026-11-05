# YT Downloader (MP3 / MP4)

Página web para descargar videos de YouTube como **MP3** (solo audio) o **MP4** (video completo).
Lista para uso local y para publicarla en un hosting con publicidad.

## Requisitos

- [Python 3.12](https://www.python.org/downloads/) — al instalar, marcá la casilla **"Add python.exe to PATH"**.
- No hace falta instalar ffmpeg por separado: se descarga automáticamente con `imageio-ffmpeg`.

## Puesta en marcha (local)

```powershell
cd youtube-downloader
pip install -r requirements.txt
python app.py
```

Después abrí **http://localhost:5000** en el navegador.

### Uso

1. Pegá el enlace del video de YouTube.
2. Elegí 🎵 **MP3 · Audio** o 🎬 **MP4 · Video**.
3. Tocá **Descargar**, esperá la barra de progreso y presioná **Guardar archivo** cuando termine (se guarda en tu carpeta *Descargas*).

Los archivos descargados quedan en la carpeta `downloads/` y se eliminan solos pasadas 6 horas.

## Variables de entorno (opcionales)

| Variable         | Default     | Qué hace                                              |
|------------------|-------------|-------------------------------------------------------|
| `PORT`           | `5000`      | Puerto del servidor                                    |
| `HOST`           | `127.0.0.1` | Interfaz a escuchar (en hosting usar `0.0.0.0`)       |
| `RATE_LIMIT`     | `5`         | Descargas máximas por IP cada minuto                   |
| `MAX_CONCURRENT` | `3`         | Descargas simultáneas en todo el servidor              |
| `FILE_TTL_HOURS` | `6`         | Horas que un archivo descargado queda disponible       |
| `MAX_HEIGHT`     | `1080`      | Tope de resolución para MP4 (ahorra disco y banda)     |

## Seguridad incluida

- **Solo URLs de YouTube**: se rechaza cualquier enlace que no sea `youtube.com`, `youtu.be` o sus subdominios oficiales (evita que usen tu servidor para descargar de donde sea / atacar servidores internos).
- **Rate limiting por IP**: 5 descargas por minuto por visitante; si se pasa, devuelve error 429.
- **Límite de concurrencia global** (3 descargas a la vez) para que no lo tumben con muchas peticiones simultáneas.
- **Archivos temporales**: los archivos descargados se borran solos pasadas las horas configuradas; el ID del archivo es aleatorio y se valida antes de servirlo.
- **Headers de seguridad** en todas las respuestas: `X-Content-Type-Options`, `X-Frame-Options: DENY` (anti clickjacking), `Referrer-Policy` y una CSP estricta que además ya permite los dominios de Google AdSense para cuando agregues publicidad.
- **Timeouts y reintentos** en yt-dlp para no colgar descargas problemáticas.

> Recomendación extra: al publicarla, usá siempre **HTTPS** (todos los hostings lo dan gratis con certificado automático).

## Publicar en un hosting + publicidad

### Opción A — PaaS (la más fácil): Render o Railway

yt-dlp corre en el servidor, así que necesitás un hosting que ejecute Python
(no sirve un hosting estático tipo Netlify/Vercel sin funciones).

1. Subí esta carpeta a un repositorio de GitHub.
2. En [Render](https://render.com) (o Railway): *New → Web Service*, elegí el repo.
3. Build command: `pip install -r requirements.txt` · Start command: `python app.py`.
4. Seteá la variable de entorno `HOST=0.0.0.0` (el puerto lo detecta solo).
5. Listo: te da una URL tipo `https://tu-app.onrender.com`.

> En Render, el plan gratuito "duerme" tras ~15 min sin uso; la primera descarga después de estar inactivo tarda unos segundos en despertar. Para uso serio, el plan Starter (~$7/mes) lo mantiene despierto.

### Opción B — VPS (más control): Hetzner, DigitalOcean, etc.

```bash
sudo apt install python3-pip
pip install -r requirements.txt
HOST=0.0.0.0 PORT=8000 nohup python app.py > server.log 2>&1 &
```

Y poné un proxy inverso (Caddy o Nginx) adelante con HTTPS automático. Con Caddy es una línea en el `Caddyfile`:

```
tudominio.com {
    reverse_proxy localhost:8000
}
```

### Agregar publicidad (Google AdSense)

1. Creá cuenta en [AdSense](https://adsense.google.com), agregá tu dominio y esperá la aprobación.
2. Google te da un bloque de código `<ins class="adsbygoogle" ...>` + el script global.
3. En `index.html` hay **dos espacios marcados** con comentarios:
   - `<div class="ad-slot" id="adTop">` — arriba, antes de la tarjeta principal.
   - `<div class="ad-slot" id="adBottom">` — abajo, después del pie.

   Pegá el código de AdSense **dentro** de esos divs (los espacios quedan invisibles hasta que los llenes). El script global de AdSense va en el `<head>`.
4. La CSP ya está configurada para permitir los dominios de Google, no tenés que tocar nada más.

## Solución de problemas

- **El botón "Guardar archivo" no hace nada**: usá un navegador normal (Chrome/Edge/Firefox). En navegadores embebidos dentro de otras apps a veces el diálogo de guardado se comporta distinto; si pasa, el botón abre el archivo en una pestaña nueva y podés guardarlo desde ahí.
- **Error 429**: esperá unos segundos (límite de descargas por minuto).
- **"El video no está disponible"**: el video es privado, eliminado o tiene restricciones regionales.
- **Aviso de yt-dlp sobre "JavaScript runtime" en la consola**: es solo un aviso; las descargas funcionan igual. Si algún día faltaran formatos, instalá [Node.js](https://nodejs.org) (o deno) y reiniciá el servidor.
