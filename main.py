"""
main.py — Phos: bot de Discord con módulos de Aternos y Spotify.

Carga dos cogs:
  - cogs.aternos : /estado /encender /apagar /reiniciar + presencia 🟢/🔴
  - cogs.spotify : añade canciones a la playlist escribiendo en un canal

Si Phos crece, añade más extensiones en setup_hook().
"""

from __future__ import annotations

import asyncio
import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-7s %(name)s: %(message)s',
)


class Phos(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        # Necesario para los comandos con prefijo (phos!estado, ...)
        # y para el canal de canciones de Spotify.
        intents.message_content = True
        super().__init__(
            command_prefix=commands.when_mentioned_or(os.getenv('PREFIX', 'phos!')),
            intents=intents,
            help_command=commands.DefaultHelpCommand(),
        )

    async def setup_hook(self) -> None:
        # --- Módulos ---
        await self.load_extension('cogs.aternos')
        await self.load_extension('cogs.spotify')

        # --- Sincronización de slash commands ---
        # Si GUILD_ID está en el .env, se sincronizan SOLO en ese servidor
        # (instantáneo, ideal para desarrollo). Sin GUILD_ID, se sincronizan
        # de forma global (tarda hasta ~1 hora la primera vez).
        guild_id = os.getenv('GUILD_ID')
        try:
            if guild_id:
                guild = discord.Object(id=int(guild_id))
                self.tree.copy_global_to(guild=guild)
                sincronizados = await self.tree.sync(guild=guild)
            else:
                sincronizados = await self.tree.sync()
            logging.getLogger('phos').info(
                '%d comandos slash sincronizados.', len(sincronizados),
            )
        except discord.HTTPException as err:
            logging.getLogger('phos').warning(
                'No se pudieron sincronizar los comandos: %s', err,
            )


bot = Phos()


@bot.event
async def on_ready() -> None:
    print(f'Phos conectado como {bot.user} (id {bot.user.id})')  # type: ignore[union-attr]


@bot.command(name='sync')
@commands.is_owner()
async def sync_guild(ctx: commands.Context) -> None:
    """Re-sincroniza los slash commands en este servidor (instantáneo)."""
    if ctx.guild is None:
        await ctx.send('Úsalo dentro de un servidor.')
        return
    bot.tree.copy_global_to(guild=ctx.guild)
    sincronizados = await bot.tree.sync(guild=ctx.guild)
    await ctx.send(f'✅ {len(sincronizados)} comandos sincronizados en este servidor.')


async def main() -> None:
    token = os.getenv('DISCORD_TOKEN')
    if not token:
        raise SystemExit('Falta DISCORD_TOKEN en el archivo .env (copia .env.example).')
    async with bot:
        await bot.start(token)


if __name__ == '__main__':
    asyncio.run(main())