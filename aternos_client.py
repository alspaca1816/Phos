"""
aternos_client.py — Cliente minimalista para la API NO OFICIAL de Aternos.

Basado en la ingeniería inversa del panel actual de Aternos (nov-2026),
validada EN VIVO el 2026-11-23: arranque y apagado reales de un servidor
con este protocolo. Referencias: python-aternos (DarkCat09), aternos-api
(sleeyax) y el bundle server-*.js del propio panel.

PROTOCOLO ACTUAL (v4) — difiere del que usaba python-aternos (2024):

  Acciones (todas GET, params en la URL):
    /ajax/server/start           access-credits, [acceptEula], [dismissUpdate], TOKEN, SEC, SERVER
    /ajax/server/stop            TOKEN, SEC, SERVER
    /ajax/server/restart         TOKEN, SEC, SERVER
    /ajax/server/confirm-queue   TOKEN, SEC, SERVER   (antes "confirm")
    /ajax/server/cancel-pending  TOKEN, SEC, SERVER   (antes "cancel")

  - SERVER (id del servidor) es un PARÁMETRO OBLIGATORIO NUEVO: sin él la
    API responde 401 {"error":"Access denied."} aunque la sesión sea válida.
    Era exactamente la causa del 401 histórico de este bot.
  - El parámetro "headstart" dejó de existir; ahora es "access-credits".
  - La EULA ya no tiene endpoint propio: se reintenta start con
    acceptEula=true. Lo mismo el update: dismissUpdate=true.
  - SEC: clave y valor de 16 chars [A-Za-z0-9], GENERADO FRESCO EN CADA
    petición (igual que hace el panel con su clase AjaxToken), junto con
    su cookie ATERNOS_SEC_<clave>=<valor>.
  - El panel actual NO envía X-Requested-With; sí Referer /server.
  - Respuestas: {"success": bool, "error": str|null, "data": {...}|null};
    data.status puede ser: inline, already, eula, update, credits,
    currently, prepared, file, size, wrongversion.
  - Lectura de estado: HTML de /server (var lastStatus, estados numéricos
    0/1/2/3/6/7/10) — sin cambios respecto a versiones anteriores.

Sesión (heredado de v3): Aternos puede ROTAR la cookie ATERNOS_SESSION vía
Set-Cookie en cualquier respuesta; este cliente adopta automáticamente el
valor más reciente y envía SIEMPRE exactamente una cookie de sesión.

ADVERTENCIA (ToS de Aternos §5.2e): el uso de herramientas automatizadas
viene los Términos de Servicio de Aternos y, en teoría, puede suponer el
baneo de tu cuenta. Haz copias de seguridad del mundo con regularidad y
no abuses de la frecuencia de consultas (esto ya lo tiene en cuenta el cog:
la presencia usa solo ping de Minecraft, no la API).

Uso básico::

    from aternos_client import AternosClient

    cliente = AternosClient(session_cookie='VALOR_DE_LA_COOKIE').connect()
    print(cliente.get_status())   # dict con todo el estado del servidor
    cliente.start()               # enciende el servidor
    cliente.stop()                # lo apaga
    cliente.restart()             # lo reinicia
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import shutil
import string
import subprocess
import time
from typing import Any, Dict, List, Optional

import requests
import cloudscraper

log = logging.getLogger('phos.aternos')

BASE_URL = 'https://aternos.org'
AJAX_URL = f'{BASE_URL}/ajax'
SERVER_API = f'{AJAX_URL}/server'

NAVEGADOR_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
)

# Función flecha ofuscada de la página que calcula window.AJAX_TOKEN
RE_ARROW_FN = re.compile(r'\(\(\).*?\)\(\);', re.S)
# lastStatus incrustado en el HTML de https://aternos.org/server
RE_LAST_STATUS = re.compile(
    r'var\s+lastStatus\s*=\s*(\{.*?\})\s*;?\s*</script>', re.S
)
# Por si algún día el token aparece en claro
RE_TOKEN_DIRECTO = re.compile(
    r'''['"]?AJAX_TOKEN['"]?\s*[:=]\s*['"]([A-Za-z0-9_-]{16,})['"]'''
)
# Tarjetas de servidores en https://aternos.org/servers/
RE_SERVER_CARD = re.compile(r'<div[^>]*class="server-body"[^>]*>', re.I)
RE_DATA_ID = re.compile(r'data-id="([^"]+)"')

# Estados numéricos que devuelve lastStatus (mapeo oficial del panel actual,
# extraído del bundle panel-server-*.js: OFFLINE=0, ONLINE=1, STARTING=2,
# STOPPING=3, RESTARTING=4, SAVING=5, LOADING=6, CRASHED=7, PENDING=8,
# TRANSFERRING=9, WAITING=10)
ESTADOS: Dict[int, str] = {
    0: 'apagado',
    1: 'encendido',
    2: 'iniciando (en cola)',
    3: 'apagándose',
    4: 'reiniciando',
    5: 'guardando el mundo',
    6: 'cargando mundo',
    7: 'crasheado (error)',
    8: 'pendiente de cola',
    9: 'transfiriendo',
    10: 'esperando confirmación',
}

# data.status que puede devolver la API ajax actual (bundle server-*.js)
ESTADOS_AJAX: Dict[str, str] = {
    'already': 'El servidor ya está encendido o en proceso de arranque.',
    'credits': 'Aternos pide créditos de acceso (se consiguen viendo anuncios '
               'en el panel). Inténtalo de nuevo en unos minutos.',
    'currently': 'Hay otra operación en curso sobre el servidor; espera a que termine.',
    'queue': 'El servidor entró en cola de arranque; inténtalo de nuevo en unos minutos.',
    'file': 'El almacenamiento de Aternos no está disponible ahora mismo '
            '(consulta status.aternos.gmbh).',
    'size': 'El mundo supera el tamaño permitido para arrancar.',
    'wrongversion': 'La versión del software del servidor quedó obsoleta; '
                    'actualízala desde el panel de Aternos.',
    'prepared': 'El servidor quedó listo de un intento anterior; prueba de nuevo.',
}

# Script de Node.js: recibe el HTML por stdin, ejecuta la función ofuscada
# con un objeto `window` falso y escribe el AJAX_TOKEN en stdout.
_NODO_TOKEN_JS = r"""
let trozos = [];
process.stdin.setEncoding('utf8');
process.stdin.on('data', c => trozos.push(c));
process.stdin.on('end', () => {
    const html = trozos.join('');
    const fns = html.match(/\(\(\)[\s\S]*?\)\(\);/g) || [];
    const ventana = {
        document: { getElementById: () => null },
        setTimeout: () => {}, setInterval: () => {},
        clearTimeout: () => {}, clearInterval: () => {},
        Map: function () {}
    };
    ventana.Map.prototype = {};
    for (const codigo of fns) {
        try {
            const fn = new Function(
                'window',
                '"use strict"; ' + codigo + ' return window["AJAX_TOKEN"];'
            );
            const t = fn(ventana);
            if (t) { process.stdout.write(String(t)); return; }
        } catch (e) { /* probar la siguiente coincidencia */ }
    }
    process.exit(1);
});
"""


class AternosError(Exception):
    """Error genérico al hablar con Aternos."""


class AternosAuthError(AternosError):
    """La sesión de Aternos es inválida o ha expirado."""


class AternosCloudflareError(AternosError):
    """Cloudflare bloqueó las peticiones."""


class AternosClient:
    """Cliente sincrónico de la API privada de Aternos.

    Args:
        session_cookie: valor de la cookie ATERNOS_SESSION (recomendado,
            se copia del navegador con F12).
        usuario / password: credenciales de Aternos (alternativa; puede
            toparse con el captcha de Cloudflare y no soporta 2FA).
        server_id: ID del servidor (si se omite se usa el primero
            de la lista de tu cuenta).
    """

    def __init__(
        self,
        session_cookie: Optional[str] = None,
        usuario: Optional[str] = None,
        password: Optional[str] = None,
        server_id: Optional[str] = None,
    ) -> None:
        self.session_cookie = session_cookie or None
        self.usuario = usuario or None
        self.password = password or None
        self.server_id = server_id or None

        self.token: Optional[str] = None   # AJAX_TOKEN
        self.sec: Optional[str] = None     # token SEC "clave:valor" (fresco por petición)
        self.scraper = cloudscraper.CloudScraper()
        self.scraper.headers.update({'User-Agent': NAVEGADOR_UA})

    # ------------------------------------------------------------------
    # Conexión inicial
    # ------------------------------------------------------------------

    def connect(self) -> 'AternosClient':
        """Prepara tokens y sesión. Debe llamarse una vez antes de usar la API.

        Raises:
            AternosAuthError: si no hay cookie/credenciales válidos.
            AternosCloudflareError: si Cloudflare bloquea el acceso.
        """
        # 1) AJAX_TOKEN (viene ofuscado en la página de login)
        pagina = self._peticion(f'{BASE_URL}/go/')
        self.token = self._extraer_token(pagina.text)
        if not self.token:
            raise AternosError(
                'No se pudo extraer el AJAX_TOKEN de aternos.org. '
                'Instala Node.js (https://nodejs.org) o usa Python 3.10/3.11.'
            )
        log.info('AJAX_TOKEN obtenido correctamente.')

        # 2) Sesión: cookie directa o login con credenciales
        if not self.session_cookie and self.usuario and self.password:
            self._iniciar_sesion()
        if not self.session_cookie:
            raise AternosAuthError(
                'No hay sesión de Aternos. Copia el valor de la cookie '
                'ATERNOS_SESSION en tu archivo .env (ver README).'
            )

        # 3) ID del servidor: el primero de la cuenta si no se indicó
        #    (obligatorio: la API actual lo exige como parámetro SERVER)
        if not self.server_id:
            servidores = self.listar_servidores()
            if not servidores:
                raise AternosError('La cuenta de Aternos no tiene servidores.')
            self.server_id = servidores[0]['id']
            log.info('Servidor detectado automáticamente: %s', self.server_id)

        # 4) Verificación completa (y cache de la primera lectura)
        self.get_status()
        return self

    # ------------------------------------------------------------------
    # Consulta de estado
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """Devuelve el dict `lastStatus` del panel de Aternos.

        Claves habituales: status, class, lang, ip, port, motd, software,
        version, players, playerlist, slots, ram, countdown.
        `status` es un número (ver ESTADOS).
        """
        pagina = self._peticion(f'{BASE_URL}/server', cookie_server=True)
        m = RE_LAST_STATUS.search(pagina.text)
        if not m:
            raise AternosAuthError(
                'No se pudo leer el estado del panel: sesión expirada '
                'o Cloudflare bloqueó la página. Renueva la cookie '
                'ATERNOS_SESSION (ver README).'
            )
        try:
            info = json.loads(m.group(1))
        except json.JSONDecodeError as err:
            raise AternosError(f'Respuesta inesperada de Aternos: {err}') from err
        info['estado_texto'] = ESTADOS.get(int(info.get('status', -1)), 'desconocido')
        return info

    def listar_servidores(self) -> List[Dict[str, str]]:
        """Devuelve la lista de servidores de la cuenta: [{'id': '...'}]."""
        pagina = self._peticion(f'{BASE_URL}/servers/')
        html = pagina.text
        if 'server-body' not in html:
            raise AternosAuthError(
                'No se pudo leer la lista de servidores: sesión inválida '
                'o expirada. Renueva la cookie ATERNOS_SESSION.'
            )
        ids: List[Dict[str, str]] = []
        for tarjeta in RE_SERVER_CARD.findall(html):
            m = RE_DATA_ID.search(tarjeta)
            if m:
                ids.append({'id': m.group(1)})
        return ids

    # ------------------------------------------------------------------
    # Acciones
    # ------------------------------------------------------------------

    def start(
        self,
        access_credits: bool = False,
        accept_eula: bool = False,
        dismiss_update: bool = False,
        _intento: int = 0,
    ) -> bool:
        """Enciende el servidor. Devuelve True si Aternos aceptó la petición.

        Si Aternos pide aceptar la EULA (data.status == "eula") se reintenta
        automáticamente con acceptEula=true; si pide actualizar el software
        ("update") se reintenta con dismissUpdate=true (arrancar sin actualizar,
        igual que pulsar "No" en el panel).
        """
        params: Dict[str, Any] = {
            'access-credits': 'true' if access_credits else 'false',
        }
        if accept_eula:
            params['acceptEula'] = 'true'
        if dismiss_update:
            params['dismissUpdate'] = 'true'

        r = self._peticion(
            f'{SERVER_API}/start',
            params=params,
            enviar_token=True,
            cookie_server=True,
        )
        datos = self._json(r)
        if datos.get('success'):
            estado = ((datos.get('data') or {}).get('status')) or ''
            if estado:
                log.info('Aternos aceptó el arranque (estado: %s).', estado)
            return True

        estado = ((datos.get('data') or {}).get('status')) or ''
        error = str(datos.get('error') or '').strip()

        # Flujos que el panel resuelve reintentando start con más campos
        if estado == 'eula' and not accept_eula:
            log.info('Aternos pide aceptar la EULA; reintentando con acceptEula.')
            return self.start(access_credits=access_credits, accept_eula=True,
                              _intento=_intento + 1)
        if estado == 'update' and not dismiss_update and _intento < 2:
            log.info('Aternos pide confirmar una actualización de software; '
                     'reintentando con dismissUpdate (arrancar sin actualizar).')
            return self.start(access_credits=access_credits, dismiss_update=True,
                              _intento=_intento + 1)

        if estado in ESTADOS_AJAX:
            raise AternosError(ESTADOS_AJAX[estado])
        if error:
            raise AternosError(f'Aternos rechazó el arranque: {error}')
        raise AternosError(f'Aternos rechazó el arranque: {datos}')

    def stop(self) -> bool:
        """Apaga el servidor."""
        r = self._peticion(f'{SERVER_API}/stop', enviar_token=True, cookie_server=True)
        datos = self._json(r)
        if not datos.get('success'):
            estado = ((datos.get('data') or {}).get('status')) or ''
            error = str(datos.get('error') or '').strip()
            if estado in ESTADOS_AJAX:
                raise AternosError(ESTADOS_AJAX[estado])
            if error:
                raise AternosError(f'Aternos rechazó el apagado: {error}')
        return True

    def restart(self) -> bool:
        """Reinicia el servidor."""
        r = self._peticion(f'{SERVER_API}/restart', enviar_token=True, cookie_server=True)
        datos = self._json(r)
        if not datos.get('success'):
            estado = ((datos.get('data') or {}).get('status')) or ''
            error = str(datos.get('error') or '').strip()
            if estado in ESTADOS_AJAX:
                raise AternosError(ESTADOS_AJAX[estado])
            if error:
                raise AternosError(f'Aternos rechazó el reinicio: {error}')
        return True

    def confirm(self) -> None:
        """Confirma el arranque en cola (el "¿Sigues ahí?" del panel).

        Usa el endpoint actual server/confirm-queue (antes server/confirm).
        """
        self._peticion(f'{SERVER_API}/confirm-queue', enviar_token=True,
                       cookie_server=True)

    def cancel(self) -> None:
        """Cancela un arranque en proceso (endpoint actual: cancel-pending)."""
        self._peticion(f'{SERVER_API}/cancel-pending', enviar_token=True,
                       cookie_server=True)

    # ------------------------------------------------------------------
    # Internos
    # ------------------------------------------------------------------

    def _armonizar_sesion(self) -> None:
        """Sincroniza la cookie ATERNOS_SESSION con el jar del scraper.

        Aternos puede rotar la cookie de sesión con un Set-Cookie en
        cualquier respuesta (el valor nuevo cae en el jar). Aquí adoptamos
        el valor más reciente como sesión actual y borramos TODAS las
        copias del jar: así cada petición envía exactamente UNA cookie
        ATERNOS_SESSION (la más reciente), que es lo que hace python-aternos
        (su "CookieConflictError bugfix").
        """
        copias = [c for c in self.scraper.cookies if c.name == 'ATERNOS_SESSION']
        if not copias:
            return  # el servidor no rotó nada; se usa self.session_cookie
        con_dominio = [c for c in copias if c.domain]
        actual = (con_dominio or copias)[-1].value
        if actual:
            if actual != self.session_cookie:
                log.info(
                    'Cookie ATERNOS_SESSION actualizada por el servidor '
                    '(rotación adoptada automáticamente).'
                )
            self.session_cookie = actual
        for c in copias:
            try:
                self.scraper.cookies.clear(c.domain, c.path, c.name)
            except KeyError:
                pass

    def _peticion(
        self,
        url: str,
        metodo: str = 'GET',
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        enviar_token: bool = False,
        cookie_server: bool = False,
        reintentos: int = 3,
    ) -> requests.Response:
        """Petición HTTP a aternos.org renovando la sesión de cloudscraper
        en cada intento (técnica que usa python-aternos para pasar Cloudflare).

        Cuando `enviar_token` es True se añaden los parámetros del protocolo
        ajax ACTUAL: TOKEN, SEC (fresco, con su cookie) y SERVER (id del
        servidor; obligatorio desde 2024-2025 — sin él Aternos responde
        401 "Access denied.").

        - Envía siempre UNA sola cookie ATERNOS_SESSION (la más reciente).
        - Si Aternos responde 401 con token, renueva TOKEN/SEC y reintenta.
        - Mapea los códigos de Aternos (401/412/418...) a errores amables.
        """
        params = dict(params or {})
        data = dict(data or {})
        headers: Dict[str, str] = {}
        cookies: Dict[str, str] = {}

        if enviar_token:
            if not self.token:
                raise AternosError('No hay TOKEN: llama a connect() primero.')
            # Cabeceras que manda el navegador actual al lanzar el ajax
            # desde la página del panel (fetch same-origin desde /server).
            # Nota: el panel actual NO envía X-Requested-With.
            headers['Referer'] = f'{BASE_URL}/server'
            headers['Accept'] = '*/*'
        if self.session_cookie:
            cookies['ATERNOS_SESSION'] = self.session_cookie
        if cookie_server:
            if not self.server_id:
                raise AternosError('Falta el ID del servidor de Aternos.')
            cookies['ATERNOS_SERVER'] = self.server_id

        r: Optional[requests.Response] = None
        token_renovado = False
        for _ in range(reintentos + 1):
            # La sesión vive en el jar del scraper (el servidor puede rotarla);
            # adoptamos su valor más reciente y enviamos solo esa.
            self._armonizar_sesion()
            if self.session_cookie:
                cookies['ATERNOS_SESSION'] = self.session_cookie  # por si rotó

            if enviar_token:
                # SEC fresco en CADA petición, como hace el panel (clase
                # AjaxToken: nueva clave:valor + cookie por cada envío).
                self._generar_sec()
                params['TOKEN'] = self.token
                params['SEC'] = self.sec
                # Parámetro SERVER: obligatorio en la API actual.
                if self.server_id:
                    params['SERVER'] = self.server_id

            self._renovar_scraper()
            try:
                if metodo.upper() == 'POST':
                    r = self.scraper.post(
                        url, params=params, data=data,
                        headers=headers, cookies=cookies, timeout=15,
                    )
                else:
                    r = self.scraper.get(
                        url, params={**params, **data},
                        headers=headers, cookies=cookies, timeout=15,
                    )
            except requests.RequestException as err:
                raise AternosError(f'Error de red hablando con aternos.org: {err}') from err

            tipo = r.headers.get('content-type', '')

            # Cloudflare bloqueó (403 con HTML): reintentar con scraper nuevo
            if r.status_code == 403 and 'text/html' in tipo:
                log.debug('Cloudflare bloqueó la petición, reintentando...')
                time.sleep(0.5)
                continue

            # Token caducado (401): renovar TOKEN/SEC y reintentar UNA vez.
            if r.status_code == 401 and enviar_token and not token_renovado:
                log.warning(
                    '401 en %s — cuerpo: %s. Renovando AJAX_TOKEN y SEC...',
                    url, self._cuerpo_error(r),
                )
                try:
                    self._renovar_token()
                except AternosError as err:
                    log.warning('No se pudo renovar el AJAX_TOKEN: %s', err)
                    break  # procesar el 401 como un error normal, abajo
                token_renovado = True
                continue

            break

        if r is None or (
            r.status_code == 403
            and 'text/html' in r.headers.get('content-type', '')
        ):
            raise AternosCloudflareError(
                'Cloudflare bloqueó las peticiones a aternos.org. '
                'Espera unos minutos o ejecuta el bot desde otra red/IP.'
            )
        if r.status_code == 402:
            raise AternosError('Permiso denegado (HTTP 402).')

        # Errores 4xx/5xx: leer el cuerpo (Aternos explica el motivo en JSON)
        # y mapear los códigos conocidos a mensajes accionables.
        if r.status_code >= 400:
            cuerpo = self._cuerpo_error(r)
            log.error('HTTP %s de %s — cuerpo: %s', r.status_code, url, cuerpo)
            error = ''
            try:
                err_json = r.json()
                if isinstance(err_json, dict):
                    error = str(err_json.get('error') or '').strip()
            except ValueError:
                pass
            error_l = error.lower()

            if r.status_code == 401 and 'access denied' in error_l:
                raise AternosAuthError(
                    'Aternos respondió "Access denied." (401): la sesión no '
                    'fue aceptada para esta acción. Ya se envían TOKEN, SEC y '
                    'SERVER correctos, así que lo habitual es que la cookie '
                    'ATERNOS_SESSION haya caducado o haya sido revocada al '
                    'iniciar sesión en otro navegador. Copia de nuevo la '
                    'cookie desde el navegador (F12 → Application → Cookies '
                    '→ aternos.org) al .env y reinicia el bot.'
                )
            if r.status_code in (401, 412, 418) or any(
                clave in error_l for clave in ('session', 'expired', 'token', 'auth')
            ):
                causa = {
                    412: 'la cookie ATERNOS_SEC no llegó al servidor',
                    418: 'la sesión no es válida para Aternos',
                }.get(r.status_code, 'la sesión o el token no son válidos')
                raise AternosAuthError(
                    f'Aternos rechazó la petición (HTTP {r.status_code}, '
                    f'{causa}{f": {error}" if error else ""}). Renueva la '
                    'cookie ATERNOS_SESSION en el .env (F12 → Application → '
                    'Cookies → aternos.org) y reinicia el bot.'
                )
            if error:
                raise AternosError(f'Aternos respondió {r.status_code}: {error}')
            try:
                r.raise_for_status()
            except requests.HTTPError as err:
                # Nunca dejar escapar un HTTPError crudo: el cog no lo captura.
                raise AternosError(f'HTTP {r.status_code} de Aternos: {err}') from err
        return r

    def _renovar_scraper(self) -> None:
        """Crea un CloudScraper nuevo conservando las cookies (bypass Cloudflare)."""
        viejo = self.scraper
        self.scraper = cloudscraper.CloudScraper()
        self.scraper.headers.update({'User-Agent': NAVEGADOR_UA})
        for galleta in viejo.cookies:
            self.scraper.cookies.set_cookie(galleta)

    def _renovar_token(self) -> None:
        """Re-extrae el AJAX_TOKEN desde páginas frescas.

        Se prefiere la página del panel (/server, cargada con sesión y
        servidor): es el mismo contexto desde el que el navegador dispara
        el arranque, así que su token es el correcto para las peticiones
        ajax. Si ahí no hay token, se usa /go/ (como python-aternos).
        """
        token = None
        try:
            pagina = self._peticion(f'{BASE_URL}/server', cookie_server=True)
            token = self._extraer_token(pagina.text)
            if token:
                log.info('AJAX_TOKEN renovado desde la página del panel (/server).')
        except AternosError as err:
            log.debug('No se pudo leer /server para renovar el token: %s', err)
        if not token:
            pagina = self._peticion(f'{BASE_URL}/go/')
            token = self._extraer_token(pagina.text)
        if not token:
            raise AternosError(
                'No se pudo re-extraer el AJAX_TOKEN de aternos.org.'
            )
        self.token = token
        log.info('AJAX_TOKEN renovado.')

    @staticmethod
    def _cuerpo_error(r: requests.Response) -> str:
        """Cuerpo (recortado) de una respuesta de error, para el log."""
        try:
            return json.dumps(r.json(), ensure_ascii=False)[:300]
        except ValueError:
            return (r.text or '')[:300]

    def _json(self, r: requests.Response) -> Dict[str, Any]:
        if 'json' not in r.headers.get('content-type', ''):
            raise AternosCloudflareError(
                'Aternos devolvió HTML en lugar de JSON (Cloudflare o sesión caída).'
            )
        try:
            return r.json()
        except ValueError as err:
            raise AternosError(f'Respuesta no válida de Aternos: {err}') from err

    def _iniciar_sesion(self) -> None:
        """Login alternativo con usuario/contraseña (MD5, como pide Aternos)."""
        md5 = hashlib.md5(self.password.encode('utf-8')).hexdigest()  # noqa: S324
        r = self._peticion(
            f'{AJAX_URL}/account/login',
            metodo='POST',
            data={'username': self.usuario, 'password': md5},
            enviar_token=True,
        )
        nueva = self.scraper.cookies.get('ATERNOS_SESSION')
        if not nueva:
            raise AternosAuthError(
                'Aternos no devolvió sesión: credenciales incorrectas, '
                '2FA activo o captcha de Cloudflare. Usa la cookie '
                'ATERNOS_SESSION (ver README).'
            )
        self.session_cookie = nueva
        log.info('Sesión de Aternos iniciada con usuario y contraseña.')

    def _extraer_token(self, html: str) -> Optional[str]:
        """Extrae el AJAX_TOKEN: primero en claro (regex), luego Node.js,
        luego js2py (Python 3.10/3.11)."""
        m = RE_TOKEN_DIRECTO.search(html)
        if m:
            return m.group(1)

        funciones = RE_ARROW_FN.findall(html)

        # Opción 1: Node.js (funciona en cualquier versión de Python)
        if funciones and shutil.which('node'):
            try:
                p = subprocess.run(
                    ['node', '-e', _NODO_TOKEN_JS],
                    input=html, capture_output=True, text=True, timeout=25,
                )
                token = p.stdout.strip()
                if p.returncode == 0 and len(token) >= 10:
                    log.info('AJAX_TOKEN extraído con Node.js.')
                    return token
                log.debug('Node.js no pudo extraer el token: %s', p.stderr[:200])
            except (OSError, subprocess.SubprocessError) as err:
                log.debug('Fallo al ejecutar Node.js: %s', err)

        # Opción 2: js2py (roto en Python 3.12+)
        if funciones:
            try:
                import js2py  # type: ignore
            except Exception:
                js2py = None  # type: ignore
            if js2py is not None:
                for codigo in funciones:
                    try:
                        ctx = js2py.EvalJs()
                        resultado = ctx.eval(
                            'var window = {document:{getElementById:function(){return null}},'
                            'setTimeout:function(){},setInterval:function(){},'
                            'clearTimeout:function(){},clearInterval:function(){},'
                            'Map:function(){}}; window.Map.prototype = {};'
                            + codigo + ' window["AJAX_TOKEN"];'
                        )
                        if resultado:
                            return str(resultado)
                    except Exception:
                        continue
        return None

    def _generar_sec(self) -> str:
        """Genera un token SEC fresco y su cookie ATERNOS_SEC_<clave>.

        Réplica de la clase AjaxToken del panel actual (nov-2026): clave y
        valor de 16 caracteres aleatorios [A-Za-z0-9] (getString(16)), y la
        cookie se crea nueva para CADA petición. Las cookies SEC anteriores
        se eliminan del jar para no acumular basura (el navegador las guarda
        separadas por path; a nosotros solo nos vale la vigente).
        """
        alfabeto = string.ascii_letters + string.digits

        def parte() -> str:
            return ''.join(secrets.choice(alfabeto) for _ in range(16))

        # limpiar SEC anteriores del jar
        for galleta in [c for c in self.scraper.cookies
                        if c.name.startswith('ATERNOS_SEC_')]:
            try:
                self.scraper.cookies.clear(galleta.domain, galleta.path, galleta.name)
            except KeyError:
                pass

        clave, valor = parte(), parte()
        self.sec = f'{clave}:{valor}'
        self.scraper.cookies.set(f'ATERNOS_SEC_{clave}', valor, domain='.aternos.org')
        return self.sec