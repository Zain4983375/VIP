"""Multi-server Discord player access bot.

Commands:
    .c example  -> create a private channel, role, and one-use invite
    .d example  -> delete that private channel and role
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


def normalize_label(label: str) -> str:
    label = label.strip().lower().replace(" ", "-")
    return "-".join(part for part in label.split("-") if part)


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


async def find_used_tracked_invites(guild: discord.Guild) -> list[InviteRecord]:
    """ALL tracked invites that look like they were just used (candidates).

    Discord deletes a 1-use invite right after it is consumed, so
    "not present in the active list" = it was used.
    """
    try:
        invites = await guild.invites()
    except discord.Forbidden:
        log.warning("Cannot inspect invites in guild %s.", guild.id)
        invites = []

    uses_now = {inv.code: (inv.uses or 0) for inv in invites}
    candidates: list[InviteRecord] = []
    for code, record in invite_records.items():
        if record.guild_id != guild.id or record.last_uses != 0:
            continue
        if code in uses_now:
            if uses_now[code] > record.last_uses:
                candidates.append(record)
        else:
            candidates.append(record)  # 1-use invite gayab = consumed
    return candidates


async def cleanup_stale_records(guild: discord.Guild) -> None:
    """If a tracked invite was consumed while the bot was offline,
    mark it as consumed — otherwise it could wrongly match the next join."""
    try:
        invites = await guild.invites()
    except discord.Forbidden:
        return
    active = {inv.code for inv in invites}
    changed = False
    for record in invite_records.values():
        if (record.guild_id == guild.id
                and record.last_uses == 0
                and record.invite_code not in active):
            record.last_uses = 1
            changed = True
    if changed:
        save_state()
        log.info("Marked stale invite records as consumed in guild %s.", guild.id)


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
            await cleanup_stale_records(guild)
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
            candidates = await find_used_tracked_invites(member.guild)

            # Case 0: no tracked invite looks used — assign no role.
            if not candidates:
                log.warning(
                    "Could not identify a tracked invite for member %s in guild %s. No role assigned.",
                    member.id, member.guild.id,
                )
                return

            # Case 2+: more than one candidate — DO NOT GUESS.
            # Assigning no role is better than assigning the wrong one.
            if len(candidates) > 1:
                log.error(
                    "Ambiguous join for member %s in guild %s: %d candidate invites %s. No role assigned.",
                    member.id, member.guild.id, len(candidates),
                    [r.invite_code for r in candidates],
                )
                for record in candidates:
                    record.last_uses = 1
                save_state()
                timestamp = discord.utils.format_dt(discord.utils.utcnow(), style="F")
                player_name = discord.utils.escape_markdown(member.display_name)
                warned_channels: set[int] = set()
                for record in candidates:
                    ch_id = record.output_channel_id or record.channel_id
                    if ch_id in warned_channels:
                        continue
                    warned_channels.add(ch_id)
                    await send_to_output_channel(
                        member.guild, record,
                        f"⚠️ **Ambiguous join — no role assigned (to avoid a wrong role)**\n"
                        f"👤 **Player:-** {player_name}\n"
                        f"📅 **Date:-** {timestamp}\n"
                        f"2+ tracked invites appear to have been used at the same time. Please assign the role manually.",
                    )
                return

            # Case 1: exactly one candidate — this is the right invite, assign the role.
            record = candidates[0]
            record.last_uses = 1
            save_state()

            # Find the role by the channel name, so the same-named role gets assigned.
            channel = await resolve_channel(member.guild, record.channel_id)
            role = None

            if channel:
                # The channel name IS the role name (e.g. with .c example both are "example")
                role = discord.utils.get(member.guild.roles, name=channel.name)

            # If no role matched the channel name, fall back to the saved role_id.
            if role is None:
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
    label = normalize_label(label)
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

            # Discord channel names ko normalize kar sakta hai (lowercase waghera),
            # is liye role ka naam final channel naam se match kara do.
            if role.name != channel.name:
                await role.edit(name=channel.name, reason="Match private channel name")
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
        await ctx.reply("❌ Unexpected error. Please check the Railway logs.", mention_author=False)


@bot.command(name="d")
@commands.guild_only()
@commands.has_guild_permissions(manage_channels=True, manage_roles=True)
async def delete_player_access(ctx: commands.Context, *, label: str = "") -> None:
    """Delete a private channel and its role: .d example"""
    label = normalize_label(label)
    if not label:
        await ctx.reply("Usage: `.d example`", mention_author=False)
        return

    async with state_lock:
        guild = ctx.guild
        assert guild is not None

        # Step 1 (most reliable): state file me is label ka exact channel/role ID dhoondo.
        # Naam badal bhi gaya ho to ID se mil jayega.
        record = next(
            (r for r in invite_records.values()
             if r.guild_id == guild.id and r.label == label),
            None,
        )

        channel = None
        role = None
        if record is not None:
            channel = await resolve_channel(guild, record.channel_id)
            role = guild.get_role(record.role_id)
            if role is None:
                log.warning("Role id %s from state not found in guild %s.", record.role_id, guild.id)

        # Step 2 (fallback): naam se dhoondo — case-insensitive.
        if channel is None:
            channel = discord.utils.get(guild.text_channels, name=label)
        if channel is None:
            channel = discord.utils.find(
                lambda c: c.name.lower() == label.lower(), guild.text_channels
            )
        if role is None:
            role = discord.utils.get(guild.roles, name=label)
        if role is None:
            role = discord.utils.find(
                lambda r: r.name.lower() == label.lower(), guild.roles
            )
        if role is None:
            log.warning("No role found for label '%s' in guild %s.", label, guild.id)

        deleted_lines: list[str] = []

        if channel is not None:
            try:
                channel_id = channel.id
                await channel.delete(reason=f"Deleted by .d ({ctx.author})")
                deleted_lines.append(f"📌 **Channel:-** {label}")
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.exception("Could not delete channel %s", channel.id)
                await ctx.reply(f"❌ Channel delete failed: `{exc}`", mention_author=False)
                return
        else:
            channel_id = None

        if role is not None:
            role_id = role.id
            try:
                await role.delete(reason=f"Deleted by .d ({ctx.author})")
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.exception("Could not delete role %s", role.id)
                await ctx.reply(f"❌ Role delete failed: `{exc}`", mention_author=False)
                return
            deleted_lines.append(f"🎭 **Role:-** {label}")
        else:
            role_id = None

        # Clear invite records linked to this channel/role,
        # so joining via the old link assigns no role.
        removed = 0
        for code in [
            c for c, rec in invite_records.items()
            if rec.guild_id == guild.id
            and ((role_id is not None and rec.role_id == role_id)
                 or (channel_id is not None and rec.channel_id == channel_id))
        ]:
            invite_records.pop(code, None)
            removed += 1
        if removed:
            save_state()
            log.info("Removed %d invite record(s) for label %s.", removed, label)

        if not deleted_lines:
            await ctx.reply(
                f"❌ Nothing found with the name `{label}` — no channel, no role.",
                mention_author=False,
            )
            return

        await ctx.reply("🗑️ **Deleted!**\n" + "\n".join(deleted_lines), mention_author=False)


@delete_player_access.error
async def delete_player_access_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply("❌ You need Manage Channels and Manage Roles permissions.", mention_author=False)
    elif isinstance(error, commands.NoPrivateMessage):
        await ctx.reply("❌ This command can only be used inside a server.", mention_author=False)
    elif isinstance(error, commands.CommandInvokeError):
        log.exception("Command error", exc_info=error.original)
        await ctx.reply("❌ Unexpected error. Please check the Railway logs.", mention_author=False)


@bot.command(name="helpbot")
async def helpbot(ctx: commands.Context) -> None:
    await ctx.reply(
        "**Player Bot**\n"
        "`.c example` — create a private channel, role, and one-use invite.\n"
        "`.d example` — delete that private channel and role.\n"
        "Player join results are posted in the channel where `.c` was used.\n"
        "The new channel is created inside the same category as the command channel.",
        mention_author=False,
    )


# Load persisted state ONCE at startup (not on every reconnect).
load_state()
bot.run(TOKEN)
