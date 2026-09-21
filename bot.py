import discord
from discord.ext import commands
import json, os, asyncio

TOKEN = os.getenv('DISCORD_TOKEN')
PREFIX = '.'
DATA_FILE = '/data/invite_data.json'

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)

invite_map: dict[str, int] = {}
invite_uses: dict[str, int] = {}
pending_roles: list[int] = []


def save():
    try:
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        with open(DATA_FILE, 'w') as f:
            json.dump(invite_map, f)
        print(f"[SAVE] invite_map = {invite_map}")
    except Exception as e:
        print(f"[SAVE ERROR] {e}")


def load():
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE) as f:
                data = {k: int(v) for k, v in json.load(f).items()}
                print(f"[LOAD] invite_map = {data}")
                return data
    except Exception as e:
        print(f"[LOAD ERROR] {e}")
    return {}


async def take_snapshot(guild):
    try:
        invs = await guild.invites()
        snap = {i.code: i.uses for i in invs}
        print(f"[SNAPSHOT] {snap}")
        return snap
    except Exception as e:
        print(f"[SNAPSHOT ERROR] {e}")
        return {}


@bot.event
async def on_ready():
    global invite_map
    invite_map = load()
    for g in bot.guilds:
        snap = await take_snapshot(g)
        invite_uses.update(snap)
    print(f"✅ Bot online: {bot.user}")
    print(f"📋 Loaded invite_map: {invite_map}")


@bot.event
async def on_invite_create(invite):
    print(f"[INVITE CREATE] {invite.code} uses={invite.uses}")
    invite_uses[invite.code] = invite.uses or 0


@bot.event
async def on_invite_delete(invite):
    if invite.code in invite_map:
        role_id = invite_map.pop(invite.code)
        pending_roles.append(role_id)
        save()
        print(f"[INVITE DELETE] {invite.code} → pending role {role_id}")


@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    print(f"[JOIN] {member.name} joined {guild.name}")
    print(f"[JOIN] pending_roles = {pending_roles}")
    print(f"[JOIN] invite_map = {invite_map}")
    print(f"[JOIN] invite_uses (before) = {invite_uses}")

    role_id = None

    # Step 1: Pending role (deleted invite case)
    if pending_roles:
        role_id = pending_roles.pop(0)
        print(f"[JOIN] Using pending role: {role_id}")

    # Step 2: Retry loop to detect invite
    if role_id is None:
        current = {}
        for attempt in range(6):
            current = await take_snapshot(guild)
            matched = False
            for code, uses in current.items():
                old_uses = invite_uses.get(code, 0)
                if uses > old_uses and code in invite_map:
                    role_id = invite_map.pop(code)
                    save()
                    matched = True
                    print(f"[JOIN] ✅ Matched invite {code} (uses {old_uses}→{uses}) → role {role_id}")
                    break
            if matched:
                break
            print(f"[JOIN] Attempt {attempt+1}: no match yet, waiting 0.4s...")
            await asyncio.sleep(0.4)

        # Step 3: Fallback — invite deleted (single-use auto delete)
        if role_id is None:
            for code in list(invite_map.keys()):
                if code not in current:
                    role_id = invite_map.pop(code)
                    save()
                    print(f"[JOIN] Fallback: invite {code} gone → role {role_id}")
                    break

        invite_uses.clear()
        invite_uses.update(current)
        print(f"[JOIN] invite_uses (after) = {invite_uses}")

    if role_id is None:
        print(f"[JOIN] ❌ No role found for {member.name}")
        return

    role = guild.get_role(role_id)
    if not role:
        print(f"[JOIN] ❌ Role {role_id} not found in guild")
        return

    try:
        await member.add_roles(role, reason="Auto-assigned via invite")
        print(f"[JOIN] ✅✅ Role '{role.name}' assigned to {member.name}")
    except Exception as e:
        print(f"[JOIN] ❌ ROLE ADD ERROR: {e}")


@bot.command(name='cc')
@commands.has_permissions(manage_channels=True)
async def create_channel(ctx, channel_name: str, role_name: str):
    guild = ctx.guild
    try:
        await ctx.message.delete()
    except:
        pass

    category = ctx.channel.category

    role = discord.utils.get(guild.roles, name=role_name)
    if not role:
        role = await guild.create_role(name=role_name, mentionable=True)
        print(f"[CC] Created role: {role.name} ({role.id})")

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

    channel = await guild.create_text_channel(
        name=channel_name,
        overwrites=overwrites,
        category=category
    )

    invite = await channel.create_invite(max_uses=1, max_age=0, unique=True)

    invite_map[invite.code] = role.id
    invite_uses[invite.code] = 0
    save()

    print(f"[CC] Created invite {invite.code} → role {role.id}")

    msg = (
        f'**:white_check_mark:Done!**\n'
        f'**:pushpin:Channel:** {channel_name}\n'
        f'**:performing_arts:Role:** @{role_name}\n'
        f'**:link:Invite (1-use, never expires):**\n'
        f'{invite.url}'
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


bot.run(TOKEN)
