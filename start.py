"""
start.py — Punto de entrada de Phos para la nube (Render) y pruebas locales.

Ejecuta en UN solo proceso:

1. El servidor web (webserver.py) en un hilo daemon: expone /health para
   el health-check de Render y para UptimeRobot.
2. El bot tal cual: se ejecuta main.py con runpy, exactamente igual que
   si se hiciera `python main.py` (mismo __name__, mismos guards, misma
   configuración). Así Phos v0.2 se conserva INTACTO: no hay que tocar
   una sola línea de main.py ni de los cogs para desplegarlo.

Orden de arranque:

    .env (sin pisar el entorno) → validación de configuración →
    siembra opcional del .cache de Spotify → servidor web → bot.

Uso:

    python start.py          # local con .env y en Render
    python main.py           # local, como siempre (sin servidor web)

Si prefieres que `python main.py` también levante el servidor web, añade
al principio de main.py (después de los imports):

    from webserver import iniciar_webserver
    iniciar_webserver()

No pasa nada si se llama dos veces: el arranque es idempotente.
"""

from __future__ import annotations

import logging
import os
import runpy
from pathlib import Path

AQUI = Path(__file__).resolve().parent
MAIN_PY = AQUI / 'main.py'


def _configurar_log() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)-8s %(name)s: %(message)s',
    )
    return logging.getLogger('phos.start')


def _sembrar_cache_spotify(log: logging.Logger) -> None:
    """Escribe el .cache de Spotify desde la variable SPOTIPY_CACHE_JSON.

    Es OPCIONAL y solo actúa si la variable está definida.

    Por qué existe: el flujo OAuth de spotipy necesita abrir un navegador
    la primera vez. En local no es problema; en la nube no hay navegador.
    Además el plan gratuito de Render tiene disco efímero: cada despliegue
    o reinicio borra el .cache que va generando spotipy.

    Solución sin tocar cogs: genera el .cache una vez en local (arranca el
    bot y autoriza la app), copia su contenido JSON a la variable de
    entorno SPOTIPY_CACHE_JSON (secreto) en Render, y este arranque lo
    escribe en disco antes de lanzar el bot. spotipy renovará el token con
    el refresh_token incluido, sin navegador y para siempre.
    """
    contenido = (os.getenv('SPOTIPY_CACHE_JSON') or '').strip()
    if not contenido:
        return
    import json
    destino = AQUI / '.cache'
    try:
        datos = json.loads(contenido)
        if not isinstance(datos, dict) or 'refresh_token' not in datos:
            raise ValueError('no parece un .cache de spotipy (falta refresh_token)')
        destino.write_text(json.dumps(datos), encoding='utf-8')
        log.info('.cache de Spotify sembrado desde SPOTIPY_CACHE_JSON '
                 '(el bot renovará el token automáticamente).')
    except (ValueError, OSError) as err:
        log.warning('SPOTIPY_CACHE_JSON está definida pero se ignora: %s', err)


def main() -> int:
    log = _configurar_log()

    # 1) Configuración: .env local SIN sobrescribir el entorno real.
    #    (En Render las variables del panel ya están; load_dotenv no las pisa.)
    import configuracion
    configuracion.cargar_entorno()

    # 2) Validación al arrancar: obligatorias → error claro y salida limpia.
    try:
        avisos = configuracion.validar()
    except configuracion.ErrorConfiguracion as err:
        log.error('%s', err)
        return 1
    for aviso in avisos:
        log.warning('%s', aviso)
    log.info('Configuración:\n%s', '\n'.join(configuracion.resumen()))

    # 3) .cache de Spotify (opcional, ver docstring de la función).
    _sembrar_cache_spotify(log)

    # 4) Servidor web en su hilo daemon (idempotente).
    import webserver
    webserver.iniciar_webserver()

    # 5) El bot: exactamente igual que `python main.py`.
    if not MAIN_PY.is_file():
        log.error('No encuentro main.py junto a start.py (busqué en %s). '
                  'start.py debe vivir en la misma carpeta que main.py.', AQUI)
        return 1
    log.info('Arrancando Phos (main.py)…')

    try:
        runpy.run_path(str(MAIN_PY), run_name='__main__')
    except SystemExit as err:  # main.py terminó solo (p. ej. token malo)
        codigo = err.code if isinstance(err.code, int) else 0
        log.info('Phos terminó (código %s).', codigo or 0)
        return codigo or 0
    except KeyboardInterrupt:
        log.info('Phos detenido manualmente (Ctrl+C).')
        return 0
    except Exception:
        log.exception('Phos terminó con un error.')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())