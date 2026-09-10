"""Discord posting layer. Consumes an asyncio.Queue of OutboxItems so the
ingest/score/correlate pipeline never depends on discord.py directly -- it
just puts messages on a queue.

Each item carries an `on_result(success: bool)` callback rather than the
caller marking its DB row "posted" as soon as it's enqueued. That way a row
is only ever marked posted once `channel.send` actually succeeds; a failed
send reports `success=False` and the caller (main.py) leaves the row
unposted so the next poll cycle re-enqueues and retries it, instead of the
alert silently disappearing forever.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable

import discord

from config import settings

log = logging.getLogger("bot.discord_bot")


_CLASSIFICATION_LABELS = {
    "already_moving_before": "already moving before headline",
    "subsequent_move": "moved after headline",
    "no_qualifying_move": "no qualifying move",
    "insufficient_evidence": "insufficient evidence",
}


def build_pin_embed(*, symbol: str, headline: str, url: str, impact_score: float,
                     reasoning: str, pct_move: float, volume_ratio: float, confirmed: bool,
                     classification: str | None = None) -> discord.Embed:
    color = discord.Color.green() if confirmed else discord.Color.greyple()
    title = f"{'✅ PINNED' if confirmed else '⬜ unconfirmed'}: {symbol} {pct_move:+.2f}%"
    embed = discord.Embed(title=title, description=headline, url=url or None, color=color)
    embed.add_field(name="Impact score", value=f"{impact_score:.1f}/10", inline=True)
    embed.add_field(name="IEX volume vs baseline", value=f"{volume_ratio:.1f}x", inline=True)
    embed.add_field(name="Data quality", value=_CLASSIFICATION_LABELS.get(classification, "unclassified"), inline=True)
    if reasoning:
        embed.add_field(name="Why flagged", value=reasoning[:200], inline=False)
    embed.set_footer(text="Associated move: an observation, not proof the headline caused it.")
    return embed


def build_unexplained_embed(*, symbol: str, pct_move: float, zscore: float, volume_ratio: float,
                             matched_headline: str | None = None, matched_url: str | None = None,
                             matched_relation: str | None = None,
                             matched_timing_secs: float | None = None) -> discord.Embed:
    embed = discord.Embed(
        title=f"❓ Unexplained move: {symbol} {pct_move:+.2f}%",
        description="No matching headline observed in monitored sources -- limited to feed coverage, not evidence of leaks or hidden activity.",
        color=discord.Color.orange(),
        url=matched_url or None,
    )
    embed.add_field(name="Z-score", value=f"{zscore:.2f}", inline=True)
    embed.add_field(name="IEX volume vs baseline", value=f"{volume_ratio:.1f}x", inline=True)
    if matched_headline:
        when = "before" if matched_relation == "preceding" else "after"
        timing = abs(matched_timing_secs) if matched_timing_secs is not None else 0.0
        embed.description = f"Candidate headline ({when} the move by {timing:.0f}s, dated as of this reconciliation): {matched_headline}"
        embed.add_field(name="Association", value="observed candidate, not a causal claim", inline=False)
    return embed


@dataclass
class OutboxItem:
    embed: discord.Embed
    on_result: Callable[[bool], None]


class PinBotClient(discord.Client):
    def __init__(self, outbox: "asyncio.Queue[OutboxItem]"):
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
            item = await self._outbox.get()
            try:
                await channel.send(embed=item.embed)
            except Exception as exc:
                log.warning("failed to post embed: %s", exc)
                item.on_result(False)
            else:
                item.on_result(True)


async def run_bot(outbox: "asyncio.Queue[OutboxItem]") -> None:
    client = PinBotClient(outbox)
    await client.start(settings.DISCORD_BOT_TOKEN)
