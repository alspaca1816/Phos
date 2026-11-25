"""
obtener_refresh_token.py — Genera el SPOTIFY_REFRESH_TOKEN para Phos.

Se ejecuta UNA VEZ en tu PC (donde sí hay navegador):

    python obtener_refresh_token.py

Qué hace:
  1. Si ya existe un .cache local (de cuando autorizaste el bot en tu PC),
     lee el refresh_token de ahí y te lo muestra: no tienes que hacer nada
     más.
  2. Si no hay .cache, abre tu navegador para que autorices la app de
     Spotify (la misma autorización de siempre; la página vuelve a
     http://127.0.0.1:8888/callback, que solo funciona en tu PC) y guarda
     el resultado en .cache.

Al terminar imprime el valor de SPOTIFY_REFRESH_TOKEN y cómo ponerlo en
Render (Environment) y en tu .env local.

Notas:
  - El refresh token NO caduca por tiempo; solo se invalida si revocas el
    acceso a la app (https://www.spotify.com/account/apps) o si la app
    cambia de modo de desarrollo/cliente.
  - NO se necesita cuenta Premium para buscar y añadir canciones.
  - Este archivo no se sube a GitHub: el .gitignore ya excluye .cache.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import spotipy
from spotipy.oauth2 import SpotifyOAuth

SCOPES = 'playlist-modify-public playlist-modify-private'
RUTA_CACHE = Path.cwd() / '.cache'


def main() -> int:
    client_id = os.environ.get('SPOTIPY_CLIENT_ID')
    client_secret = os.environ.get('SPOTIPY_CLIENT_SECRET')
    redirect_uri = os.environ.get(
        'SPOTIPY_REDIRECT_URI', 'http://127.0.0.1:8888/callback')

    if not client_id or not client_secret:
        print('Faltan SPOTIPY_CLIENT_ID / SPOTIPY_CLIENT_SECRET en el .env.')
        return 1
    if '127.0.0.1' not in redirect_uri and 'localhost' not in redirect_uri:
        print(f'OJO: SPOTIPY_REDIRECT_URI es {redirect_uri!r} (no es local).')
        print('El flujo con navegador necesita el redirect local '
              'http://127.0.0.1:8888/callback registrado en tu app de Spotify.')

    # 1) ¿Ya hay .cache con refresh_token?
    if RUTA_CACHE.is_file():
        try:
            datos = json.loads(RUTA_CACHE.read_text(encoding='utf-8'))
            refresh = datos.get('refresh_token')
            if refresh:
                mostrar(refresh, origen='.cache existente')
                return 0
        except (OSError, json.JSONDecodeError):
            pass  # .cache ilegible → hacer el flujo completo

    # 2) Flujo con navegador (igual que la primera vez que autorizaste el bot)
    print('Abriendo el navegador para autorizar la app de Spotify...')
    print('(Autoriza y vuelve a esta ventana; en el navegador pondrá '
          '"You can close this window" cuando termine.)')
    oauth = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=SCOPES,
        open_browser=True,
    )
    # get_access_token abre el navegador, espera el callback en
    # 127.0.0.1:8888 y guarda el .cache.
    oauth.get_access_token()
    datos = json.loads(RUTA_CACHE.read_text(encoding='utf-8'))
    refresh = datos.get('refresh_token')
    if not refresh:
        print('No se obtuvo refresh_token. Revisa que autorizaste la app '
              'completa y que el redirect URI coincida.')
        return 1
    mostrar(refresh, origen='nueva autorización')
    return 0


def mostrar(refresh: str, origen: str) -> None:
    print(f'\n[OK] Refresh token obtenido ({origen}):\n')
    print(f'SPOTIFY_REFRESH_TOKEN={refresh}\n')
    print('Cómo usarlo:')
    print('  · En Render: servicio Phos → Environment → Add environment '
          'variable:')
    print(f'        Key:   SPOTIFY_REFRESH_TOKEN')
    print(f'        Value: {refresh}')
    print('    (Guarda y el servicio se reinicia solo con el modo servidor '
          'de Spotify, sin navegador).')
    print('  · En tu .env local (opcional, si también quieres ese modo en '
          'casa): añade la misma línea.')
    print('\nConsejo de seguridad: el refresh token da acceso a modificar '
          'tus playlists con los permisos que autorizaste. No lo pegues en '
          'chats ni lo subas a GitHub (el .gitignore ya protege el .env).')


if __name__ == '__main__':
    sys.exit(main())