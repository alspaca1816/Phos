# Phos

Bot de Discord con:

- **Aternos**: `/estado`, `/encender`, `/apagar`, `/reiniciar` (según los roles
  de `config_aternos.json`) y presencia automática con ping directo de Minecraft.
- **Spotify**: escribe `"Nombre - Autor"` en el canal de música y el bot la
  busca y la añade a la playlist comunal, reaccionando con emojis.

Corre como **un único proceso** que mantiene a la vez el bot de Discord y un
pequeño servidor web (`/health`) para el alojamiento en Render y la
monitorización con UptimeRobot.

---

## Estructura

| Archivo | Qué es |
|---|---|
| `main.py` | El bot, tal cual (punto de entrada original). **No hace falta tocarlo.** |
| `start.py` | Punto de entrada para la nube: valida la configuración, arranca el servidor web y ejecuta `main.py` igual que `python main.py`. |
| `webserver.py` | Servidor Flask servido con Waitress en un hilo daemon. Endpoints `/` y `/health`. Arranque idempotente. |
| `configuracion.py` | Carga del `.env` (sin pisar el entorno), validación al arrancar y resumen sin secretos. |
| `aternos_client.py` | Cliente del panel de Aternos (protocolo ajax actual). Usa Node.js si está disponible (en Render sí lo está) con respaldo en Python. |
| `cogs/aternos.py`, `cogs/spotify.py` | Lógica del bot (v0.2, intacta). |
| `config_aternos.json` | Roles que pueden encender/apagar/reiniciar/ver la IP. |
| `.env.example` | Plantilla de configuración: **cópiala a `.env`** y rellénala. |
| `render.yaml` | Blueprint de despliegue en Render (opcional, comodidad). |

## Configuración

Todo se configura por **variables de entorno** (nunca credenciales en el código):

- **Local**: crea un `.env` a partir de `.env.example` (el `.env` no se sube
  a GitHub: `.gitignore` lo excluye).
- **Render**: las mismas variables, definidas en el panel del servicio
  (Environment). Las del panel siempre ganan sobre cualquier `.env`.

| Variable | Obligatoria | Qué hace |
|---|---|---|
| `DISCORD_TOKEN` | Sí (secreto) | Token del bot. Sin ella no arranca. |
| `PREFIX` | Recomendada | Prefijo de comandos de texto (si tu `main.py` la usa). |
| `GUILD_ID` | Opcional | Sincroniza los slash commands al instante en tu servidor. |
| `SPOTIPY_CLIENT_ID` / `SPOTIPY_CLIENT_SECRET` | Para Spotify (secreto) | App de developer.spotify.com. |
| `SPOTIPY_REDIRECT_URI` | Para Spotify | Misma URL que en el dashboard de Spotify. |
| `CHANNEL_ID` | Para Spotify | Canal donde se escriben las canciones. |
| `PLAYLIST_ID` | Para Spotify | Playlist comunal (es texto, no un número). |
| `EMOJI_SUCCESS_ID`, `EMOJI_DUPLICATE_ID`, `EMOJI_ERROR_ID`, `EMOJI_WARNING_ID` | Para Spotify | Reacciones del bot. |
| `SPOTIPY_CACHE_JSON` | Opcional (secreto) | Solo Render: contenido del `.cache` de spotipy para renovar el token sin navegador (ver abajo). |
| `ATERNOS_SESSION` | Para Aternos (secreto) | Cookie de sesión de aternos.org. Caduca: se renueva en el panel de Render. |
| `ATERNOS_SERVER_ID` (alias `ATERNOS_SERVER`) | Opcional | Servidor concreto; si falta, se usa el primero de la cuenta. |
| `ATERNOS_USUARIO` / `ATERNOS_PASSWORD` | Opcional (secretos) | Login alternativo (puede chocar con el captcha; mejor la cookie). |
| `MC_ADDRESS` (alias `MC_ADRESS`) | Para presencia | Dirección del servidor para el ping con mcstatus. |
| `PORT` | Solo local | Puerto del servidor web. Render lo define él solo. |

Al arrancar con `start.py` se valida todo: si falta una obligatoria, el bot no
arranca y el log dice cuál y cómo conseguirla; las opcionales que falten
producen un aviso indicando qué función se pierde. Los registros nunca
muestran valores de secretos.

## Ejecución local

```powershell
pip install -r requirements.txt

python main.py     # como siempre (solo el bot)
python start.py    # bot + servidor web en http://127.0.0.1:5000/health
python webserver.py  # solo el servidor web, para probar /health
```

`start.py` ejecuta `main.py` tal cual (`runpy`), así que el bot se comporta
exactamente igual. Si quieres que `python main.py` también levante el servidor
web, añade al principio de `main.py` (tras los imports):

```python
from webserver import iniciar_webserver
iniciar_webserver()
```

Es idempotente: llamarlo dos veces (start.py + main.py) no crea dos servidores.

---

## Desplegar en Render

1. **Sube el proyecto a GitHub** (ver comandos abajo). Comprueba antes que
   `.env` **no** queda versionado (`git status` / `git ls-files`).
2. En [render.com](https://render.com) → **New** → o bien **Blueprint**
   (selecciona el repo: lee `render.yaml` y lo deja todo configurado), o
   **Web Service** a mano:
   - **Runtime**: Python · **Branch**: main · **Plan**: Free
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python start.py`
   - **Health Check Path**: `/health`
3. En **Environment**, añade las variables de la tabla de arriba
   (empieza por `DISCORD_TOKEN`).
4. **Create Web Service** y espera el despliegue. En los logs verás:
   `Servidor web escuchando en http://0.0.0.0:PORT` y, después, el arranque
   normal del bot (`connected to Gateway`).
5. Comprueba `https://TU-SERVICIO.onrender.com/health` → debe responder
   `{"status":"ok","servicio":"Phos"}`.

### Spotify en la nube: `SPOTIPY_CACHE_JSON` (importante)

El login de Spotify necesita navegador la primera vez, y en Render no hay.
Además el plan gratuito tiene **disco efímero**: lo escrito en disco se pierde
en cada reinicio. Para que las canciones sigan funcionando en la nube:

1. Arranca el bot **en tu PC** una vez con las mismas credenciales y autoriza
   la app cuando se abra el navegador. Se crea un archivo `.cache` en la raíz.
2. Copia su contenido (un JSON con `refresh_token`) y pégalo como valor de la
   variable `SPOTIPY_CACHE_JSON` en el panel de Render.
3. `start.py` lo escribirá en disco en cada arranque y spotipy renovará el
   token solo, sin navegador, para siempre (mientras no revoques la app).

Sin esta variable, el resto del bot funciona igual; solo las canciones
reaccionarán con el emoji de aviso.

### Node.js y Aternos

El cliente de Aternos extrae su token con Node.js cuando está disponible.
Los entornos nativos de Render **incluyen `node` en runtime**, así que no hay
que hacer nada. Si algún día la cookie `ATERNOS_SESSION` caduca, los comandos
lo avisan: renuévala en el panel de Render (Environment → editar valor) y el
bot la adopta en el siguiente arranque (o al reintento automático).

## Configurar UptimeRobot

1. Cuenta gratis en [uptimerobot.com](https://uptimerobot.com) → **Add New Monitor**.
2. **Monitor Type**: HTTP(s) · **Friendly Name**: Phos ·
   **URL**: `https://TU-SERVICIO.onrender.com/health`
3. **Monitoring Interval**: 5 minutos (el mínimo del plan gratuito) → Create.

Con un chequeo cada 5 minutos el servicio siempre tiene tráfico HTTP entrante,
lo que en la práctica mantiene despierto el plan gratuito de Render, y además
recibes avisos por correo si el bot/process cae de verdad.

## Limitaciones del plan gratuito de Render

- **Suspensión por inactividad**: sin tráfico entrante durante 15 minutos, el
  servicio se duerme y el bot se desconecta de Discord. Con UptimeRobot a
  5 minutos no ocurre. (La documentación de Render también cuenta los
  mensajes WebSocket entrantes como tráfico, y el bot recibe actividad del
  gateway de Discord constantemente, pero no conviene depender solo de eso.)
- **Arranque en frío**: al despertar tarda ~1 minuto en responder.
- **750 horas/instancia al mes**: un solo servicio 24/7 cabe (un mes tiene
  ~744 h). Si se agotan, Render suspende los servicios gratuitos hasta el
  mes siguiente.
- **Disco efímero**: todo lo escrito en disco se pierde en cada despliegue,
  reinicio o suspensión (por eso existe `SPOTIPY_CACHE_JSON`).
- Render recomienda oficialmente *Background Workers* para bots de Discord,
  pero no tienen plan gratuito; el patrón "Web Service + /health + monitor" es
  la forma estándar de alojar un bot en el plan free. Para uso 24/7 serio, el
  plan Starter (~7 US$/mes) elimina todas estas limitaciones.

## Git: revisar y publicar los cambios

Desde la carpeta del proyecto (`C:\Users\LENOVO\Desktop\Phos`):

```powershell
git init                                    # solo si aún no es un repositorio
git remote add origin https://github.com/alspaca1816/Phos.git
git status                                  # revisa qué va a subirse
git ls-files | findstr /i "env"             # NO debe aparecer .env
git add .
git commit -m "Infraestructura para despliegue en Render (webserver + start)"
git push -u origin main                     # cuando lo hayas revisado
```

Si en algún momento `.env` llegó a versionarse, quítalo del índice SIN borrar
tu copia local:

```powershell
git rm --cached .env
git commit -m "Excluir .env del repositorio"
```

Y si llegó a PUBLICARSE en un repositorio público: **rota los secretos**
(Reset Token de Discord, Client Secret de Spotify, cookie de Aternos) antes
de nada; retirarlos después no borra el historial.
