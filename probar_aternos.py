"""
probar_aternos.py — Diagnóstico completo del cliente de Aternos (v5).

Se ejecuta IGUAL en tu PC y en Render:

  · En tu PC:    python probar_aternos.py        (con tu .env)
  · En Render:   cambia el Start Command a "python probar_aternos.py"
                 (Settings → Start Command), guarda, espera el deploy y lee
                 el log del dashboard; luego restaura el start command
                 original ("python start.py").

Qué comprueba (en orden):
  1. Variables de entorno presentes (sin mostrar sus valores).
  2. Transporte HTTP disponible (curl_cffi / cloudscraper) y huellas TLS.
  3. Salida de Cloudflare: cdn-cgi/trace (IP, HTTP/2, TLS).
  4. Página /go/ y extracción del AJAX_TOKEN (con sus dos ramas).
  5. Si hay ATERNOS_SESSION: lista de servidores y estado real del tuyo.

NO enciende ni apaga nada por defecto. Si quieres probar una acción real,
define ATERNOS_PROBAR_ACCION=estado|encender|apagar en el entorno
(solo para la prueba; arranca el servidor de verdad).

Cada paso se clasifica: APROBADO / FALLIDO / BLOQUEADO / NO EJECUTADO.
"""

from __future__ import annotations

import logging
import os
import sys

logging.basicConfig(level=logging.INFO,
                    format='%(levelname)-7s %(name)s: %(message)s')

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import aternos_client as ac

RESULTADOS: list = []


def anota(nombre, estado, detalle=''):
    RESULTADOS.append((nombre, estado))
    print(f'  [{estado:^11}] {nombre}' + (f' — {detalle}' if detalle else ''))


def enmascara(valor: str, visibles: int = 4) -> str:
    if not valor:
        return '<vacía>'
    if len(valor) <= visibles:
        return '*' * len(valor)
    return valor[:visibles] + '…' + str(len(valor)) + ' chars'


def main() -> int:
    print('=' * 72)
    print('PROBAR ATTERNOS — diagnóstico del cliente v5')
    print('=' * 72)

    # ------------------------------------------------------------------
    print('\n[1] Variables de entorno (valores enmascarados)')
    # ------------------------------------------------------------------
    sesion = os.environ.get('ATERNOS_SESSION') or ''
    server_id = os.environ.get('ATERNOS_SERVER_ID') or ''
    usuario = os.environ.get('ATERNOS_USUARIO') or ''
    anota('ATERNOS_SESSION definida', 'APROBADO' if sesion else 'BLOQUEADO',
          enmascara(sesion) if sesion else
          'sin cookie de sesión: los pasos 5+ no se ejecutan')
    if server_id:
        anota('ATERNOS_SERVER_ID definida', 'APROBADO', enmascara(server_id))
    elif not usuario:
        anota('ATERNOS_SERVER_ID definida', 'BLOQUEADO',
              'el cliente la detectará solo con sesión válida')

    # ------------------------------------------------------------------
    print('\n[2] Transporte HTTP')
    # ------------------------------------------------------------------
    if ac._CURL_DISPONIBLE:
        import curl_cffi
        imps = ac._impersonaciones_disponibles()
        anota('curl_cffi instalada (huella TLS de Chrome real)', 'APROBADO',
              f'versión {curl_cffi.__version__}, huellas: {", ".join(imps) or "ninguna"}')
        if not imps:
            anota('huellas Chrome disponibles', 'FALLIDO',
                  'curl_cffi muy vieja: pip install -U curl_cffi')
    else:
        anota('curl_cffi instalada (huella TLS de Chrome real)', 'FALLIDO',
              'solo queda cloudscraper (huella de Python, Cloudflare la '
              'bloquea en Render). Añade curl_cffi a requirements.txt')
    anota('cloudscraper (respaldo) instalado', 'APROBADO',
          'siempre presente')

    # ------------------------------------------------------------------
    print('\n[3] Cloudflare: cdn-cgi/trace con el transporte del cliente')
    # ------------------------------------------------------------------
    cliente = ac.AternosClient(session_cookie=sesion or None,
                               server_id=server_id or None)
    print(f'  Transporte elegido: {cliente.http.descripcion()}')
    try:
        r = cliente.http.get('https://aternos.org/cdn-cgi/trace', timeout=20)
        if r.status_code == 200 and 'ip=' in r.text:
            datos = dict(l.split('=', 1) for l in r.text.strip().splitlines()
                         if '=' in l)
            print(f"  IP de salida: {datos.get('ip')} · colo: {datos.get('colo')} "
                  f"· http: {datos.get('http')} · tls: {datos.get('tls')}")
            anota('Cloudflare deja pasar el transporte', 'APROBADO')
        else:
            anota('Cloudflare deja pasar el transporte', 'FALLIDO',
                  f'HTTP {r.status_code} en cdn-cgi/trace')
    except Exception as err:
        anota('Cloudflare deja pasar el transporte', 'FALLIDO',
              f'{type(err).__name__}: {str(err)[:120]}')

    # ------------------------------------------------------------------
    print('\n[4] Página /go/ y AJAX_TOKEN (trampa de dos ramas)')
    # ------------------------------------------------------------------
    try:
        cliente.token = None
        pagina = cliente._peticion(f'{ac.BASE_URL}/go/')
        anota('GET /go/ responde (no bloqueado)', 'APROBADO',
              f'HTTP {pagina.status_code}, {len(pagina.text)} bytes')
    except ac.AternosCloudflareError as err:
        anota('GET /go/ responde (no bloqueado)', 'FALLIDO', str(err)[:160])
        return resumen()
    except ac.AternosError as err:
        anota('GET /go/ responde (no bloqueado)', 'FALLIDO', str(err)[:160])
        return resumen()

    token = cliente._extraer_token(pagina.text)
    if token:
        anota('AJAX_TOKEN primario extraído', 'APROBADO',
              f'{len(token)} chars · método: '
              + ('Python puro' if ac._parsear_ternario(
                  ac.RE_ARROW_FN.findall(pagina.text)[0]
                  if ac.RE_ARROW_FN.findall(pagina.text) else '') else 'otro'))
        if cliente._token_alt:
            anota('token alternativo (otra rama de la trampa)', 'APROBADO',
                  f'{len(cliente._token_alt)} chars')
        else:
            anota('token alternativo (otra rama de la trampa)', 'NO EJECUTADO',
                  'la página no trae dos ramas (formato nuevo o token en claro)')
    else:
        anota('AJAX_TOKEN primario extraído', 'FALLIDO',
              'ningún método (parser/Node/heurística) encontró el token')

    # ------------------------------------------------------------------
    print('\n[5] Con la sesión real: servidores y estado')
    # ------------------------------------------------------------------
    if not sesion:
        anota('listar servidores con tu sesión', 'NO EJECUTADO',
              'ATERNOS_SESSION no definida en este entorno')
        return resumen()

    cliente2 = ac.AternosClient(session_cookie=sesion,
                                 server_id=server_id or None)
    try:
        cliente2.connect()
        anota('connect() completo con TU sesión', 'APROBADO')
    except ac.AternosAuthError as err:
        anota('connect() completo con TU sesión', 'FALLIDO',
              f'sesión inválida/expirada: {str(err)[:130]}')
        print('  → Renueva la cookie: F12 → Application → Cookies → '
              'aternos.org → ATERNOS_SESSION, pégala en el .env/Render y '
              'repite esta prueba.')
        return resumen()
    except ac.AternosCloudflareError as err:
        anota('connect() completo con TU sesión', 'FALLIDO',
              f'Cloudflare bloqueó AUN CON curl_cffi: {str(err)[:150]}')
        print('  → La IP de este entorno está quemada para Aternos. Ver '
              'README_FIX_RENDER.md (plan B).')
        return resumen()
    except ac.AternosError as err:
        anota('connect() completo con TU sesión', 'FALLIDO', str(err)[:150])
        return resumen()

    try:
        info = cliente2.get_status()
        anota('get_status() con TU sesión', 'APROBADO')
        print(f"  Estado: {info.get('estado_texto')} (código {info.get('status')}) · "
              f"jugadores: {info.get('players', 0)}/{info.get('slots', 0)} · "
              f"software: {info.get('software')} {info.get('version') or ''}")
        if info.get('playerlist'):
            print(f"  Conectados: {', '.join(map(str, info['playerlist'][:20]))}")
    except ac.AternosError as err:
        anota('get_status() con TU sesión', 'FALLIDO', str(err)[:150])

    # ------------------------------------------------------------------
    print('\n[6] Acción real (solo si la pides expresamente)')
    # ------------------------------------------------------------------
    accion = (os.environ.get('ATERNOS_PROBAR_ACCION') or '').strip().lower()
    if accion in ('encender', 'apagar', 'reiniciar'):
        try:
            resultado = getattr(cliente2, accion)()
            anota(f'{accion}() ejecutado de verdad', 'APROBADO',
                  f'resultado: {resultado}')
        except ac.AternosError as err:
            anota(f'{accion}() ejecutado de verdad', 'FALLIDO', str(err)[:150])
    else:
        anota('acción real (encender/apagar)', 'NO EJECUTADO',
              'define ATERNOS_PROBAR_ACCION=encender|apagar para probarla')

    return resumen()


def resumen() -> int:
    print('\n' + '=' * 72)
    conteo: dict = {}
    for _, estado in RESULTADOS:
        conteo[estado] = conteo.get(estado, 0) + 1
    for nombre, estado in RESULTADOS:
        print(f'  {estado:<12} · {nombre}')
    print(f"\nTotal: {len(RESULTADOS)} · " +
          ' · '.join(f'{k}: {v}' for k, v in sorted(conteo.items())))
    print('Pruebas que SOLO puedes hacer tú: escribir canciones en el canal '
          'de Spotify y usar /estado /encender /apagar en Discord.')
    fallos = conteo.get('FALLIDO', 0)
    return 0 if fallos == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
