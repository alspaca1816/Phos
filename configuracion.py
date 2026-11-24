"""
configuracion.py — Configuración centralizada de Phos por variables de entorno.

Toda la configuración de Phos vive en variables de entorno:

- En desarrollo: se cargan de un archivo .env en la raíz del proyecto
  (python-dotenv). Nunca se sobrescriben variables que ya existan en el
  entorno: en producción (Render) las del panel SIEMPRE ganan.
- En producción (Render): las define el panel del servicio.

Este módulo centraliza:

- la carga del .env (``cargar_entorno``);
- la lectura limpia de variables (``valor``): una cadena vacía o con
  solo espacios cuenta como ausente, nunca como configuración válida;
- la validación al arrancar (``validar``): una obligatoria que falta
  lanza ``ErrorConfiguracion`` con un mensaje comprensible; las
  opcionales que faltan producen avisos que indican qué función se
  pierde;
- un resumen seguro para los registros (``resumen``): nombres y
  presencia, jamás valores.

Ninguna credencial se escribe en el código ni en los registros.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple


class ErrorConfiguracion(RuntimeError):
    """Falta o es inválida una variable de entorno obligatoria."""


# ---------------------------------------------------------------------------
# Catálogo de variables
# ---------------------------------------------------------------------------

# Sin estas el bot no puede arrancar. (nombre → para qué sirve)
OBLIGATORIAS: Dict[str, str] = {
    'DISCORD_TOKEN': 'token del bot (Discord Developer Portal → tu app → Bot → Reset Token)',
}

# Módulos opcionales: (nombre del grupo → [(variables…), …]).
# Dentro de cada tupla los nombres son ALTERNATIVAS: basta con que exista
# una (así se aceptan los alias históricos del .env sin duplicar nada).
GRUPOS_OPCIONALES: Dict[str, List[Tuple[str, ...]]] = {
    'Spotify (añadir canciones escribiendo "Nombre - Autor" en el canal)': [
        ('SPOTIPY_CLIENT_ID',),
        ('SPOTIPY_CLIENT_SECRET',),
        ('SPOTIPY_REDIRECT_URI',),
        ('CHANNEL_ID',),
        ('PLAYLIST_ID',),
        ('EMOJI_SUCCESS_ID',),
        ('EMOJI_DUPLICATE_ID',),
        ('EMOJI_ERROR_ID',),
        ('EMOJI_WARNING_ID',),
    ],
    'Aternos (encender/apagar/reiniciar y /estado)': [
        ('ATERNOS_SESSION',),
    ],
    'Servidor concreto de Aternos (si no, se usa el primero de la cuenta)': [
        ('ATERNOS_SERVER_ID', 'ATERNOS_SERVER'),
    ],
    'Presencia automática con ping de Minecraft': [
        ('MC_ADDRESS', 'MC_ADRESS'),
    ],
}

# Variables que, si están definidas, deben ser números enteros (IDs de
# Discord o puertos). PLAYLIST_ID NO está: los IDs de playlist de Spotify
# son cadenas tipo "37i9dQZF1E…" (base62).
ENTERAS: Tuple[str, ...] = (
    'CHANNEL_ID', 'GUILD_ID',
    'EMOJI_SUCCESS_ID', 'EMOJI_DUPLICATE_ID', 'EMOJI_ERROR_ID',
    'EMOJI_WARNING_ID',
    'PORT',
)

# Variables de solo desarrollo local (Render las ignora o las define él).
SOLO_LOCAL: Tuple[str, ...] = ('PORT',)


# ---------------------------------------------------------------------------
# Carga y lectura
# ---------------------------------------------------------------------------

def cargar_entorno(ruta_env: Optional[str] = None) -> None:
    """Carga el .env local SIN sobrescribir nada del entorno real.

    python-dotenv, con ``override=False``, deja intactas las variables que
    ya existan: así la configuración del panel de Render siempre gana a la
    del archivo y el mismo código sirve en ambos sitios.
    """
    from dotenv import load_dotenv
    load_dotenv(ruta_env, override=False)


def valor(nombre: str) -> Optional[str]:
    """Valor limpio de una variable: None si falta, está vacía o son espacios."""
    crudo = os.getenv(nombre)
    if crudo is None:
        return None
    crudo = crudo.strip()
    return crudo or None


def enmascarar(texto: str) -> str:
    """Representación segura de un valor para registros: nunca revela
    contenido (ni un prefijo)."""
    return f'•••• ({len(texto)} caracteres)'


# ---------------------------------------------------------------------------
# Validación
# ---------------------------------------------------------------------------

def validar() -> List[str]:
    """Valida la configuración de Phos.

    Devuelve una lista de AVISOS (problemas no bloqueantes, listados para
    el log). Lanza :class:`ErrorConfiguracion` si falta alguna variable
    obligatoria. Los mensajes nunca incluyen valores de variables.
    """
    avisos: List[str] = []

    # 1) Obligatorias → error duro
    faltan = [nombre for nombre in OBLIGATORIAS if valor(nombre) is None]
    if faltan:
        detalles = ', '.join(
            f'{nombre} ({desc})' for nombre, desc in OBLIGATORIAS.items()
            if nombre in faltan
        )
        raise ErrorConfiguracion(
            f'Configuración incompleta: falta(n) {len(faltan)} variable(s) '
            f'obligatoria(s): {detalles}. Define la(s) variable(s) en tu '
            'archivo .env (copia .env.example) o, en Render, en Environment '
            'del servicio. El bot no puede arrancar sin ella(s).'
        )

    # 2) Grupos opcionales → avisos
    for grupo, alternativas in GRUPOS_OPCIONALES.items():
        faltantes = [nombres[0] for nombres in alternativas
                     if not any(valor(n) for n in nombres)]
        if faltantes:
            avisos.append(
                f'[aviso] {grupo}: falta(n) {", ".join(faltantes)}. '
                'Esa función quedará desactivada o dará error al usarse.'
            )

    # 3) Formato de las numéricas → avisos
    for nombre in ENTERAS:
        v = valor(nombre)
        if v is not None and not v.lstrip('-').isdigit():
            avisos.append(
                f'[aviso] {nombre} debería ser un número entero y no lo es; '
                'revísala en el .env o en el panel de Render.'
            )

    return avisos


def resumen() -> List[str]:
    """Líneas de resumen de la configuración, SIN valores (solo presencia).

    Útil al arrancar para detectar typos en los nombres de las variables.
    """
    lineas: List[str] = []
    for nombre in OBLIGATORIAS:
        v = valor(nombre)
        estado = f'definida ({enmascarar(v)})' if v else 'NO definida'
        lineas.append(f'  {nombre:<22} {estado}')
    vistas = set(OBLIGATORIAS)
    for alternativas in GRUPOS_OPCIONALES.values():
        for nombres in alternativas:
            for nombre in nombres:
                if nombre in vistas:
                    continue
                vistas.add(nombre)
                v = valor(nombre)
                lineas.append(
                    f'  {nombre:<22} '
                    + (f'definida ({enmascarar(v)})' if v else 'no definida')
                )
    return lineas