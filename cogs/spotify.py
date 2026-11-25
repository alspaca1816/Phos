"""
cogs/spotify.py — Módulo de Spotify para Phos.

Es tu antiguo spotify.py convertido en cog para que conviva con el resto
del bot (no puede haber dos `discord.Client` a la vez). El comportamiento:

- Escucha un canal concreto (CHANNEL_ID del .env).
- SOLO se procesan los mensajes con la estructura "Nombre - Autor"
  (un guion separando dos textos no vacíos). Cualquier otra cosa que
  se escriba en el canal (saludos, una canción a secas, enlaces...)
  se ignora en silencio: no se busca ni se reacciona.
- Busca la canción en Spotify y la añade a la playlist (PLAYLIST_ID).
- Reacciona con tus emojis personalizados:
  SUCCESS = añadida · DUPLICATE = ya estaba · ERROR = no encontrada ·
  WARNING = fallo inesperado.

Autenticación (v2, para el despliegue en Render):
- En LOCAL funciona igual que siempre (SpotifyOAuth: abre el navegador la
  primera vez y luego usa el .cache).
- En un SERVIDOR no hay navegador: el error "Server listening on localhost
  has not been accessed" de spotipy es exactamente eso. La solución es
  definir SPOTIFY_REFRESH_TOKEN en el .env/entorno: el bot renueva el
  access token solo (sin navegador ni .cache). Ese refresh token se genera
  UNA VEZ en tu PC con el script obtener_refresh_token.py y no caduca
  mientras no revoques el acceso a la app.
- Ojo: esto NO requiere cuenta Premium. Buscar canciones y añadirlas a
  playlists funciona con cuenta gratuita; el error anterior era del
  navegador ausente, no del tipo de cuenta.

Mejoras respecto al original (sin cambiar el comportamiento visible):
- Las llamadas a Spotify ya no bloquean el event loop del bot
  (asyncio.to_thread), así los comandos de Aternos no se tarden.
- Si alguien escribe un comando (phos!estado, etc.) en el canal de
  canciones, se ignora como canción.
- Los emojis y constantes se leen al cargar el cog, no al importar.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional

import discord
import requests
import spotipy
from discord.ext import commands
from dotenv import load_dotenv
from spotipy.cache_handler import MemoryCacheHandler
from spotipy.oauth2 import SpotifyOAuth

log = logging.getLogger('phos.spotify')

SCOPES = 'playlist-modify-public playlist-modify-private'


def _emoji(nombre_env: str) -> discord.PartialEmoji:
    return discord.PartialEmoji(name='e', id=int(os.environ[nombre_env]))


class _AuthPorRefreshToken:
    """Auth manager de Spotify que parte de un REFRESH TOKEN.

    Pensado para el servidor (Render): no hay navegador ni archivo .cache
    ahí, así que el flujo interactivo de spotipy no puede completarse. Con
    un refresh token (generado una vez en local con
    obtener_refresh_token.py) este manager renueva el access token
    automáticamente cada hora, sin interacción.

    Implementa la interfaz que spotipy espera de un auth_manager
    (get_access_token) y la que usa este cog.
    """

    def __init__(self, client_id: str, client_secret: str,
                 refresh_token: str) -> None:
        # SpotifyOAuth "mudo": se usa solo por su método refresh_access_token
        # (cabeceras y endpoint correctos). Sin navegador y sin .cache en
        # disco (MemoryCacheHandler).
        self._oauth = SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=os.environ.get(
                'SPOTIPY_REDIRECT_URI', 'http://127.0.0.1:8888/callback'),
            scope=SCOPES,
            open_browser=False,
            cache_handler=MemoryCacheHandler(),
        )
        self._refresh = refresh_token
        self._token: Optional[str] = None
        self._expira = 0.0

    def _renovar(self) -> None:
        info = self._oauth.refresh_access_token(self._refresh)
        # Spotify puede devolver un refresh_token nuevo (rotación): adoptarlo.
        if info.get('refresh_token'):
            self._refresh = info['refresh_token']
        self._token = info['access_token']
        self._expira = time.time() + int(info.get('expires_in', 3600))
        log.info('Access token de Spotify renovado (válido %s s).',
                 info.get('expires_in', 3600))

    def get_access_token(self, as_dict: bool = True):
        """Token vigente; lo renueva 60 s antes de caducar."""
        if self._token is None or time.time() > self._expira - 60:
            self._renovar()
        if as_dict:
            return {
                'access_token': self._token,
                'token_type': 'Bearer',
                'expires_at': int(self._expira),
                'scope': SCOPES,
                'refresh_token': self._refresh,
            }
        return self._token


class SpotifyCog(commands.Cog, name='Spotify'):
    """Añade canciones a la playlist comunal escribiendo en un canal."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        load_dotenv()

        if not os.environ.get('SPOTIPY_CLIENT_ID'):
            raise RuntimeError(
                'Faltan las variables SPOTIPY_* en el .env '
                '(revisa la sección de Spotify en .env.example).'
            )

        self.channel_id = int(os.environ['CHANNEL_ID'])
        self.playlist_id = os.environ['PLAYLIST_ID']

        self.emoji_error = _emoji('EMOJI_ERROR_ID')
        self.emoji_duplicada = _emoji('EMOJI_DUPLICATE_ID')
        self.emoji_exito = _emoji('EMOJI_SUCCESS_ID')
        self.emoji_aviso = _emoji('EMOJI_WARNING_ID')

        refresh_token = os.environ.get('SPOTIFY_REFRESH_TOKEN') or None
        if refresh_token:
            # Modo servidor (Render): sin navegador, sin .cache.
            self.auth: Any = _AuthPorRefreshToken(
                os.environ['SPOTIPY_CLIENT_ID'],
                os.environ.get('SPOTIPY_CLIENT_SECRET', ''),
                refresh_token,
            )
            modo = 'refresh token (modo servidor, sin navegador)'
        else:
            # Modo local (comportamiento original): navegador la primera
            # vez y luego el archivo .cache.
            self.auth = SpotifyOAuth(scope=SCOPES)
            modo = 'OAuth interactivo (modo local, usa .cache)'
        self.sp = spotipy.Spotify(auth_manager=self.auth)
        # URIs añadidas desde que el bot está encendido (como en el original;
        # se pierde al reiniciar el bot)
        self.ya_añadidas: set[str] = set()

        log.info(
            'Módulo Spotify listo (canal %s, playlist %s, %s). '
            'Formato requerido: "Nombre - Autor".',
            self.channel_id, self.playlist_id, modo,
        )

    # ------------------------------------------------------------------
    # Lógica de Spotify (idéntica a la original)
    # ------------------------------------------------------------------

    def _buscar_cancion(self, texto: str) -> Optional[dict[str, Any]]:
        """Acepta el formato "Nombre-Autor". Si no encuentra nada,
        reintenta con el texto completo."""
        resultados = []
        if '-' in texto:
            nombre, autor = [p.strip() for p in texto.rsplit('-', 1)]
            resultados = self.sp.search(
                q=f'track:{nombre} artist:{autor}', type='track', limit=5,
            )['tracks']['items']
        if not resultados:
            resultados = self.sp.search(
                q=texto.replace('-', ' '), type='track', limit=5,
            )['tracks']['items']
        return resultados[0] if resultados else None

    def _añadir_a_playlist(self, track_uri: str) -> None:
        token = self.auth.get_access_token(as_dict=False)
        r = requests.post(
            f'https://api.spotify.com/v1/playlists/{self.playlist_id}/items',
            headers={'Authorization': f'Bearer {token}'},
            json={'uris': [track_uri]},
            timeout=10,
        )
        r.raise_for_status()

    # ------------------------------------------------------------------
    # Listener
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.channel.id != self.channel_id:
            return

        # Si el mensaje es un comando del bot (phos!estado, etc.),
        # no lo tratamos como una canción
        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return

        texto = message.content.strip()
        if not texto:
            return

        # Estructura OBLIGATORIA: "Nombre - Autor" (un guion separando dos
        # textos no vacíos). Cualquier otra cosa que se escriba en el canal
        # (saludos, una canción a secas, un enlace...) se ignora en
        # silencio: no se busca en Spotify ni se reacciona.
        if '-' not in texto:
            log.debug('Ignorado (sin guion "Nombre - Autor"): %r', texto[:60])
            return
        nombre, _, autor = texto.rpartition('-')
        if not nombre.strip() or not autor.strip():
            log.debug('Ignorado (falta el nombre o el autor): %r', texto[:60])
            return

        try:
            track = await asyncio.to_thread(self._buscar_cancion, texto)
            if not track:
                await message.add_reaction(self.emoji_error)
                await message.reply('No pude encontrarla. Prueba con el formato Nombre-Autor')
                return

            if track['uri'] in self.ya_añadidas:
                await message.add_reaction(self.emoji_duplicada)
                await message.reply('Esa ya está en la playlist')
                return

            await asyncio.to_thread(self._añadir_a_playlist, track['uri'])
            self.ya_añadidas.add(track['uri'])
            await message.add_reaction(self.emoji_exito)
            await message.reply(
                f"Recién añadida: **{track['name']}** — {track['artists'][0]['name']}"
            )
        except Exception as err:  # mismo comportamiento que el original
            log.exception('Error inesperado procesando canción: %s', err)
            await message.add_reaction(self.emoji_aviso)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SpotifyCog(bot))