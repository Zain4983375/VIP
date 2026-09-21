import discord
from discord.ext import commands
import json, os, asyncio
from datetime import datetime, timezone, timedelta

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
TOKEN = os.getenv('DISCORD_TOKEN')
PREFIX = '.'
DATA_FILE = '/data/invite_data.json'

# Pakistan Standard Time (UTC+5)
PKT = timezone(timedelta(hours=5))

# ─────────────────────────────────────────────
# BOT SETUP
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)

# ─────────────────────────────────────────────
# GLOBAL STATE
# ─────────────────────────────────────────────
invite_map: dict[str, int] = {}       # code → role_id
invite_owners: dict[str, int] = {}    # code → user_id (jisne .cc chalayi)
invite_channels: dict[str, str] = {}  # code → channel name (DM ke liye)
invite_uses: dict[str, int] = {}      # code → uses count
pending_roles: list[dict] = []        # deleted invites ka fallback


# ─────────────────────────────────────────────
# DATA PERSISTENCE
# ─────────────────────────────────────────────
def save():
    try:
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        data = {
            "invites": invite_map,
            "owners": invite_owners,
            "channels": invite_channels,
        }
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f)
        print(f"[SAVE] Saved {len(invite_map)} invites")
    except Exception as e:
        print(f"[SAVE ERROR] {e}")


def load():
    """Returns (invites, owners, channels). Handles old format too."""
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE) as f:
                raw = json.load(f)
            # Old format: {code: role_id}
            if raw and all(isinstance(v, int) for v in raw.values()):
                print("[LOAD] Old format detected, migrating...")
                return ({k: int(v) for k, v in raw.items()}, {}, {})
            # New format
            return (
                {k: int(v) for k, v in raw.get("invites", {}).items()},
                {k: int(v) for k, v in raw.get("owners", {}).items()},
                raw.get("channels", {}),
            )
    except Exception as e:
        print(f"[LOAD ERROR] {e}")
    return {}, {}, {}


async def take_snapshot(guild):
    try:
        invs = await guild.invites()
        return {i.code: i.uses for i in invs}
    except Exception as e:
        print(f"[SNAPSHOT ERROR] {e}")
        return {}


# ─────────────────────────────────────────────
# DM NOTIFICATION
# ─────────────────────────────────────────────
async def dm_owner(inviter_id: int, member: discord.Member, role: discord.Role,
                   channel_name: str, invite_code: str):
    """Inviter ko DM bhejo jab naya player join kare."""
    if not inviter_id:
        print("[DM] No owner ID, skipping DM")
        return

    try:
        user = await bot.fetch_user(inviter_id)

        # Date/Time in Pakistan Standard Time
        now_pkt = datetime.now(PKT)
        date_str = now_pkt.strftime("%d %b %Y")          # 22 Sep 2026
        time_str = now_pkt.strftime("%I:%M %p PKT")      # 08:45 PM PKT
        discord_ts = f"<t:{int(now_pkt.timestamp())}:F>"  # Auto local time

        embed = discord.Embed(
            title="🎉 New Player Joined VIP!",
            description=f"{member.mention} just joined **{member.guild.name}**!",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="👤 Player", value=f"{member.mention}\n`{member.name}`", inline=True)
        embed.add_field(name="🎭 Role Given", value=role.mention, inline=True)
        embed.add_field(name="📨 Via Channel", value=f"#{channel_name or 'Unknown'}", inline=True)
        embed.add_field(name="🔗 Invite Code", value=f"`{invite_code}`", inline=True)
        embed.add_field(name="📅 Date", value=date_str, inline=True)
        embed.add_field(name="⏰ Time", value=f"{time_str}\n{discord_ts}", inline=True)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"User ID: {member.id} • VIP System")

        await user.send(embed=embed)
        print(f"[DM] ✅ Sent notification to {user.name} about {member.name}")

    except discord.Forbidden:
        print(f"[DM] ❌ Can't DM user {inviter_id} (DMs closed)")
    except Exception as e:
        print(f"[DM ERROR] {e}")


# ─────────────────────────────────────────────
# EVENTS
# ─────────────────────────────────────────────
@bot.event
async def on_ready():
    global invite_map, invite_owners, invite_channels
    invite_map, invite_owners, invite_channels = load()
    for g in bot.guilds:
        snap = await take_snapshot(g)
        invite_uses.update(snap)
    print(f"✅ Bot online: {bot.user}")
    print(f"📋 Loaded {len(invite_map)} invites, {len(invite_owners)} owners")


@bot.event
async def on_invite_create(invite):
    invite_uses[invite.code] = invite.uses or 0


@bot.event
async def on_invite_delete(invite):
    if invite.code in invite_map:
        role_id = invite_map.pop(invite.code)
        owner_id = invite_owners.pop(invite.code, None)
        chan_name = invite_channels.pop(invite.code, None)
        pending_roles.append({
            "role_id": role_id,
            "owner_id": owner_id,
            "channel_name": chan_name,
            "code": invite.code,
        })
        save()
        print(f"[INVITE DELETE] {invite.code} → moved to pending")


@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    print(f"[JOIN] {member.name} joined {guild.name}")

    role_id = None
    owner_id = None
    channel_name = None
    invite_code = None

    # ── Step 1: Pending (deleted invite case) ──
    if pending_roles:
        p = pending_roles.pop(0)
        role_id = p["role_id"]
        owner_id = p.get("owner_id")
        channel_name = p.get("channel_name")
        invite_code = p.get("code")
        print(f"[JOIN] Using pending: role={role_id} owner={owner_id}")

    # ── Step 2: Retry loop to detect which invite was used ──
    if role_id is None:
        current = {}
        for attempt in range(6):
            current = await take_snapshot(guild)
            matched = False
            for code, uses in current.items():
                old_uses = invite_uses.get(code, 0)
                if uses > old_uses and code in invite_map:
                    role_id = invite_map.pop(code)
                    owner_id = invite_owners.pop(code, None)
                    channel_name = invite_channels.pop(code, None)
                    invite_code = code
                    save()
                    matched = True
                    print(f"[JOIN] ✅ Matched invite {code} (uses {old_uses}→{uses}) → role {role_id}")
                    break
            if matched:
                break
            print(f"[JOIN] Attempt {attempt + 1}: no match, waiting 0.4s...")
            await asyncio.sleep(0.4)

        # ── Step 3: Fallback — invite gone from guild ──
        if role_id is None:
            for code in list(invite_map.keys()):
                if code not in current:
                    role_id = invite_map.pop(code)
                    owner_id = invite_owners.pop(code, None)
                    channel_name = invite_channels.pop(code, None)
                    invite_code = code
                    save()
                    print(f"[JOIN] Fallback: invite {code} gone → role {role_id}")
                    break

        invite_uses.clear()
        invite_uses.update(current)

    if role_id is None:
        print(f"[JOIN] ❌ No role found for {member.name}")
        return

    role = guild.get_role(role_id)
    if not role:
        print(f"[JOIN] ❌ Role {role_id} not found in guild")
        return

    # ── Assign role ──
    try:
        await member.add_roles(role, reason="Auto-assigned via invite")
        print(f"[JOIN] ✅✅ Role '{role.name}' assigned to {member.name}")
    except Exception as e:
        print(f"[JOIN] ❌ ROLE ADD ERROR: {e}")
        return

    # ── DM the inviter ──
    await dm_owner(owner_id, member, role, channel_name, invite_code)


# ─────────────────────────────────────────────
# COMMAND: .cc <channel_name> <role_name>
# ─────────────────────────────────────────────
@bot.command(name='cc')
@commands.has_permissions(manage_channels=True)
async def create_channel(ctx, channel_name: str, role_name: str):
    guild = ctx.guild

    try:
        await ctx.message.delete()
    except:
        pass

    category = ctx.channel.category

    # Create or find role
    role = discord.utils.get(guild.roles, name=role_name)
    if not role:
        role = await guild.create_role(name=role_name, mentionable=True)
        print(f"[CC] Created role: {role.name} ({role.id})")

    # Channel overwrites
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            manage_channels=True,
            create_instant_invite=True
        ),
        role: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True
        ),
    }

    # Create channel
    channel = await guild.create_text_channel(
        name=channel_name,
        overwrites=overwrites,
        category=category
    )

    # Create 1-use invite
    invite = await channel.create_invite(max_uses=1, max_age=0, unique=True)

    # Save mapping
    invite_map[invite.code] = role.id
    invite_owners[invite.code] = ctx.author.id
    invite_channels[invite.code] = channel_name
    invite_uses[invite.code] = 0
    save()

    print(f"[CC] Invite {invite.code} → role {role.id} owner {ctx.author.id}")

    # DM the invite link
    msg = (
        f'**:white_check_mark: Done!**\n'
        f'**:pushpin: Channel:** {channel_name}\n'
        f'**:performing_arts: Role:** @{role_name}\n'
        f'**:link: Invite (1-use, never expires):**\n'
        f'{invite.url}\n\n'
        f'*Jab koi is link se join karega, tumhe yahan DM mein notification milegi.* 🔔'
    )
    try:
        await ctx.author.send(msg)
    except discord.Forbidden:
        await ctx.send('DM band hai!', delete_after=8)


@create_channel.error
async def cc_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send('`.cc <channel_name> <role_name>`', delete_after=10)
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send('Missing permissions.', delete_after=8)
    else:
        await ctx.send(f'Error: {error}', delete_after=10)


# ─────────────────────────────────────────────
# START
# ─────────────────────────────────────────────
bot.run(TOKEN)
