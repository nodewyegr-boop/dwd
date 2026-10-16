import discord
from discord.ext import commands
from discord import ui
import json
import os
import asyncio
import aiohttp
import io
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

DISCORD_TOKEN  = os.getenv('DISCORD_TOKEN')
OWNER_ID       = 1488103702488154173
PRICE_PER_TOKEN = 0.65
PHONE_NUMBER   = '0837751528'
VALAN_URL      = 'https://valan.shop/'
GIF_URL        = (
    'https://cdn.discordapp.com/attachments/1559527131728584766/'
    '1560446530874515526/6414b459c71ca7c53b14798235d412e5.gif'
    '?ex=6ad2be94&is=6ad16d14'
    '&hm=e0bd9ff3c7ca11dbf972427c215c24d8e9c807f3b31848e3b1a9580db935508f&'
)

# Custom emojis from the server (used inside embed text)
E_SELL     = '<a:1000030144:1559280351254151199>'
E_PRICE    = '<a:1000035910:1559173862820810803>'
E_CURRENCY = '<:1000039252:1560441416784674897>'
E_SOLD     = '<a:1000039250:1560440723743182869>'
E_PRODUCT  = '<a:1000039257:1560444683895709797>'
E_RETURN   = '<a:1000039257:1560444683895709797>'
E_DM       = '<a:1000039256:1560444516429996145>'
E_ACCEPT   = '<:1000035915:1559174417097953340>'
E_TICKET   = '<a:1000039255:1560444220764848269>'

DATA_DIR        = 'data'
INVENTORY_FILE  = os.path.join(DATA_DIR, 'inventory.json')
USERS_FILE      = os.path.join(DATA_DIR, 'users.json')
PURCHASES_FILE  = os.path.join(DATA_DIR, 'purchases.json')

# ─────────────────────────────────────────────
# DATA LAYER
# ─────────────────────────────────────────────

def _load(path: str, default):
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return default

def _save(path: str, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# inventory  →  { "items": ["tok1", "tok2", ...], "sold": 0 }
def load_inv():     return _load(INVENTORY_FILE, {'items': [], 'sold': 0})
def save_inv(d):    _save(INVENTORY_FILE, d)

# users  →  { "user_id": { "balance": 0.0, "username": "", "top_ups": [], "purchases": [] } }
def load_users():   return _load(USERS_FILE, {})
def save_users(d):  _save(USERS_FILE, d)

# purchases  →  { "P0001": { "user_id", "username", "items", "qty", "cost", "timestamp" } }
def load_purchases():   return _load(PURCHASES_FILE, {})
def save_purchases(d):  _save(PURCHASES_FILE, d)

def get_user(users: dict, member: discord.Member) -> dict:
    uid = str(member.id)
    if uid not in users:
        users[uid] = {
            'balance': 0.0,
            'username': str(member),
            'top_ups': [],
            'purchases': [],
        }
    else:
        users[uid]['username'] = str(member)
    return users[uid]

# ─────────────────────────────────────────────
# EMBED HELPERS
# ─────────────────────────────────────────────

def ok_embed(msg: str) -> discord.Embed:
    return discord.Embed(description=msg, color=discord.Color.green())

def err_embed(msg: str) -> discord.Embed:
    return discord.Embed(description=msg, color=discord.Color.red())

# ─────────────────────────────────────────────
# TRUEMONEY / VALAN.SHOP
# ─────────────────────────────────────────────

async def redeem_truemoney(voucher_link: str) -> float | None:
    """
    Redeem a TrueMoney Wallet envelope via valan.shop.
    POSTs the phone number + voucher link, parses the credited amount.

    If valan.shop changes its API shape, adjust the payload keys or
    response-parsing block below — everything else stays the same.
    """
    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                'phone':   PHONE_NUMBER,
                'voucher': voucher_link,   # adjust key name if valan.shop uses a different one
                'link':    voucher_link,   # fallback key some sites use
            }
            headers = {
                'User-Agent': (
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/124.0 Safari/537.36'
                ),
                'Accept': 'application/json, text/plain, */*',
                'Referer': VALAN_URL,
            }
            timeout = aiohttp.ClientTimeout(total=30)
            async with session.post(
                VALAN_URL, data=payload, headers=headers, timeout=timeout
            ) as resp:
                if resp.status != 200:
                    return None

                # Try JSON first
                try:
                    data = await resp.json(content_type=None)
                    # Common field names across TrueMoney proxy sites
                    for key in ('amount', 'value', 'money', 'credit', 'redeemAmount'):
                        if key in data and isinstance(data[key], (int, float)):
                            return float(data[key])

                    # Nested under data / result
                    for wrapper in ('data', 'result'):
                        if isinstance(data.get(wrapper), dict):
                            for key in ('amount', 'value', 'money'):
                                val = data[wrapper].get(key)
                                if isinstance(val, (int, float)):
                                    return float(val)
                    return None
                except Exception:
                    pass

                # Fallback: look for a bare number in plain-text body
                text = await resp.text()
                import re
                m = re.search(r'(?:amount|value|money)["\s:]+(\d+(?:\.\d+)?)', text, re.I)
                if m:
                    return float(m.group(1))
                return None

    except asyncio.TimeoutError:
        return None
    except Exception as e:
        print(f'[valan.shop] error: {e}')
        return None

# ─────────────────────────────────────────────
# MODALS
# ─────────────────────────────────────────────

class BuyModal(ui.Modal, title='Buy Token'):
    quantity = ui.TextInput(
        label='Quantity (1–999)',
        placeholder='Enter a number between 1 and 999',
        min_length=1,
        max_length=3,
    )

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.quantity.value.strip()

        if not raw.isdigit():
            await interaction.response.send_message(
                embed=err_embed('Please enter numbers only.'), ephemeral=True
            )
            return

        qty = int(raw)
        if not 1 <= qty <= 999:
            await interaction.response.send_message(
                embed=err_embed('Please enter a number between 1 and 999.'), ephemeral=True
            )
            return

        users     = load_users()
        inv       = load_inv()
        purchases = load_purchases()
        user_data = get_user(users, interaction.user)
        total     = round(qty * PRICE_PER_TOKEN, 2)

        # Out of stock
        if len(inv['items']) < qty:
            await interaction.response.send_message(
                embed=err_embed('The item is out of stock. Please inform the admin.'),
                ephemeral=True,
            )
            return

        # Not enough money
        if user_data['balance'] < total:
            await interaction.response.send_message(
                embed=err_embed('Not enough money'), ephemeral=True
            )
            return

        # Take items (FIFO)
        taken        = inv['items'][:qty]
        inv['items'] = inv['items'][qty:]
        inv['sold'] += qty

        # Deduct balance
        user_data['balance'] = round(user_data['balance'] - total, 2)

        # Record purchase
        pid = f"P{len(purchases) + 1:04d}"
        ts  = datetime.now().isoformat()
        purchases[pid] = {
            'user_id':  str(interaction.user.id),
            'username': str(interaction.user),
            'items':    taken,
            'qty':      qty,
            'cost':     total,
            'timestamp': ts,
        }
        user_data['purchases'].append({
            'purchase_id': pid,
            'qty':   qty,
            'cost':  total,
            'timestamp': ts,
        })

        save_inv(inv)
        save_users(users)
        save_purchases(purchases)

        # Try to DM the tokens
        file_content = '\n'.join(taken).encode('utf-8')
        filename     = f"Token {interaction.user.name} {qty}.txt"

        try:
            dm       = await interaction.user.create_dm()
            file_obj = discord.File(io.BytesIO(file_content), filename=filename)
            await dm.send('Token your', file=file_obj)
            await interaction.response.send_message(
                embed=ok_embed('succeed\nThe item is in DM'), ephemeral=True
            )
        except discord.Forbidden:
            # DM failed — roll everything back
            inv2            = load_inv()
            inv2['items']   = taken + inv2['items']
            inv2['sold']   -= qty
            users2          = load_users()
            ud2             = users2.get(str(interaction.user.id), {})
            ud2['balance']  = round(ud2.get('balance', 0.0) + total, 2)
            if ud2.get('purchases'):
                ud2['purchases'].pop()
            purchases2 = load_purchases()
            purchases2.pop(pid, None)
            save_inv(inv2)
            save_users(users2)
            save_purchases(purchases2)

            await interaction.response.send_message(
                embed=err_embed('open your Dm'), ephemeral=True
            )


class TopUpModal(ui.Modal, title='Top Up — TrueMoney Wallet'):
    envelope = ui.TextInput(
        label='TrueMoney Envelope Link',
        placeholder='https://gift.truemoney.com/campaign/?v=...',
        min_length=10,
        max_length=500,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        link   = self.envelope.value.strip()
        amount = await redeem_truemoney(link)

        if not amount or amount <= 0:
            await interaction.followup.send(
                embed=err_embed('Unsuccessful. Please notify the administrator.'),
                ephemeral=True,
            )
            return

        users     = load_users()
        user_data = get_user(users, interaction.user)
        user_data['balance'] = round(user_data['balance'] + amount, 2)
        user_data['top_ups'].append({
            'amount':    amount,
            'link':      link,
            'timestamp': datetime.now().isoformat(),
        })
        save_users(users)

        await interaction.followup.send(
            embed=ok_embed(f'Success You received {amount:.2f} baht'),
            ephemeral=True,
        )

# ─────────────────────────────────────────────
# SELECT MENU  (persistent — custom_id locked)
# ─────────────────────────────────────────────

class SystemSelect(ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label='buy Token',
                value='buy',
                emoji=discord.PartialEmoji(animated=True, name='_', id=1560447458834915449),
                description='Purchase tokens from the shop',
            ),
            discord.SelectOption(
                label='check money',
                value='check_money',
                emoji=discord.PartialEmoji(animated=True, name='_', id=1560447714368422000),
                description='View your current balance',
            ),
            discord.SelectOption(
                label='Top up',
                value='topup',
                emoji=discord.PartialEmoji(animated=False, name='_', id=1559174417097953340),
                description='Top up via TrueMoney Wallet',
            ),
            discord.SelectOption(
                label='Purchase and top-up history',
                value='history',
                emoji=discord.PartialEmoji(animated=True, name='_', id=1560448511588302928),
                description='View your transaction history',
            ),
            discord.SelectOption(
                label='wash',
                value='wash',
                emoji=discord.PartialEmoji(animated=True, name='_', id=1559280351254151199),
                description='wash',
            ),
        ]
        super().__init__(
            custom_id='shop:system_select',   # fixed → survives restarts
            placeholder='system',
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]

        # ── buy Token ──────────────────────────────────
        if value == 'buy':
            await interaction.response.send_modal(BuyModal())

        # ── check money ────────────────────────────────
        elif value == 'check_money':
            users     = load_users()
            user_data = get_user(users, interaction.user)
            save_users(users)

            embed = discord.Embed()
            embed.set_author(
                name=interaction.user.display_name,
                icon_url=interaction.user.display_avatar.url,
            )
            embed.set_thumbnail(url=interaction.user.display_avatar.url)
            embed.add_field(name='Name', value=str(interaction.user),       inline=True)
            embed.add_field(name='ID',   value=str(interaction.user.id),    inline=True)
            embed.add_field(name='Balance',
                            value=f'**{user_data["balance"]:.2f} ฿**',
                            inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)

        # ── Top up ─────────────────────────────────────
        elif value == 'topup':
            await interaction.response.send_modal(TopUpModal())

        # ── history ────────────────────────────────────
        elif value == 'history':
            users     = load_users()
            user_data = get_user(users, interaction.user)

            # Last 10 top-ups
            tops = user_data['top_ups'][-10:]
            tops_txt = '\n'.join(
                f"• {t['amount']:.2f} ฿ — {t['timestamp'][:10]}" for t in tops
            ) or 'No top-up history.'

            # Last 10 purchases
            purs = user_data['purchases'][-10:]
            purs_txt = '\n'.join(
                f"• {p['purchase_id']} — {p['qty']} token(s) — {p['timestamp'][:10]}"
                for p in purs
            ) or 'No purchase history.'

            embed = discord.Embed()
            embed.set_author(
                name=interaction.user.display_name,
                icon_url=interaction.user.display_avatar.url,
            )
            embed.set_thumbnail(url=interaction.user.display_avatar.url)
            embed.add_field(name='Name', value=str(interaction.user),    inline=True)
            embed.add_field(name='ID',   value=str(interaction.user.id), inline=True)
            embed.add_field(name='Top-up History',   value=tops_txt, inline=False)
            embed.add_field(name='Purchase History', value=purs_txt, inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)

        # ── wash ───────────────────────────────────────
        elif value == 'wash':
            await interaction.response.send_message(
                embed=ok_embed('wash succeed'), ephemeral=True
            )


class ShopView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)   # persistent
        self.add_item(SystemSelect())

# ─────────────────────────────────────────────
# BOT
# ─────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.members         = True

bot = commands.Bot(command_prefix='!', intents=intents)

# ─── owner check ──────────────────────────────

def owner_only():
    async def predicate(ctx: commands.Context):
        return ctx.author.id == OWNER_ID
    return commands.check(predicate)

# ─────────────────────────────────────────────
# COMMANDS
# ─────────────────────────────────────────────

@bot.command(name='shop')
@owner_only()
async def cmd_shop(ctx: commands.Context):
    """Post the shop embed with the system select menu."""
    inv = load_inv()

    # No color → no left-border stripe (the "ไม่มีเส้น" look)
    embed = discord.Embed(
        description=(
            f'{E_SELL} **Sell Token**\n\n'
            f'This is an automated shop selling tokens\n\n'
            f'{E_PRICE} Price **0.65** {E_CURRENCY} '
            f'sold **{inv["sold"]}** {E_SOLD} '
            f'Product **{len(inv["items"])}** {E_PRODUCT}\n\n'
            f'{E_RETURN} We do not offer returns after the purchase **In some cases**\n\n'
            f'{E_DM} When purchasing products, please keep your DM open at all times '
            f'because we will send the products to you\n\n'
            f'*The shop token are in Thai*\n\n'
            f'{E_ACCEPT} We only accept transfers via TrueMoney Wallet\n\n'
            f'{E_TICKET} If you have any questions, you can ask us via Ticket'
        )
    )
    embed.set_image(url=GIF_URL)

    await ctx.send(embed=embed, view=ShopView())
    try:
        await ctx.message.delete()
    except Exception:
        pass


@bot.command(name='t')
@owner_only()
async def cmd_add_token(ctx: commands.Context, *, raw: str):
    """
    !t <tokens>
    Add one or more tokens to inventory.  One token per line.
    e.g.:
        !t mt
        my
        mn
    """
    items = [line.strip() for line in raw.splitlines() if line.strip()]
    if not items:
        return

    inv = load_inv()
    inv['items'].extend(items)
    save_inv(inv)

    await ctx.send(
        embed=ok_embed(
            f'Added **{len(items)}** token(s).\n'
            f'Total in stock: **{len(inv["items"])}**'
        )
    )
    try:
        await ctx.message.delete()
    except Exception:
        pass


@bot.command(name='check')
@owner_only()
async def cmd_check(ctx: commands.Context, *, arg: str):
    """
    !check <purchase_id>   →  send the tokens from that purchase as a file
    !check all             →  send the full current inventory as a file
    """
    arg = arg.strip()

    if arg.lower() == 'all':
        inv = load_inv()
        if not inv['items']:
            await ctx.send(embed=err_embed('No items in stock.'))
            return
        lines    = '\n'.join(inv['items'])
        file_obj = discord.File(io.BytesIO(lines.encode('utf-8')), filename='inventory.txt')
        await ctx.send(f'Total in stock: **{len(inv["items"])}**', file=file_obj)
        return

    # Normalize  "1" → "P0001"
    pid = arg.upper()
    if not pid.startswith('P'):
        pid = 'P' + arg.zfill(4)

    purchases = load_purchases()
    if pid not in purchases:
        await ctx.send(embed=err_embed(f'Purchase `{pid}` not found.'))
        return

    p        = purchases[pid]
    content  = '\n'.join(p['items'])
    file_obj = discord.File(
        io.BytesIO(content.encode('utf-8')),
        filename=f'Token_{pid}.txt',
    )
    await ctx.send(
        f'**{pid}** — {p["username"]} — {p["qty"]} token(s) — {p["timestamp"][:10]}',
        file=file_obj,
    )


@bot.command(name='ckeck')       # intentional typo matching the spec
@owner_only()
async def cmd_ckeck(ctx: commands.Context, *, arg: str):
    """Alias: !ckeck all"""
    await cmd_check(ctx, arg=arg)


@bot.command(name='m')
@owner_only()
async def cmd_give_money(ctx: commands.Context, member: discord.Member, amount: float):
    """
    !m @user <amount>
    Add money to a user's balance.
    """
    if amount <= 0:
        await ctx.send(embed=err_embed('Amount must be positive.'))
        return

    users     = load_users()
    user_data = get_user(users, member)
    user_data['balance'] = round(user_data['balance'] + amount, 2)
    save_users(users)

    await ctx.send(
        embed=ok_embed(
            f'Added **{amount:.2f} ฿** to {member.mention}.\n'
            f'New balance: **{user_data["balance"]:.2f} ฿**'
        )
    )
    try:
        await ctx.message.delete()
    except Exception:
        pass

# ─────────────────────────────────────────────
# EVENTS
# ─────────────────────────────────────────────

@bot.event
async def on_ready():
    # Register the persistent view so buttons/selects survive restarts
    bot.add_view(ShopView())
    print(f'[BOT] {bot.user} online | {len(bot.guilds)} guild(s)')


@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.CheckFailure):
        return   # unauthorized user — silent
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=err_embed(f'Missing argument: `{error.param.name}`'))
    elif isinstance(error, (commands.MemberNotFound, commands.BadArgument)):
        await ctx.send(embed=err_embed('Invalid argument.'))
    else:
        print(f'[ERROR] {type(error).__name__}: {error}')


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == '__main__':
    os.makedirs(DATA_DIR, exist_ok=True)
    bot.run(DISCORD_TOKEN)
