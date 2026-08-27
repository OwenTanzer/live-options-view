"""Discord posting layer. Consumes an asyncio.Queue of pre-formatted embeds
so the ingest/score/correlate pipeline never depends on discord.py directly
-- it just puts messages on a queue.
"""
from __future__ import annotations

import asyncio
import logging

import discord

from config import settings

log = logging.getLogger("bot.discord_bot")


def build_pin_embed(*, symbol: str, headline: str, url: str, impact_score: float,
                     reasoning: str, pct_move: float, volume_ratio: float, confirmed: bool) -> discord.Embed:
    color = discord.Color.green() if confirmed else discord.Color.greyple()
    title = f"{'✅ PINNED' if confirmed else '⬜ unconfirmed'}: {symbol} {pct_move:+.2f}%"
    embed = discord.Embed(title=title, description=headline, url=url or None, color=color)
    embed.add_field(name="Impact score", value=f"{impact_score:.1f}/10", inline=True)
    embed.add_field(name="Volume vs baseline", value=f"{volume_ratio:.1f}x", inline=True)
    if reasoning:
        embed.add_field(name="Why flagged", value=reasoning[:200], inline=False)
    return embed


def build_unexplained_embed(*, symbol: str, pct_move: float, zscore: float, volume_ratio: float) -> discord.Embed:
    embed = discord.Embed(
        title=f"❓ Unexplained move: {symbol} {pct_move:+.2f}%",
        description="No matching headline in the last 5 minutes -- watching for news.",
        color=discord.Color.orange(),
    )
    embed.add_field(name="Z-score", value=f"{zscore:.2f}", inline=True)
    embed.add_field(name="Volume vs baseline", value=f"{volume_ratio:.1f}x", inline=True)
    return embed


class PinBotClient(discord.Client):
    def __init__(self, outbox: asyncio.Queue[discord.Embed]):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self._outbox = outbox

    async def on_ready(self) -> None:
        log.info("discord bot logged in as %s", self.user)
        self.loop.create_task(self._drain_outbox())

    async def _drain_outbox(self) -> None:
        channel = self.get_channel(settings.DISCORD_CHANNEL_ID)
        if channel is None:
            channel = await self.fetch_channel(settings.DISCORD_CHANNEL_ID)
        while True:
            embed = await self._outbox.get()
            try:
                await channel.send(embed=embed)
            except Exception as exc:
                log.warning("failed to post embed: %s", exc)


async def run_bot(outbox: asyncio.Queue[discord.Embed]) -> None:
    client = PinBotClient(outbox)
    await client.start(settings.DISCORD_BOT_TOKEN)
