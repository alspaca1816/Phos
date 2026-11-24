"""
webserver.py — Servidor HTTP de Phos (Flask + Waitress).

Da a la plataforma de alojamiento (Render) y al monitor externo
(UptimeRobot) un punto de entrada HTTP que confirma que el proceso
está vivo, sin interferir con el bot de Discord:

- Flask construye la aplicación (endpoints / y /health).
- Waitress la sirve en producción: es un servidor WSGI maduro, 100 %
  Python, sin configuración extra y que funciona igual en Windows
  (desarrollo local) y en Linux (Render). Se descartó gunicorn porque
  no funciona en Windows de forma nativa y no aporta nada aquí.
- ``iniciar_webserver()`` lo lanza en un HILO DAEMON aparte y es
  IDEMPOTENTE: si ya hay un servidor en marcha, no arranca otro.

¿Por qué un hilo daemon? El servidor web es un servicio auxiliar del
bot, no al revés. Si el bot muere, el proceso debe morir con él (el
hilo daemon no lo mantiene vivo); así /health deja de responder y
UptimeRobot detecta y avisa del apagón. Un hilo no daemon mantendría
el proceso vivo "solo" con el servidor web: el bot estaría caído pero
el monitor diría que todo va bien (falso positivo).

Los endpoints no tocan el bot, su event loop ni datos internos:
/health refleja exclusivamente que este proceso responde.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from flask import Flask, jsonify

log = logging.getLogger('phos.web')

PUERTO_DEFECTO = 5000  # solo para desarrollo local; Render define PORT él solo


# ---------------------------------------------------------------------------
# Aplicación Flask
# ---------------------------------------------------------------------------

def crear_app() -> Flask:
    """Construye (sin arrancar) la aplicación Flask de Phos."""
    app = Flask('phos')

    @app.get('/')
    def raiz() -> dict:
        """Presentación mínima del servicio."""
        return jsonify(
            servicio='Phos',
            descripcion='Bot de Discord (Aternos + Spotify)',
            salud='/health',
        )

    @app.get('/health')
    def salud() -> tuple[dict, int]:
        """Endpoint de salud para Render y UptimeRobot.

        HTTP 200 con un JSON mínimo y comprensible. No expone
        credenciales, variables de entorno ni estado interno del bot.
        """
        return jsonify(status='ok', servicio='Phos'), 200

    return app


# ---------------------------------------------------------------------------
# Arranque en hilo (idempotente)
# ---------------------------------------------------------------------------

_hilo: Optional[threading.Thread] = None
_candado = threading.Lock()


def iniciar_webserver(puerto: Optional[int] = None) -> Optional[threading.Thread]:
    """Arranca el servidor web en un hilo daemon independiente.

    - ``puerto``: si no se indica, se usa la variable de entorno PORT y,
      en su defecto, 5000 (solo desarrollo local; Render inyecta PORT).
    - Es IDEMPOTENTE: si ya hay un servidor corriendo (hilo vivo), no
      hace nada y devuelve el hilo existente. Así da igual que se llame
      dos veces (p. ej. desde start.py y desde main.py).
    - No bloquea: devuelve el hilo inmediatamente.

    Devuelve el hilo del servidor o None si algo falló al lanzarlo.
    """
    global _hilo
    with _candado:
        if _hilo is not None and _hilo.is_alive():
            log.debug('El servidor web ya está en marcha; no se inicia otro.')
            return _hilo
        if puerto is None:
            puerto = _puerto_configurado()
        hilo = threading.Thread(
            target=_servir, args=(puerto,),
            name='phos-webserver', daemon=True,
        )
        _hilo = hilo
        hilo.start()
        return hilo


def en_marcha() -> bool:
    """True si el servidor web está corriendo ahora mismo."""
    return _hilo is not None and _hilo.is_alive()


def _puerto_configurado() -> int:
    """Lee PORT del entorno con valores razonables ante cualquier error."""
    crudo = (os.getenv('PORT') or '').strip()
    if not crudo:
        return PUERTO_DEFECTO
    try:
        return int(crudo)
    except ValueError:
        log.warning('PORT=%r no es un número; se usa el puerto %s.',
                    crudo, PUERTO_DEFECTO)
        return PUERTO_DEFECTO


def _servir(puerto: int) -> None:
    """Cuerpo del hilo: sirve la app con Waitress (bloqueante, en su hilo)."""
    from waitress import serve  # import diferido: solo se necesita aquí
    try:
        log.info('Servidor web escuchando en http://0.0.0.0:%s (/health)', puerto)
        serve(
            crear_app(),
            host='0.0.0.0',      # Render exige escuchar en todas las interfaces
            port=puerto,
            threads=4,           # suficiente para /health y poco más
            ident='Phos',        # cabecera Server
        )
    except Exception:
        # El hilo es daemon: si muere, el bot sigue (y el log explica por qué).
        log.exception('El servidor web se detuvo por un error inesperado.')


# ---------------------------------------------------------------------------
# Modo autónomo (pruebas):  python webserver.py
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)-8s %(name)s: %(message)s',
    )
    _puerto = _puerto_configurado()
    print(f'Servidor web de Phos en http://127.0.0.1:{_puerto}/health (Ctrl+C para salir)')
    _servir(_puerto)