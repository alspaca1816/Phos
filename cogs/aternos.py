"""
cogs/aternos.py — Cog de Aternos para Phos (discord.py 2.x).

Comandos híbridos (funcionan como slash /comando y como prefijo !comando):

  estado    — estado del servidor + jugadores conectados (nombres in-game).
              La IP solo la ven los roles de "roles_pueden_ver_ip".
  encender  — enciende el servidor (permisos configurables en config_aternos.json).
  apagar    — apaga el servidor (solo roles de "roles_pueden_apagar").
  reiniciar — reinicia el servidor (solo roles de "roles_pueden_reiniciar").

Además, si defines MC_ADDRESS (o MC_ADRESS) en el .env, el bot actualiza su presencia
consultando el servidor con el protocolo de Minecraft (mcstatus), sin tocar
la API de Aternos: menos peticiones automatizadas, menos riesgo. Mientras
hay una operación en curso (encendiendo, apagando, reiniciando), la
presencia muestra el estado de transición correspondiente en vez de un
ping engañoso.

Requiere la variable de entorno DISCORD_TOKEN (la carga el main del bot),
ATERNOS_SESSION y, opcionalmente, MC_ADDRESS (se acepta también el
alias MC_ADRESS) y ATERNOS_SERVER_ID (alias ATERNOS_SERVER).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from aternos_client import AternosAuthError, AternosClient, AternosError

log = logging.getLogger('phos.aternos')


def _var(*nombres: str) -> Optional[str]:
    """Devuelve la primera variable de entorno definida y no vacía de las
dadas, o None. Permite aceptar alias históricos de un mismo ajuste
(p. ej. MC_ADDRESS y MC_ADRESS) sin tocar el resto de la lógica."""
    for nombre in nombres:
        valor = os.getenv(nombre)
        if valor and valor.strip():
            return valor.strip()
    return None


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

# Se busca en la carpeta desde donde se lanza el bot y junto al proyecto.
RUTAS_CONFIG = [
    Path.cwd() / 'config_aternos.json',
    Path(__file__).resolve().parent.parent / 'config_aternos.json',
]

CONFIG_DEFECTO: Dict[str, Any] = {
    # Se puede poner el NOMBRE del rol ("Miembro") o su ID numérico.
    'roles_pueden_encender': ['Azumanguero', 'Moderador', 'Haru_w', 'Poemas_Azumanga', 'Alspaca'],
    'roles_pueden_apagar': ['Moderador', 'Haru_w', 'Poemas_Azumanga', 'Alspaca'],
    'roles_pueden_reiniciar': ['Moderador', 'Haru_w', 'Poemas_Azumanga', 'Alspaca'],
    'roles_pueden_ver_ip': ['Moderador', 'Haru_w', 'Poemas_Azumanga', 'Alspaca'],
    # Cada cuántos minutos se actualiza la presencia del bot.
    'presencia_minutos': 2,
}

TEXTO_ESTADO = {
    0: 'Apagado',
    1: 'En línea',
    2: 'Iniciando (en cola de Aternos)',
    3: 'Apagándose',
    6: 'Cargando mundo',
    7: 'Error',
    10: 'Esperando confirmación',
}
EMOJI_ESTADO = {0: '🔴', 1: '🟢', 2: '🟡', 3: '🟠', 6: '🟡', 7: '⚠️', 10: '🟡'}


class AternosCog(commands.Cog, name='Aternos'):
    """Estado y control del servidor de Minecraft en Aternos."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg: Dict[str, Any] = self._cargar_config()
        self.mc_address: Optional[str] = _var('MC_ADDRESS', 'MC_ADRESS')

        self.cliente: Optional[AternosClient] = None
        self._lock_cliente = asyncio.Lock()
        # Candado de exclusión mutua: solo cubre el envío de la petición
        # a Aternos (se libera en el `finally` del comando).
        self._accion_en_curso = False
        # Etiqueta informativa de la operación que se vigila en segundo
        # plano ('encendiendo' / 'apagando' / 'reiniciando'). La ponen y
        # la limpian las tareas de fondo; la lee el loop de presencia.
        self._accion_actual: Optional[str] = None

    # ------------------------------------------------------------------
    # Ciclo de vida del cog
    # ------------------------------------------------------------------

    async def cog_load(self) -> None:
        load_dotenv()
        self.mc_address = _var('MC_ADDRESS', 'MC_ADRESS')
        self.cfg = self._cargar_config()

        if not self.mc_address:
            log.warning(
                'MC_ADDRESS no está definido en el .env: el ping directo y '
                'la presencia automática quedan desactivados.'
            )
        try:
            await self._obtener_cliente()
        except Exception as err:  # se reintenta al usar cada comando
            log.warning('No se pudo conectar con Aternos al arrancar: %s', err)

        if self.mc_address:
            self.presencia.change_interval(
                minutes=max(1, int(self.cfg.get('presencia_minutos', 2)))
            )
            self.presencia.start()

    async def cog_unload(self) -> None:
        self.presencia.cancel()

    # ------------------------------------------------------------------
    # Configuración y permisos
    # ------------------------------------------------------------------

    @staticmethod
    def _cargar_config() -> Dict[str, Any]:
        cfg = dict(CONFIG_DEFECTO)
        for ruta in RUTAS_CONFIG:
            if ruta.is_file():
                try:
                    cfg.update(json.loads(ruta.read_text(encoding='utf-8')))
                except (OSError, json.JSONDecodeError) as err:
                    log.error('No se pudo leer %s (%s); se usan valores por defecto.', ruta, err)
                return cfg
        # Si no existe, lo creamos para que sea fácil de editar
        ruta = RUTAS_CONFIG[0]
        try:
            ruta.write_text(
                json.dumps(CONFIG_DEFECTO, indent=2, ensure_ascii=False),
                encoding='utf-8',
            )
            log.info('Creado %s con valores por defecto. ¡Edítalo a tu gusto!', ruta)
        except OSError:
            pass
        return cfg

    @staticmethod
    def _rol_autorizado(usuario: Any, lista: List[Any]) -> bool:
        """True si el usuario tiene alguno de los roles de la lista.

        Cada entrada puede ser el nombre del rol, su ID (número) o
        '@everyone' para permitir a todos.
        """
        entradas = {str(x).strip().lower() for x in lista}
        if '@everyone' in entradas or not lista:
            return True
        if not isinstance(usuario, discord.Member):
            return False  # mensajes por MD: sin roles
        for rol in usuario.roles:
            if str(rol.id) in entradas or rol.name.lower() in entradas:
                return True
        return False

    def _puede_ver_ip(self, usuario: Any) -> bool:
        if self._rol_autorizado(usuario, self.cfg.get('roles_pueden_ver_ip', [])):
            return True
        return isinstance(usuario, discord.Member) and usuario.guild_permissions.manage_guild

    # ------------------------------------------------------------------
    # Cliente de Aternos y consulta de estado
    # ------------------------------------------------------------------

    async def _obtener_cliente(self) -> AternosClient:
        """Devuelve el cliente conectado; lo crea/reconecta si hace falta."""
        if self.cliente is not None:
            return self.cliente
        async with self._lock_cliente:
            if self.cliente is not None:
                return self.cliente
            cliente = AternosClient(
                session_cookie=_var('ATERNOS_SESSION'),
                usuario=_var('ATERNOS_USUARIO'),
                password=_var('ATERNOS_PASSWORD'),
                server_id=_var('ATERNOS_SERVER_ID', 'ATERNOS_SERVER'),
            )
            await asyncio.to_thread(cliente.connect)
            self.cliente = cliente
            log.info(
                'Conectado a Aternos (servidor %s).', cliente.server_id,
            )
        return self.cliente

    async def _invalidar_cliente(self) -> None:
        async with self._lock_cliente:
            self.cliente = None

    def _ping_mc(self) -> Any:
        """Ping directo con el protocolo de Minecraft (mcstatus). Bloqueante."""
        from mcstatus import JavaServer
        servidor = JavaServer.lookup(self.mc_address)  # type: ignore[arg-type]
        return servidor.status()

    async def _recolectar_estado(self, usar_api: bool = True) -> Dict[str, Any]:
        """Estado combinado: API de Aternos (rico) con respaldo de ping directo."""
        est: Dict[str, Any] = {
            'estado_num': None,      # None = desconocido; 0 off; 1 on; 2 starting...
            'jugadores': [],         # nombres in-game
            'num_jugadores': 0,
            'slots': 0,
            'ip': None,
            'version': None,
            'software': None,
            'motd': None,
            'countdown': None,       # segundos hasta auto-apagado
            'fuente': 'ninguna',
        }

        # 1) API de Aternos (estado detallado + lista de jugadores)
        if usar_api:
            try:
                cliente = await self._obtener_cliente()
                info = await asyncio.to_thread(cliente.get_status)
                est.update(
                    estado_num=int(info.get('status', -1)),
                    jugadores=[str(j) for j in (info.get('playerlist') or [])],
                    num_jugadores=int(info.get('players', 0) or 0),
                    slots=int(info.get('slots', 0) or 0),
                    ip=f"{info.get('ip', '')}:{info.get('port', '')}",
                    version=info.get('version'),
                    software=info.get('software'),
                    motd=info.get('motd'),
                    countdown=info.get('countdown'),
                    fuente='API de Aternos',
                )
                return est
            except AternosAuthError as err:
                log.warning('Sesión de Aternos inválida: %s', err)
                await self._invalidar_cliente()
            except AternosError as err:
                log.warning('API de Aternos falló: %s', err)

        # 2) Respaldo: ping directo al servidor de Minecraft
        if self.mc_address:
            try:
                st = await asyncio.to_thread(self._ping_mc)
                est.update(
                    estado_num=1,
                    jugadores=[p.name for p in (st.players.sample or [])],
                    num_jugadores=st.players.online,
                    slots=st.players.max,
                    version=st.version.name,
                    ip=self.mc_address,
                    fuente='ping directo',
                )
            except Exception:
                est.update(estado_num=0, fuente='ping directo (sin respuesta)')
        return est

    # ------------------------------------------------------------------
    # Presentación
    # ------------------------------------------------------------------

    def _embed_estado(self, est: Dict[str, Any], ver_ip: bool) -> discord.Embed:
        num = est['estado_num']
        emoji = EMOJI_ESTADO.get(num, '❔')
        texto = TEXTO_ESTADO.get(num, 'Desconocido')
        color = (
            discord.Color.green() if num == 1
            else discord.Color.red() if num == 0
            else discord.Color.gold()
        )
        embed = discord.Embed(title=f'{emoji} Servidor de Minecraft — {texto}', color=color)

        if num is None:
            embed.description = (
                'No se pudo consultar el estado del servidor.\n'
                '- Si la sesión de Aternos expiró, un admin debe renovar la cookie '
                '`ATERNOS_SESSION` del `.env`.\n'
                '- Define `MC_ADDRESS` en el `.env` para tener un respaldo con ping directo.'
            )
            embed.set_footer(text='Phos · sin fuente de datos')
            return embed

        detalles: List[str] = []
        if est.get('software') or est.get('version'):
            detalles.append(
                f"**Software:** {est.get('software') or '?'} {est.get('version') or ''}".strip()
            )
        jugadores = est.get('jugadores') or []
        nombres = ', '.join(jugadores[:30]) or '—'
        if len(jugadores) > 30:
            nombres += f' … y {len(jugadores) - 30} más'
        detalles.append(f"**Jugadores:** {est.get('num_jugadores', 0)}/{est.get('slots', 0)}")
        detalles.append(f"**Conectados:** {nombres}")
        if est.get('motd'):
            detalles.append(f"**MOTD:** {str(est['motd'])[:100]}")
        countdown = est.get('countdown')
        if num == 1 and countdown and int(countdown) > 0:
            minutos = int(countdown) // 60
            detalles.append(f'**Auto-apagado:** en ~{minutos} min (si nadie juega)')
        embed.description = '\n'.join(detalles)

        if ver_ip and est.get('ip'):
            embed.add_field(name='Dirección', value=f'`{est["ip"]}`', inline=False)
        embed.set_footer(
            text=f'Phos · {est.get("fuente", "")} · '
                 f'{discord.utils.format_dt(discord.utils.utcnow(), style="t")}'
        )
        return embed

    # ------------------------------------------------------------------
    # Comandos
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name='estado',
        aliases=['status', 'servidor'],
        description='Muestra el estado del servidor de Minecraft',
    )
    async def estado(self, ctx: commands.Context) -> None:
        """Estado del servidor: en línea, jugadores conectados, etc."""
        await ctx.defer()
        est = await self._recolectar_estado()
        await ctx.send(embed=self._embed_estado(est, self._puede_ver_ip(ctx.author)))

    @commands.hybrid_command(
        name='encender',
        aliases=['on', 'start'],
        description='Enciende el servidor de Minecraft (Aternos)',
    )
    async def encender(self, ctx: commands.Context) -> None:
        """Enciende el servidor desde Discord."""
        permitidos = self.cfg.get('roles_pueden_encender', ['@everyone'])
        if not self._rol_autorizado(ctx.author, permitidos):
            await ctx.send(
                '⛔ No tienes permiso para encender el servidor. '
                'Pídelo a alguien con el rol adecuado.',
                ephemeral=True,
            )
            return

        if self._accion_en_curso:
            await ctx.send('⏳ Ya hay una operación en curso. Espera a que termine.')
            return
        self._accion_en_curso = True

        try:
            await ctx.defer()

            est = await self._recolectar_estado()
            if est['estado_num'] == 1:
                await ctx.send('✅ El servidor **ya está en línea**.', embed=self._embed_estado(est, self._puede_ver_ip(ctx.author)))
                return
            if est['estado_num'] in (2, 6, 10):
                await ctx.send('🟡 El servidor **ya se está iniciando**. Paciencia, la cola de Aternos a veces tarda.')
                return

            try:
                cliente = await self._obtener_cliente()
                await asyncio.to_thread(cliente.start)
            except AternosAuthError:
                await ctx.send(
                    '🔐 La sesión de Aternos expiró. Un admin debe renovar la cookie '
                    'ATERNOS_SESSION en el `.env` (los pasos están en el README).'
                )
                return
            except AternosError as err:
                await ctx.send(f'⚠️ Aternos rechazó el arranque: {err}')
                return

            mensaje = await ctx.send(
                '⚡ **Solicitud de encendido enviada.** El servidor puede tardar '
                'entre 1 y 5 minutos (cola + carga del mundo). Avisaré aquí cuando esté listo.'
            )
        finally:
            self._accion_en_curso = False

        # La vigilancia corre como tarea independiente, ya con el candado libre:
        # ella mantiene la etiqueta de acción para la presencia.
        asyncio.create_task(self._tarea_encendido(mensaje, ctx.author, etiqueta='encendiendo'))

    @commands.hybrid_command(
        name='apagar',
        aliases=['off', 'stop'],
        description='Apaga el servidor de Minecraft (solo roles autorizados)',
    )
    async def apagar(self, ctx: commands.Context) -> None:
        """Apaga el servidor desde Discord."""
        permitidos = self.cfg.get('roles_pueden_apagar', [])
        if not self._rol_autorizado(ctx.author, permitidos):
            await ctx.send('⛔ Solo ciertos roles pueden apagar el servidor.', ephemeral=True)
            return

        if self._accion_en_curso:
            await ctx.send('⏳ Ya hay una operación en curso.')
            return
        self._accion_en_curso = True

        try:
            await ctx.defer()
            try:
                cliente = await self._obtener_cliente()
                await asyncio.to_thread(cliente.stop)
            except AternosAuthError:
                await ctx.send('🔐 La sesión de Aternos expiró. Renueva la cookie ATERNOS_SESSION (ver README).')
                return
            except AternosError as err:
                await ctx.send(f'⚠️ No se pudo apagar: {err}')
                return

            mensaje = await ctx.send('🛑 **Apagando el servidor...** Avisaré cuando esté apagado.')
        finally:
            self._accion_en_curso = False

        # (Antes este create_task estaba DUPLICADO: uno dentro del try y
        # otro fuera; lanzaba dos vigilancias que peleaban por el mensaje.)
        asyncio.create_task(self._tarea_apagado(mensaje, etiqueta='apagando'))

    @commands.hybrid_command(
        name='reiniciar',
        aliases=['restart'],
        description='Reinicia el servidor de Minecraft (solo roles autorizados)',
    )
    async def reiniciar(self, ctx: commands.Context) -> None:
        """Reinicia el servidor desde Discord."""
        permitidos = self.cfg.get('roles_pueden_reiniciar', [])
        if not self._rol_autorizado(ctx.author, permitidos):
            await ctx.send('⛔ Solo ciertos roles pueden reiniciar el servidor.', ephemeral=True)
            return

        if self._accion_en_curso:
            await ctx.send('⏳ Ya hay una operación en curso.')
            return
        self._accion_en_curso = True

        try:
            await ctx.defer()
            try:
                cliente = await self._obtener_cliente()
                await asyncio.to_thread(cliente.restart)
            except AternosAuthError:
                await ctx.send('🔐 La sesión de Aternos expiró. Renueva la cookie ATERNOS_SESSION (ver README).')
                return
            except AternosError as err:
                await ctx.send(f'⚠️ No se pudo reiniciar: {err}')
                return

            mensaje = await ctx.send('🔄 **Reiniciando el servidor...** Avisaré cuando vuelva a estar en línea.')
        finally:
            self._accion_en_curso = False

        asyncio.create_task(self._tarea_encendido(mensaje, ctx.author, etiqueta='reiniciando'))

    # ------------------------------------------------------------------
    # Tareas en segundo plano
    # ------------------------------------------------------------------

    async def _tarea_encendido(
        self, mensaje: discord.Message, autor: Any, etiqueta: str = 'encendiendo',
    ) -> None:
        """Vigila el arranque: confirma si Aternos lo pide y avisa al terminar."""
        self._accion_actual = etiqueta
        try:
            confirmado = False
            for _ in range(30):  # hasta 5 minutos
                await asyncio.sleep(10)
                est = await self._recolectar_estado()
                num = est['estado_num']

                if num == 10 and not confirmado:
                    try:
                        cliente = await self._obtener_cliente()
                        await asyncio.to_thread(cliente.confirm)
                        confirmado = True
                        log.info('Arranque confirmado ante Aternos.')
                    except AternosError as err:
                        log.debug('No se pudo confirmar el arranque: %s', err)

                if num == 1:
                    # pequeña espera extra para que entren los primeros jugadores
                    await asyncio.sleep(5)
                    est = await self._recolectar_estado(usar_api=False)
                    if est['estado_num'] is None:
                        est['estado_num'] = 1
                    await mensaje.edit(
                        content='✅ **¡Servidor encendido!** Ya pueden entrar a jugar.',
                        embed=self._embed_estado(est, self._puede_ver_ip(autor)),
                    )
                    return
                if num == 7:
                    await mensaje.edit(
                        content='⚠️ Aternos reportó un **error** al iniciar el servidor. '
                                'Revisa el panel (https://aternos.org) por si hay que aceptar algo.'
                    )
                    return

            await mensaje.edit(
                content='⏳ El servidor **sigue arrancando** después de 5 minutos '
                        '(cola de Aternos muy lenta). Prueba `/estado` en un rato.'
            )
        except Exception as err:  # nunca debe romper el bot
            log.error('Error vigilando el encendido: %s', err)
        finally:
            self._accion_actual = None
            self._refrescar_presencia()

    async def _tarea_apagado(
        self, mensaje: discord.Message, etiqueta: str = 'apagando',
    ) -> None:
        self._accion_actual = etiqueta
        try:
            for _ in range(12):  # hasta 2 minutos
                await asyncio.sleep(10)
                est = await self._recolectar_estado(usar_api=False)
                if est['estado_num'] == 0:
                    await mensaje.edit(content='🔴 **Servidor apagado.** Nos vemos en la próxima partida.')
                    return
            await mensaje.edit(content='🤔 El servidor aún constaba como activo hace 2 minutos. Usa `/estado` para comprobarlo.')
        except Exception as err:
            log.error('Error vigilando el apagado: %s', err)
        finally:
            self._accion_actual = None
            self._refrescar_presencia()

    # ------------------------------------------------------------------
    # Presencia del bot — ping de Minecraft + estados de transición
    # ------------------------------------------------------------------

    def _texto_presencia(self, accion: Optional[str]) -> Optional[str]:
        """Texto de la presencia durante una operación en curso.

        Devuelve None si no hay operación: en ese caso la presencia se
        decide con el ping directo al servidor de Minecraft.
        """
        if accion == 'encendiendo':
            return '🟡 Servidor iniciándose...'
        if accion == 'apagando':
            return '🟠 Servidor apagándose...'
        if accion == 'reiniciando':
            return '🔄 Servidor reiniciándose...'
        return None

    def _refrescar_presencia(self) -> None:
        """Fuerza una actualización inmediata de la presencia al terminar
        una operación (si el loop está activo)."""
        try:
            if self.presencia.is_running():
                self.presencia.restart()
        except RuntimeError:
            pass

    @tasks.loop(minutes=2)
    async def presencia(self) -> None:
        try:
            if not self.mc_address:
                return
            # ¿Hay una operación en curso? Mostrar transición, sin ping
            # (durante un arranque el ping falla y diría "apagado" mintiendo).
            texto = self._texto_presencia(self._accion_actual)
            if texto is None:
                try:
                    st = await asyncio.to_thread(self._ping_mc)
                    texto = f'🟢 Servidor encendido · {st.players.online}/{st.players.max}'
                except Exception:
                    texto = '🔴 Servidor apagado'
            await self.bot.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.watching, name=texto,
                )
            )
        except Exception as err:
            log.debug('Error actualizando la presencia: %s', err)

    @presencia.before_loop
    async def _antes_presencia(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AternosCog(bot))