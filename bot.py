"""Multi-server Discord player access bot.

Commands:
    .c example  -> create a private channel, role, and one-use invite
    .helpbot    -> show help

Join results are posted in the channel where .c was used.
The new channel is created inside the SAME CATEGORY as the .c command channel.

⚠️ IMPORTANT (Discord Developer Portal):
   Enable "SERVER MEMBERS INTENT" and "MESSAGE CONTENT INTENT" under
   Bot -> Privileged Gateway Intents. Without Server Members Intent,
   on_member_join will NEVER fire (no role assignment, no output).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is missing.")

PREFIX = os.getenv("BOT_PREFIX", ".")
STATE_FILE = Path(os.getenv("BOT_STATE_FILE", "bot_state.json"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("player-bot")


@dataclass
class InviteRecord:
    invite_code: str
    guild_id: int
    channel_id: int
    role_id: int
    label: str
    last_uses: int = 0
    output_channel_id: int = 0


invite_records: dict[str, InviteRecord] = {}
state_lock = asyncio.Lock()


def load_state() -> None:
    global invite_records
    if not STATE_FILE.exists():
        return
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        invite_data = raw.get("invites", raw) if isinstance(raw, dict) else {}
        loaded: dict[str, InviteRecord] = {}
        for code, record in invite_data.items():
            try:
                loaded[code] = InviteRecord(**record)
            except TypeError:
                log.warning("Skipping malformed invite record: %s", code)
        invite_records = loaded
        log.info("Loaded %d invite record(s).", len(invite_records))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        log.warning("Could not load state: %s", exc)


def save_state() -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "invites": {code: asdict(record) for code, record in invite_records.items()},
        }
        temp_file = STATE_FILE.with_suffix(".tmp")
        temp_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp_file.replace(STATE_FILE)
    except OSError:
        log.exception("Could not save state.")


intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)


async def resolve_channel(guild: discord.Guild, channel_id: int):
    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            channel = None
    return channel


async def send_to_output_channel(guild: discord.Guild, record: InviteRecord, message: str) -> None:
    channel_id = record.output_channel_id or record.channel_id
    channel = await resolve_channel(guild, channel_id)
    if channel is None:
        log.warning("Output channel %s not found in guild %s.", channel_id, guild.id)
        return
    try:
        await channel.send(message)
    except (discord.Forbidden, discord.HTTPException):
        log.exception("Could not send join output to channel %s.", channel_id)


async def refresh_tracked_invites(guild: discord.Guild) -> None:
    """Sync last_uses for invites that still exist."""
    try:
        invites = await guild.invites()
    except discord.Forbidden:
        log.warning("Cannot read invites in guild %s.", guild.id)
        return
    changed = False
    for invite in invites:
        record = invite_records.get(invite.code)
        if record and record.guild_id == guild.id:
            current = invite.uses or 0
            if record.last_uses != current:
                record.last_uses = current
                changed = True
    if changed:
        save_state()


async def find_used_tracked_invite(guild: discord.Guild) -> Optional[InviteRecord]:
    try:
        invites = await guild.invites()
    except discord.Forbidden:
        log.warning("Cannot inspect invites in guild %s.", guild.id)
        invites = []

    existing_codes = {inv.code for inv in invites}
    used: Optional[InviteRecord] = None

    # Case 1: multi-use invite still exists, uses counter went up
    for invite in invites:
        record = invite_records.get(invite.code)
        if not record or record.guild_id != guild.id:
            continue
        current = invite.uses or 0
        if current > record.last_uses:
            record.last_uses = current
            used = record

    # Case 2: 1-use invite is DELETED by Discord after being used.
    if used is None:
        for code, record in invite_records.items():
            if record.guild_id != guild.id:
                continue
            if record.last_uses == 0 and code not in existing_codes:
                record.last_uses = 1
                used = record
                break

    save_state()
    return used


@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s (%s).", bot.user, bot.user.id if bot.user else "?")
    log.info(
        "Intents -> members=%s message_content=%s guilds=%s",
        bot.intents.members, bot.intents.message_content, bot.intents.guilds,
    )
    if not bot.intents.members:
        log.error(
            "SERVER MEMBERS INTENT is OFF. on_member_join will NEVER fire! "
            "Enable it in Discord Developer Portal -> Bot -> Privileged Gateway Intents."
        )
    log.info("Connected to %d server(s).", len(bot.guilds))
    for guild in bot.guilds:
        try:
            await refresh_tracked_invites(guild)
        except Exception:
            log.exception("refresh_tracked_invites failed for guild %s", guild.id)


@bot.event
async def on_member_join(member: discord.Member) -> None:
    try:
        log.info(
            "Member join event: %s (%s) joined %s (%s).",
            member, member.id, member.guild.name, member.guild.id,
        )

        if member.bot:
            return

        # Discord updates invite usage a moment after the join event.
        await asyncio.sleep(3)

        async with state_lock:
            record = await find_used_tracked_invite(member.guild)

            if not record:
                candidates = [
                    item for item in invite_records.values()
                    if item.guild_id == member.guild.id and item.last_uses == 0
                ]
                if candidates:
                    record = candidates[-1]
                    record.last_uses = 1
                    save_state()

            if not record:
                log.warning(
                    "Could not identify a tracked invite for member %s in guild %s.",
                    member.id, member.guild.id,
                )
                return

            role = member.guild.get_role(record.role_id)
            timestamp = discord.utils.format_dt(discord.utils.utcnow(), style="F")
            player_name = discord.utils.escape_markdown(member.display_name)

            if role is None:
                await send_to_output_channel(
                    member.guild, record,
                    f"⚠️ Player joined but role was not found.\n"
                    f"👤 Player: **{player_name}**\n📅 Date: {timestamp}",
                )
                return

            me = member.guild.me
            if me is None:
                try:
                    me = await member.guild.fetch_member(bot.user.id)
                except Exception:
                    me = None

            if me and role >= me.top_role:
                await send_to_output_channel(
                    member.guild, record,
                    f"❌ **Player joined — Role assignment failed**\n"
                    f"👤 **Player:-** {player_name}\n"
                    f"🎭 **Role:-** {role.name}\n"
                    f"📅 **Date:-** {timestamp}\n"
                    f"Reason: Move the bot role above the `{role.name}` role.",
                )
                return

            try:
                await member.add_roles(
                    role, reason=f"Tracked player invite: {record.invite_code}"
                )
                message = (
                    f"✅ **Player joined — Role assigned**\n"
                    f"👤 **Player:-** {player_name}\n"
                    f"🎭 **Role:-** {role.name}\n"
                    f"📅 **Date:-** {timestamp}"
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.exception("Could not assign role %s to %s", role.id, member.id)
                message = (
                    f"❌ **Player joined — Role assignment failed**\n"
                    f"👤 **Player:-** {player_name}\n"
                    f"🎭 **Role:-** {role.name}\n"
                    f"📅 **Date:-** {timestamp}\n"
                    f"Reason: {exc}"
                )
            await send_to_output_channel(member.guild, record, message)

    except Exception:
        log.exception("on_member_join handler crashed for %s", getattr(member, "id", "?"))


@bot.command(name="c")
@commands.guild_only()
@commands.has_guild_permissions(manage_channels=True, manage_roles=True)
async def create_player_access(ctx: commands.Context, *, label: str = "") -> None:
    """Create a private channel, role, and tracked invite: .c example"""
    label = label.strip().lower().replace(" ", "-")
    label = "-".join(part for part in label.split("-") if part)
    if not label or len(label) > 40:
        await ctx.reply("Usage: `.c example` — label 1–40 characters.", mention_author=False)
        return

    async with state_lock:
        guild = ctx.guild
        assert guild is not None
        role_name = label
        existing_role = discord.utils.get(guild.roles, name=role_name)
        if existing_role:
            await ctx.reply(f"Role `{role_name}` already exists.", mention_author=False)
            return

        try:
            role = await guild.create_role(
                name=role_name, mentionable=False, reason=f"Created by .c for {label}"
            )
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                role: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                ),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, manage_channels=True,
                    manage_messages=True, create_instant_invite=True,
                ),
            }
            channel = await guild.create_text_channel(
                label[:90],
                overwrites=overwrites,
                category=ctx.channel.category,   # 👈 same category as .c command channel
                reason=f"Private player channel for {label}",
            )
            invite = await channel.create_invite(
                max_age=0, max_uses=1, unique=True,
                reason=f"Tracked player invite for {label}",
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.exception("Could not create player access for %s", label)
            await ctx.reply(f"❌ Creation failed: `{exc}`", mention_author=False)
            return

        invite_records[invite.code] = InviteRecord(
            invite_code=invite.code, guild_id=guild.id, channel_id=channel.id,
            role_id=role.id, label=label, last_uses=invite.uses or 0,
            output_channel_id=ctx.channel.id,
        )
        save_state()

    await ctx.reply(
        f"✅ **Done!**\n"
        f"📌 **Channel:-** {channel.mention}\n"
        f"🎭 **Role:-** {role.mention}\n"
        f"🔗 **Invite (1-use, never expires):-👇**\n"
        f"{invite.url}",
        mention_author=False,
    )


@create_player_access.error
async def create_player_access_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply("❌ You need Manage Channels and Manage Roles permissions.", mention_author=False)
    elif isinstance(error, commands.NoPrivateMessage):
        await ctx.reply("❌ This command can only be used inside a server.", mention_author=False)
    elif isinstance(error, commands.CommandInvokeError):
        log.exception("Command error", exc_info=error.original)
        await ctx.reply("❌ Unexpected error aa gaya. Railway logs check karein.", mention_author=False)


@bot.command(name="helpbot")
async def helpbot(ctx: commands.Context) -> None:
    await ctx.reply(
        "**Player Bot**\n"
        "`.c example` — create a private channel, role, and one-use invite.\n"
        "Player join results are posted in the channel where `.c` was used.\n"
        "The new channel is created inside the same category as the command channel.",
        mention_author=False,
    )


# Load persisted state ONCE at startup (not on every reconnect).
load_state()
bot.run(TOKEN)
