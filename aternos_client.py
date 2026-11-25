"""
aternos_client.py — Cliente minimalista para la API NO OFICIAL de Aternos.
Versión 5 (adaptación a Render, 2026-11-24).

Basado en la ingeniería inversa del panel actual de Aternos (nov-2026),
validada EN VIVO el 2026-11-23: arranque y apagado reales de un servidor
con este protocolo. Referencias: python-aternos (DarkCat09), aternos-api
(sleeyax) y el bundle server-*.js del propio panel.

PROTOCOLO ACTUAL (v4, SIN CAMBIOS en esta versión) — difiere del que usaba
python-aternos (2024):

  Acciones (todas GET, params en la URL):
    /ajax/server/start           access-credits, [acceptEula], [dismissUpdate], TOKEN, SEC, SERVER
    /ajax/server/stop            TOKEN, SEC, SERVER
    /ajax/server/restart         TOKEN, SEC, SERVER
    /ajax/server/confirm-queue   TOKEN, SEC, SERVER   (antes "confirm")
    /ajax/server/cancel-pending  TOKEN, SEC, SERVER   (antes "cancel")

  - SERVER (id del servidor) es un PARÁMETRO OBLIGATORIO: sin él la API
    responde 401 {"error":"Access denied."} aunque la sesión sea válida.
  - El parámetro "headstart" dejó de existir; ahora es "access-credits".
  - La EULA ya no tiene endpoint propio: se reintenta start con
    acceptEula=true. Lo mismo el update: dismissUpdate=true.
  - SEC: clave y valor de 16 chars [A-Za-z0-9], GENERADO FRESCO EN CADA
    petición, junto con su cookie ATERNOS_SEC_<clave>=<valor>.
  - El panel actual NO envía X-Requested-With; sí Referer /server.
  - Respuestas: {"success": bool, "error": str|null, "data": {...}|null}.
  - Lectura de estado: HTML de /server (var lastStatus, estados numéricos
    0/1/2/3/6/7/10).

Sesión (heredado de v3): Aternos puede ROTAR la cookie ATERNOS_SESSION vía
Set-Cookie en cualquier respuesta; este cliente adopta automáticamente el
valor más reciente y envía SIEMPRE exactamente una cookie de sesión.

QUÉ CAMBIA EN LA v5 (solo el transporte, no el protocolo):

  1. Transporte principal: curl_cffi con huella TLS/HTTP2 de Chrome REAL
     ("impersonación"). Motivo: en hosts como Render (IPs compartidas de
     datacenter) Cloudflare puntúa la petición por (IP + huella TLS +
     cabeceras); con requests/cloudscraper la huella TLS es la de Python
     (HTTP/1.1, kex antiguo) y Cloudflare responde 403 "Just a moment..."
     aunque las cabeceras parezcan de navegador. curl_cffi negocia TLS 1.3
     y HTTP/2 exactamente igual que un Chrome de escritorio.
     Verificado el 2026-11-24 contra aternos.org: las huellas Chrome 136+ y
     el alias "chrome" pasan; las antiguas (chrome124, chrome131) reciben
     reto incluso desde IPs limpias. La rotación de reintento es:
     chrome → chrome145 → chrome142 → cloudscraper.
  2. cloudscraper queda como RESPALDO automático si curl_cffi no está
     instalada (comportamiento idéntico a la v4, que funcionó en local).
  3. AJAX_TOKEN extraíble en PYTHON PURO (la v4 necesitaba Node.js, que no
     está garantizado en el entorno Python de Render). El token viene en una
     función ofuscada con TRAMPA ANTI-BOT de dos ramas (una para navegadores
     reales y otra para entornos incompletos); se parsean AMBAS y, si la API
     rechaza la primera (401), se reintenta automáticamente con la otra.
     Node.js y js2py siguen disponibles como respaldo/verificación.
  4. Errores de Cloudflare con detalle real (código HTTP, tipo de reto) y
     candado interno para llamadas desde varios hilos del bot.

ADVERTENCIA (ToS de Aternos §5.2e): el uso de herramientas automatizadas
viene los Términos de Servicio de Aternos y, en teoría, puede suponer el
baneo de tu cuenta. Haz copias de seguridad del mundo con regularidad y
no abuses de la frecuencia de consultas (esto ya lo tiene en cuenta el cog:
la presencia usa solo ping de Minecraft, no la API).

Uso básico (idéntico a v4)::

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
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import cloudscraper
import requests

# curl_cffi es el transporte recomendado; si no está, se cae a cloudscraper
# (comportamiento v4) y se deja constancia en el log.
try:  # pragma: no cover - depende del entorno
    from curl_cffi import requests as _creq

    _CURL_DISPONIBLE = True
except ImportError:  # pragma: no cover
    _creq = None  # type: ignore[assignment]
    _CURL_DISPONIBLE = False

log = logging.getLogger('phos.aternos')

BASE_URL = 'https://aternos.org'
AJAX_URL = f'{BASE_URL}/ajax'
SERVER_API = f'{AJAX_URL}/server'

NAVEGADOR_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
)

# Huellas Chrome a probar, en orden. Solo MODERNAS: el Cloudflare de Aternos
# bloquea expresamente las huellas antiguas típicas de scrapers (verificado
# el 2026-11-24: chrome124/chrome131 → 403; chrome136+ → 200). El alias
# "chrome" siempre apunta a la última versión incluida en curl_cffi.
_IMPERSONACIONES: Tuple[str, ...] = ('chrome', 'chrome145', 'chrome142')

# Función flecha ofuscada de la página que calcula window.AJAX_TOKEN
RE_ARROW_FN = re.compile(r'\(\(\)[\s\S]*?\)\(\);')
# lastStatus incrustado en el HTML de https://aternos.org/server
RE_LAST_STATUS = re.compile(
    r'var\s+lastStatus\s*=\s*(\{.*?\})\s*;?\s*</script>', re.S
)
# Por si algún día el token aparece en claro
RE_TOKEN_DIRECTO = re.compile(
    r'''['"]?AJAX_TOKEN['"]?\s*[:=]\s*['"]([A-Za-z0-9_-]{16,})['"]'''
)
# Literales de cadena dobles (para extraer el token ofuscado en Python puro)
RE_LITERAL = re.compile(r'"((?:[^"\\\n]|\\.)*)"')
# Concatenaciones de literales: "a" + "b" + "c"
RE_CONCAT = re.compile(r'(?:"(?:[^"\\\n]|\\.)*"\s*\+\s*){1,}"(?:[^"\\\n]|\\.)*"')
# Forma del AJAX_TOKEN
RE_TOKEN_FORMA = re.compile(r'[A-Za-z0-9_-]{16,}')
# Idiomas de las ramas del ternario anti-bot (ver 24-nov-2026):
# ["a","b"].map(s => s.split('').reverse().join('')).join('')
RE_RAMA_MAP = re.compile(r'\s*\[\s*(?:"[^"]*"\s*,?\s*)+\]\s*\.map\(')
# ["a","b"].reverse().join('')
RE_RAMA_REV_JOIN = re.compile(r'\s*\[\s*(?:"[^"]*"\s*,?\s*)+\]\s*\.reverse\(\)\s*\.join\(')
# ["a","b"].join('')
RE_RAMA_JOIN = re.compile(r'\s*\[\s*(?:"[^"]*"\s*,?\s*)+\]\s*\.join\(')
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


# ---------------------------------------------------------------------------
# Parser del ternario anti-bot del AJAX_TOKEN (Python puro, sin Node.js)
# ---------------------------------------------------------------------------

def _vista_codigo(texto: str) -> str:
    """Copia del texto con el CONTENIDO de los literales sustituido por
    espacios (las comillas se conservan): los operadores quedan visibles
    aunque las cadenas contengan paréntesis o signos de puntuación."""
    chars = list(texto)
    en_cadena = None
    escape = False
    for k, ch in enumerate(texto):
        if en_cadena:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == en_cadena:
                en_cadena = None
            else:
                chars[k] = ' '
            continue
        if ch in '"\'':
            en_cadena = ch
    return ''.join(chars)


def _valor_rama(fragmento: str) -> Optional[str]:
    """Valor de cadena de una rama del ternario (idiomas vistos en producción).

    - ("a" + "b" + "c") o "literal" desnudo
    - ["a","b"].join('')
    - ["a","b"].reverse().join('')      (se invierte el ORDEN)
    - ["a","b"].map(s => s.split('').reverse().join('')).join('')  (se
      invierte CADA literal, el orden se mantiene)
    """
    m = RE_RAMA_MAP.match(fragmento)
    if m:
        lits = re.findall(r'"([^"]*)"', m.group(0))
        return ''.join(l[::-1] for l in lits)
    m = RE_RAMA_REV_JOIN.match(fragmento)
    if m:
        lits = re.findall(r'"([^"]*)"', m.group(0))
        return ''.join(reversed(lits))
    m = RE_RAMA_JOIN.match(fragmento)
    if m:
        lits = re.findall(r'"([^"]*)"', m.group(0))
        return ''.join(lits)
    lits = RE_LITERAL.findall(fragmento)
    if lits:
        return ''.join(lits)
    return None


def _parsear_ternario(fn: str) -> Optional[Tuple[str, str]]:
    """Extrae (primario, alternativo) del ternario anti-bot, o None.

    La función ofuscada de Aternos tiene esta forma (ejemplo real):

      window["AJAX_TOKEN"] = !(<comprobaciones del navegador>)
          ? ("DoCJGTSv" + "xb" + "FVeRBtgvfS")   ← rama de entorno INCOMPLETO
          : ("q0Y" + "6ApEBcfz" + "3VT" + "U1y" + "WAX");  ← rama navegador

    - PRIMARIO = rama que toma un entorno SIN navegador (condición FALSA),
      exactamente el valor que extraía la v4 con Node.js y una window falsa
      (validado EN VIVO el 23-11-2026 con arranque/apagado reales). Con la
      condición negada (!) esa rama es la primera; sin negar, la segunda.
    - ALTERNATIVO = la otra rama (la que computa un navegador real).

    La función además lleva un COMENTARIO señuelo con valores anticuados:
    se descarta antes de parsear. Idiomas y estructura verificados contra
    páginas reales del 23-11 y del 24-11-2026 (7/7 muestras = Node.js).
    """
    cuerpo = re.sub(r'/\*.*?\*/', '', fn, count=1, flags=re.S)
    vista = _vista_codigo(cuerpo)

    # --- window[ <corchetes equilibrados> ] = ---
    m = re.search(r'window\[', vista)
    if not m:
        return None
    j = m.end() - 1
    prof = 0
    fin_llave = None
    for k in range(j, len(vista)):
        if vista[k] == '[':
            prof += 1
        elif vista[k] == ']':
            prof -= 1
            if prof == 0:
                fin_llave = k
                break
    if fin_llave is None:
        return None
    resto = vista[fin_llave + 1:].lstrip()
    if not resto.startswith('='):
        return None
    pos_cond = len(cuerpo) - len(resto) + 1
    if resto[1:2] == '!':
        negado = True
        pos_cond += 1
    else:
        negado = False

    def buscar(caracter: str, desde: int) -> int:
        """Índice del primer `caracter` fuera de cadenas y a profundidad 0."""
        prof = 0
        for k in range(desde, len(vista)):
            ch = vista[k]
            if ch in '([{':
                prof += 1
            elif ch in ')]}':
                prof -= 1
            elif ch == caracter and prof == 0:
                return k
        return -1

    pos_interrogacion = buscar('?', pos_cond)
    if pos_interrogacion < 0:
        return None
    pos_dospuntos = buscar(':', pos_interrogacion + 1)
    if pos_dospuntos < 0:
        return None
    pos_fin = buscar(';', pos_dospuntos + 1)
    if pos_fin < 0:
        pos_fin = len(cuerpo)

    rama1 = cuerpo[pos_interrogacion + 1: pos_dospuntos]
    rama2 = cuerpo[pos_dospuntos + 1: pos_fin]
    v1, v2 = _valor_rama(rama1), _valor_rama(rama2)
    if not v1 or not v2:
        return None
    if not (RE_TOKEN_FORMA.fullmatch(v1) and RE_TOKEN_FORMA.fullmatch(v2)):
        return None
    return (v1, v2) if negado else (v2, v1)


# ---------------------------------------------------------------------------
# Fábricas de sesiones (se parchean en las pruebas)
# ---------------------------------------------------------------------------

def _nueva_sesion_curl(impersonacion: str) -> Any:
    """Sesión curl_cffi con la huella TLS/HTTP2 indicada."""
    return _creq.Session(impersonate=impersonacion)  # type: ignore[union-attr]


def _nueva_sesion_cloudscraper() -> Any:
    """Sesión cloudscraper (comportamiento v4)."""
    return cloudscraper.CloudScraper()


_CACHE_IMPS: Optional[List[str]] = None


def _impersonaciones_disponibles() -> List[str]:
    """Qué huellas de la lista están soportadas por la curl_cffi instalada."""
    global _CACHE_IMPS
    if _CACHE_IMPS is not None:
        return _CACHE_IMPS
    if not _CURL_DISPONIBLE:
        _CACHE_IMPS = []
        return _CACHE_IMPS
    validas: List[str] = []
    for imp in _IMPERSONACIONES:
        try:
            ses = _nueva_sesion_curl(imp)
            try:
                ses.close()
            except Exception:
                pass
            validas.append(imp)
        except Exception:
            continue
    _CACHE_IMPS = validas
    return validas


class _Transporte:
    """Sesión HTTP contra aternos.org con la API de cookies unificada.

    Modo 'curl'      → curl_cffi con huella de Chrome real (recomendado; es
                       lo que pasa la puntuación de Cloudflare en IPs de
                       datacenter como las de Render). No se toca el
                       User-Agent: la impersonación ya envía el de Chrome
                       con sus cabeceras coherentes (sec-ch-ua, etc.).
    Modo 'cloudscraper' → como la v4 (Chrome/120 + UA propio), usado cuando
                       curl_cffi no está instalada.

    Ambos modos exponen el mismo manejo de cookies para que la lógica del
    cliente (rotación de sesión, cookies SEC) no dependa del transporte.
    """

    def __init__(self, tipo: str = 'curl', impersonacion: str = 'chrome') -> None:
        self.tipo = tipo
        self.impersonacion = impersonacion
        if tipo == 'curl' and _CURL_DISPONIBLE:
            self._ses = _nueva_sesion_curl(impersonacion)
        else:
            self.tipo = 'cloudscraper'
            self._ses = _nueva_sesion_cloudscraper()
            self._ses.headers.update({'User-Agent': NAVEGADOR_UA})

    # ---------------- peticiones ----------------

    def get(self, url: str, params: Optional[Dict[str, Any]] = None,
            headers: Optional[Dict[str, str]] = None,
            cookies: Optional[Dict[str, str]] = None,
            timeout: float = 15) -> Any:
        try:
            return self._ses.get(url, params=params, headers=headers,
                                 cookies=cookies, timeout=timeout)
        except Exception as err:
            raise AternosError(
                f'Error de red hablando con aternos.org '
                f'({type(err).__name__}): {err}'
            ) from err

    def post(self, url: str, params: Optional[Dict[str, Any]] = None,
             data: Optional[Dict[str, Any]] = None,
             headers: Optional[Dict[str, str]] = None,
             cookies: Optional[Dict[str, str]] = None,
             timeout: float = 15) -> Any:
        try:
            return self._ses.post(url, params=params, data=data, headers=headers,
                                  cookies=cookies, timeout=timeout)
        except Exception as err:
            raise AternosError(
                f'Error de red hablando con aternos.org '
                f'({type(err).__name__}): {err}'
            ) from err

    # ---------------- cookies ----------------

    def _jar(self) -> Any:
        """Jar iterable con objetos cookie (.name/.value/.domain/.path)."""
        if self.tipo == 'curl':
            return self._ses.cookies.jar
        return self._ses.cookies

    def cookies(self) -> List[Any]:
        """Fotografía del jar de cookies actual."""
        try:
            return list(self._jar())
        except Exception:
            return []

    def valor_cookie(self, nombre: str) -> Optional[str]:
        """Valor más reciente de una cookie por nombre (o None)."""
        valores = [c.value for c in self.cookies()
                   if c.name == nombre and c.value]
        return valores[-1] if valores else None

    def fijar_cookie(self, nombre: str, valor: str,
                     dominio: str = '.aternos.org') -> None:
        try:
            self._ses.cookies.set(nombre, valor, domain=dominio, path='/')
        except TypeError:  # firmas sin `path`
            self._ses.cookies.set(nombre, valor, domain=dominio)

    def borrar_cookie(self, galleta: Any) -> None:
        self._jar().clear(galleta.domain, galleta.path, galleta.name)

    # ---------------- ciclo de vida ----------------

    def renovar(self) -> '_Transporte':
        """Sesión NUEVA del mismo tipo conservando las cookies (técnica v4)."""
        galletas = self.cookies()
        nueva = _Transporte(tipo=self.tipo, impersonacion=self.impersonacion)
        for g in galletas:
            nueva.fijar_cookie(g.name, g.value, dominio=g.domain or '.aternos.org')
        return nueva

    def descripcion(self) -> str:
        if self.tipo == 'curl':
            return (f'curl_cffi con huella TLS/HTTP2 de Chrome real '
                    f'("{self.impersonacion}")')
        return ('cloudscraper (curl_cffi NO instalada: ejecuta '
                '"pip install curl_cffi")')


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

        self.token: Optional[str] = None   # AJAX_TOKEN en uso
        self._token_alt: Optional[str] = None  # la otra rama de la trampa
        self._preferir_alternativo = False    # True si la API aceptó la rama alt
        self.sec: Optional[str] = None     # token SEC "clave:valor" (fresco por petición)
        self._sec_cookie: Tuple[str, str] = ('', '')

        # Candado reentrante: el bot llama a este cliente desde varios
        # hilos (asyncio.to_thread); así las peticiones no se intercalan.
        self._lock = threading.RLock()

        self._impersonaciones = _impersonaciones_disponibles()
        self._imp_activa: Optional[str] = None
        self.http: Optional[_Transporte] = None
        try:
            if self._impersonaciones:
                self._imp_activa = self._impersonaciones[0]
                self.http = _Transporte(tipo='curl',
                                        impersonacion=self._imp_activa)
            else:
                self.http = _Transporte(tipo='cloudscraper')
        except Exception as err:
            log.warning('No se pudo crear el transporte principal (%s); '
                        'se intentará al primer uso.', err)
            self._imp_activa = None
            self.http = _Transporte(tipo='cloudscraper')

    # ------------------------------------------------------------------
    # Conexión inicial
    # ------------------------------------------------------------------

    def connect(self) -> 'AternosClient':
        """Prepara tokens y sesión. Debe llamarse una vez antes de usar la API.

        Raises:
            AternosAuthError: si no hay cookie/credenciales válidos.
            AternosCloudflareError: si Cloudflare bloquea el acceso.
        """
        log.info('Transporte HTTP hacia aternos.org: %s', self.http.descripcion())

        # 1) AJAX_TOKEN (viene ofuscado en la página de login; trampa de
        #    dos ramas — ver _parsear_ternario)
        pagina = self._peticion(f'{BASE_URL}/go/')
        self.token = self._extraer_token(pagina.text)
        if not self.token:
            raise AternosError(
                'No se pudo extraer el AJAX_TOKEN de aternos.org. '
                'Instala Node.js (https://nodejs.org) o actualiza el bot.'
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
    # Internos: sesión y cookies
    # ------------------------------------------------------------------

    def _armonizar_sesion(self) -> None:
        """Sincroniza la cookie ATERNOS_SESSION con el jar del transporte.

        Aternos puede rotar la cookie de sesión con un Set-Cookie en
        cualquier respuesta (el valor nuevo cae en el jar). Aquí adoptamos
        el valor más reciente como sesión actual y borramos TODAS las
        copias del jar: así cada petición envía exactamente UNA cookie
        ATERNOS_SESSION (la más reciente), que es lo que hace python-aternos
        (su "CookieConflictError bugfix").
        """
        if self.http is None:
            return
        copias = [c for c in self.http.cookies() if c.name == 'ATERNOS_SESSION']
        if not copias:
            return  # el servidor no rotó nada; se usa self.session_cookie
        actual = copias[-1].value
        if actual:
            if actual != self.session_cookie:
                log.info(
                    'Cookie ATERNOS_SESSION actualizada por el servidor '
                    '(rotación adoptada automáticamente).'
                )
            self.session_cookie = actual
        for c in copias:
            try:
                self.http.borrar_cookie(c)
            except KeyError:
                pass

    def _generar_sec(self) -> str:
        """Genera un token SEC fresco y su cookie ATERNOS_SEC_<clave>.

        Réplica de la clase AjaxToken del panel actual: clave y valor de 16
        caracteres aleatorios [A-Za-z0-9] (getString(16)), y la cookie se
        crea nueva para CADA petición. En la v5 la cookie SEC viaja en la
        propia petición (no se deja residuo en el jar).
        """
        alfabeto = string.ascii_letters + string.digits

        def parte() -> str:
            return ''.join(secrets.choice(alfabeto) for _ in range(16))

        clave, valor = parte(), parte()
        self.sec = f'{clave}:{valor}'
        self._sec_cookie = (f'ATERNOS_SEC_{clave}', valor)
        return self.sec

    # ------------------------------------------------------------------
    # Internos: transporte y peticiones
    # ------------------------------------------------------------------

    def _plan_intentos(self) -> List[Tuple[str, Optional[str]]]:
        """Orden de transportes a intentar contra Cloudflare.

        Empieza por la impersonación que funcionó la última vez y acaba con
        cloudscraper (comportamiento v4) como último recurso.
        """
        plan: List[Tuple[str, Optional[str]]] = []
        if _CURL_DISPONIBLE and self._impersonaciones:
            imps = list(self._impersonaciones)
            if self._imp_activa in imps:
                imps.remove(self._imp_activa)
                imps.insert(0, self._imp_activa)
            plan += [('curl', i) for i in imps]
        plan.append(('cloudscraper', None))
        return plan

    def _preparar_transporte(self, tipo: str,
                             imp: Optional[str]) -> Optional[_Transporte]:
        """Transporte para un intento, o None si no se puede construir.

        - curl: reutiliza la sesión existente si es la misma huella (un
          navegador de verdad mantiene su sesión; así la conexión HTTP/2 y
          las cookies persisten entre peticiones).
        - cloudscraper: sesión nueva en cada intento conservando cookies
          (técnica de renovación de la v4).
        """
        if tipo == 'curl':
            if not (_CURL_DISPONIBLE and imp):
                return None
            if (self.http is not None and self.http.tipo == 'curl'
                    and self.http.impersonacion == imp):
                return self.http
            try:
                return _Transporte(tipo='curl', impersonacion=imp)
            except Exception as err:
                log.debug('No se pudo crear sesión curl_cffi(%s): %s', imp, err)
                return None
        if self.http is not None and self.http.tipo == 'cloudscraper':
            return self.http.renovar()
        try:
            return _Transporte(tipo='cloudscraper')
        except Exception as err:
            log.debug('No se pudo crear cloudscraper: %s', err)
            return None

    def _peticion(
        self,
        url: str,
        metodo: str = 'GET',
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        enviar_token: bool = False,
        cookie_server: bool = False,
        reintentos: int = 3,
    ) -> Any:
        """Petición HTTP a aternos.org rotando huellas TLS si Cloudflare
        responde 403 (el bloqueo típico en IPs de datacenter como las de
        Render se evita presentando la huella TLS/HTTP2 de un Chrome real).

        Cuando `enviar_token` es True se añaden los parámetros del protocolo
        ajax ACTUAL: TOKEN, SEC (fresco, con su cookie) y SERVER (id del
        servidor; obligatorio — sin él Aternos responde 401 "Access denied.").

        - Envía siempre UNA sola cookie ATERNOS_SESSION (la más reciente).
        - Si Aternos responde 401 con token, renueva TOKEN/SEC y reintenta.
        - Mapea los códigos de Aternos (401/412/418...) a errores amables.
        """
        with self._lock:
            return self._peticion_interna(
                url, metodo, params, data, enviar_token, cookie_server, reintentos,
            )

    def _peticion_interna(
        self,
        url: str,
        metodo: str,
        params: Optional[Dict[str, Any]],
        data: Optional[Dict[str, Any]],
        enviar_token: bool,
        cookie_server: bool,
        reintentos: int,
    ) -> Any:
        params = dict(params or {})
        data = dict(data or {})
        headers: Dict[str, str] = {}
        cookies: Dict[str, str] = {}

        if enviar_token:
            if not self.token:
                raise AternosError('No hay TOKEN: llama a connect() primero.')
            # Cabeceras que manda el navegador real al lanzar el ajax desde
            # la página del panel (fetch same-origin desde /server). El panel
            # actual NO envía X-Requested-With.
            headers['Referer'] = f'{BASE_URL}/server'
            headers['Accept'] = '*/*'
            headers['Sec-Fetch-Site'] = 'same-origin'
            headers['Sec-Fetch-Mode'] = 'cors'
            headers['Sec-Fetch-Dest'] = 'empty'
        if self.session_cookie:
            cookies['ATERNOS_SESSION'] = self.session_cookie
        if cookie_server:
            if not self.server_id:
                raise AternosError('Falta el ID del servidor de Aternos.')
            cookies['ATERNOS_SERVER'] = self.server_id

        r: Optional[Any] = None
        bloqueado_cf = False
        token_renovado = False
        alterno_usado = False
        plan = self._plan_intentos()[: max(1, reintentos + 1)]

        for tipo, imp in plan:
            transporte = self._preparar_transporte(tipo, imp)
            if transporte is None:
                log.debug('Transporte %s(%s) no disponible.', tipo, imp)
                continue
            while True:  # reintentos sin rotar huella (401 → renovar token)
                self.http = transporte
                # La sesión puede rotar en cualquier respuesta: adoptarla y
                # dejar el jar limpio (una sola cookie de sesión).
                self._armonizar_sesion()
                if self.session_cookie:
                    cookies['ATERNOS_SESSION'] = self.session_cookie  # por si rotó

                if enviar_token:
                    # SEC fresco en CADA petición, como hace el panel (clase
                    # AjaxToken: nueva clave:valor + cookie por cada envío).
                    # Primero se retira la SEC del intento anterior para no
                    # acumular cookies (el navegador no acumula las viejas).
                    for k in [k for k in cookies if k.startswith('ATERNOS_SEC_')]:
                        del cookies[k]
                    self._generar_sec()
                    params['TOKEN'] = self.token
                    params['SEC'] = self.sec
                    nombre_sec, valor_sec = self._sec_cookie
                    cookies[nombre_sec] = valor_sec
                    if self.server_id:
                        params['SERVER'] = self.server_id

                try:
                    if metodo.upper() == 'POST':
                        r = transporte.post(
                            url, params=params, data=data,
                            headers=headers, cookies=cookies, timeout=15,
                        )
                    else:
                        r = transporte.get(
                            url, params={**params, **data},
                            headers=headers, cookies=cookies, timeout=15,
                        )
                except AternosError:
                    raise  # error de red ya mapeado (igual que la v4)

                # La respuesta puede rotar la cookie de sesión (Set-Cookie):
                # adoptarla YA (la renovación del token usa transportes
                # internos; sin esto, la rotación se perdería al volver).
                self._armonizar_sesion()
                if self.session_cookie:
                    cookies['ATERNOS_SESSION'] = self.session_cookie

                tipo_ct = r.headers.get('content-type', '')

                # Cloudflare bloqueó (403 con HTML): rotar huella TLS
                if r.status_code == 403 and 'text/html' in tipo_ct:
                    log.debug('Cloudflare bloqueó la petición (HTTP 403) con '
                              '%s(%s); probando otra huella…', tipo, imp or '-')
                    time.sleep(0.75)
                    bloqueado_cf = True
                    break  # → siguiente transporte del plan

                bloqueado_cf = False

                # 401 = sesión o TOKEN rechazados. Recuperación en dos pasos:
                #   1) renovar TOKEN/SEC desde una página fresca y reintentar;
                #   2) si sigue 401 y conocemos la OTRA rama de la trampa
                #      anti-bot, intercambiarlas (la rama válida puede ser la
                #      contraria) y reintentar una última vez.
                if r.status_code == 401 and enviar_token:
                    if not token_renovado:
                        log.warning(
                            '401 en %s — cuerpo: %s. Renovando AJAX_TOKEN y SEC…',
                            url, self._cuerpo_error(r),
                        )
                        try:
                            self._renovar_token()
                        except AternosError as err:
                            log.warning('No se pudo renovar el AJAX_TOKEN: %s', err)
                            break  # procesar el 401 como un error normal, abajo
                        token_renovado = True
                        continue
                    if (self._token_alt and not alterno_usado
                            and self._token_alt != self.token):
                        log.warning(
                            '401 persistente: reintentando con la otra rama de '
                            'la trampa anti-bot del AJAX_TOKEN.'
                        )
                        self.token, self._token_alt = self._token_alt, self.token
                        self._preferir_alternativo = True
                        alterno_usado = True
                        continue
                    if alterno_usado:
                        # la alternativa tampoco funcionó: dejarlo como estaba
                        self._preferir_alternativo = False
                    break  # procesar el 401 como un error normal, abajo
                break  # respuesta utilizable

            if not bloqueado_cf:
                if tipo == 'curl' and imp:
                    self._imp_activa = imp  # esta huella funciona: reusarla
                break  # no hace falta probar más transportes

        if r is None or (
            r.status_code == 403
            and 'text/html' in r.headers.get('content-type', '')
        ):
            detalle = ''
            if r is not None:
                reto = r.headers.get('cf-mitigated', '')
                detalle = f'HTTP {r.status_code}' + (f', reto {reto}' if reto else '')
            raise AternosCloudflareError(
                'Cloudflare bloqueó las peticiones a aternos.org'
                + (f' [{detalle}]' if detalle else '')
                + '. Se probó con huella TLS de Chrome real (curl_cffi) y con '
                'cloudscraper. Si esto ocurre en Render, la IP de salida está '
                'marcada para Aternos: ejecuta probar_aternos.py desde el mismo '
                'entorno para ver el diagnóstico completo (IP, huella y reto).'
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
            raise AternosError(f'HTTP {r.status_code} de Aternos ({cuerpo[:120]}).')
        return r

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
    def _cuerpo_error(r: Any) -> str:
        """Cuerpo (recortado) de una respuesta de error, para el log."""
        try:
            return json.dumps(r.json(), ensure_ascii=False)[:300]
        except ValueError:
            return (r.text or '')[:300]

    def _json(self, r: Any) -> Dict[str, Any]:
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
        nueva = self.http.valor_cookie('ATERNOS_SESSION') if self.http else None
        if not nueva:
            raise AternosAuthError(
                'Aternos no devolvió sesión: credenciales incorrectas, '
                '2FA activo o captcha de Cloudflare. Usa la cookie '
                'ATERNOS_SESSION (ver README).'
            )
        self.session_cookie = nueva
        log.info('Sesión de Aternos iniciada con usuario y contraseña.')

    # ------------------------------------------------------------------
    # Extracción del AJAX_TOKEN
    # ------------------------------------------------------------------

    def _extraer_token(self, html: str) -> Optional[str]:
        """Extrae el AJAX_TOKEN y deja en self._token_alt la otra rama de la
        trampa anti-bot (por si la API prefiere esa).

        Devuelve el token a usar (None si no se pudo extraer). Si ya se
        descubrió antes que la rama válida es la alternativa
        (self._preferir_alternativo), se devuelve esa.
        """
        primario, alternativo = self._extraer_token_completo(html)
        if not primario:
            self._token_alt = None
            return None
        if self._preferir_alternativo and alternativo:
            self._token_alt = primario
            return alternativo
        self._token_alt = alternativo
        return primario

    def _extraer_token_completo(
        self, html: str
    ) -> Tuple[Optional[str], Optional[str]]:
        """Extrae el AJAX_TOKEN. Devuelve (primario, alternativo).

        primario    = rama que computa un entorno SIN navegador (el valor
                      que extraía la v4 con Node.js, validado EN VIVO el
                      23-11-2026 con arranque/apagado reales).
        alternativo = la otra rama (la que computa un navegador real).

        Orden:
        1. En claro (regex) — por si algún día vuelve a aparecer así.
        2. Parser del ternario anti-bot en Python puro (novedad v5: no
           requiere Node.js, clave para el entorno Python de Render).
        3. Node.js ejecutando el ofuscado real (v4); si discrepa del
           parser, manda Node (verificación cruzada).
        4. Heurística de literales únicos (último recurso sin Node).
        5. js2py (Python 3.10/3.11).
        """
        m = RE_TOKEN_DIRECTO.search(html)
        if m:
            log.debug('AJAX_TOKEN encontrado en claro en el HTML.')
            return m.group(1), None

        funciones = RE_ARROW_FN.findall(html)
        if not funciones:
            return None, None

        # 1) Parser del ternario anti-bot (Python puro)
        parseado: Optional[Tuple[str, str]] = None
        for fn in funciones:
            parseado = _parsear_ternario(fn)
            if parseado:
                break

        # 2) Node.js (v4 exacto) — se usa también para verificación cruzada
        node_token = None
        if shutil.which('node'):
            node_token = self._token_con_node(html)

        if parseado and node_token:
            primario, alternativo = parseado
            if node_token == primario:
                log.info('AJAX_TOKEN extraído con Python puro '
                         '(verificado con Node.js).')
                return primario, alternativo
            # Discrepancia: Node ejecuta el JS de verdad → manda Node.
            log.warning('El parser Python y Node.js difirieron; se usa el '
                        'valor de Node.js (ejecución real del JS).')
            alt = alternativo if alternativo != node_token else primario
            return node_token, alt
        if parseado:
            primario, alternativo = parseado
            log.info('AJAX_TOKEN extraído con Python puro (sin Node.js).')
            return primario, alternativo
        if node_token:
            log.info('AJAX_TOKEN extraído con Node.js.')
            return node_token, None

        # 3) Heurística de literales (último recurso sin Node.js)
        heuristico = self._token_heuristico(funciones)
        if heuristico:
            log.info('AJAX_TOKEN extraído por heurística de literales.')
            return heuristico, None

        # 4) js2py (roto en Python 3.12+)
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
                        return str(resultado), None
                except Exception:
                    continue
        return None, None

    def _token_con_node(self, html: str) -> Optional[str]:
        """Ejecuta el ofuscado con Node.js y una window falsa (v4 exacto)."""
        try:
            p = subprocess.run(
                ['node', '-e', _NODO_TOKEN_JS],
                input=html, capture_output=True, text=True, timeout=25,
            )
            if p.returncode == 0 and len(p.stdout.strip()) >= 10:
                return p.stdout.strip()
            log.debug('Node.js no pudo extraer el token: %s', p.stderr[:200])
        except (OSError, subprocess.SubprocessError) as err:
            log.debug('Fallo al ejecutar Node.js: %s', err)
        return None

    @staticmethod
    def _token_heuristico(funciones: List[str]) -> Optional[str]:
        """Último recurso: un único literal (o concatenación) con forma de
        token dentro de la función flecha. Se descartan los literales del
        comentario señuelo cuando hay valor ejecutable."""
        enteros: set = set()
        concatenados: set = set()
        for fn in funciones:
            cuerpo = re.sub(r'/\*.*?\*/', '', fn, count=1, flags=re.S)
            for lit in RE_LITERAL.findall(cuerpo):
                if RE_TOKEN_FORMA.fullmatch(lit):
                    enteros.add(lit)
            for grupo in RE_CONCAT.findall(cuerpo):
                junto = ''.join(RE_LITERAL.findall(grupo))
                if RE_TOKEN_FORMA.fullmatch(junto):
                    concatenados.add(junto)
        if len(enteros) == 1:
            return enteros.pop()
        if not enteros and len(concatenados) == 1:
            return concatenados.pop()
        return None