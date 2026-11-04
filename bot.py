#!/usr/bin/env python3
"""slime scheduler: expedition / grind scheduler bot for maplestory private servers.

/expo   -> form: boss, time, max players, min level, note -> signup card with role buttons
/grind  -> form: map, time, slots, note -> party card with join button
/tz     -> set your timezone so times you type are interpreted right (default: DEFAULT_TZ)

signups live inside the embed itself, so restarts lose nothing.
"""
import os, re, json, zoneinfo, asyncio, datetime as dt
from typing import Optional
import discord
from discord import app_commands
import dateparser, aiohttp, html as html_mod

TOKEN = os.environ["DISCORD_TOKEN"]
DEFAULT_TZ = os.environ.get("DEFAULT_TZ", "America/New_York")
DATA_DIR = "/data" if os.path.isdir("/data") else "."
TZ_FILE = os.path.join(DATA_DIR, "tz.json")
PRICE_FILE = os.path.join(DATA_DIR, "prices.json")
TRACK_FILE = os.path.join(DATA_DIR, "tracked.json")
RECUR_FILE = os.path.join(DATA_DIR, "recurring.json")
CLASS_FILE = os.path.join(DATA_DIR, "classes.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

def load_settings():
    try: return json.load(open(SETTINGS_FILE))
    except Exception: return {}
def save_settings(d): json.dump(d, open(SETTINGS_FILE, "w"))
def runs_channel_ids(guild_id):
    return load_settings().get("runs_channels", {}).get(str(guild_id), [])

def pick_runs_channel(inter):
    """which runs channel a card typed in inter.channel should land in:
    typed in a runs channel -> that one; else a runs channel in the same category; else the first one set."""
    ids = runs_channel_ids(inter.guild_id) if inter.guild_id else []
    if not ids: return None
    if inter.channel_id in ids: return inter.channel
    chans = [c for c in (inter.guild.get_channel(i) for i in ids) if c]
    if not chans: return None
    cat = getattr(inter.channel, "category_id", None)
    for c in chans:
        if cat and c.category_id == cat: return c
    return chans[0]

TZ_NUDGE = "\n-# ⚠️ you haven't set `/tz`, so the time you typed was read as **{tz}**. if that's not your zone, run `/tz` and use **change time** in the host panel."

async def post_card(inter, embed, view):
    """post a run card. if the server has runs channels set and the command was used elsewhere, the card goes to the
    matching one and the person gets a private link. otherwise it posts right here."""
    nudge = TZ_NUDGE.format(tz=DEFAULT_TZ) if str(inter.user.id) not in load_tz() else ""
    ch = pick_runs_channel(inter)
    if ch and ch.id != inter.channel_id:
        try:
            msg = await ch.send(embed=embed, view=view)
            await inter.response.send_message(f"posted in {ch.mention} → {msg.jump_url}" + nudge, ephemeral=True)
            return msg
        except discord.Forbidden:
            await inter.response.send_message(f"I can't post in {ch.mention} — give me Send Messages + Embed Links there, or `/runs-channel` to change it", ephemeral=True)
            return None
    await inter.response.send_message(embed=embed, view=view)
    msg = await inter.original_response()
    if nudge: await inter.followup.send(nudge.strip(), ephemeral=True)
    return msg
CLASSES = [
    ("Hero", "DPS"),
    ("Paladin", "DPS · Threaten"),
    ("Dark Knight", "HB"),
    ("Bowmaster", "SE"),
    ("Marksman", "SE"),
    ("Night Lord", "DPS · mobility"),
    ("Shadower", "DPS · Smokescreen"),
    ("Buccaneer", "SI · Time Leap"),
    ("Corsair", "DPS"),
    ("Bishop", "HS · Res · Dispel · Holy Shield · Door"),
    ("Priest", "HS · Door · Dispel"),
    ("F/P Mage", "DPS · Poison Mist"),
    ("I/L Mage", "DPS · Elquines"),
    ("Other / Fill", "whatever's needed"),
]
ROLES = [c for c, _ in CLASSES]
GREEN = 0x68C434; GOLD = 0xF4C430; GREY = 0x555555

# ---------- tiny per-user timezone store ----------
def load_tz():
    try: return json.load(open(TZ_FILE))
    except FileNotFoundError: return {}
def save_tz(d): json.dump(d, open(TZ_FILE, "w"))
def user_tz(uid): return load_tz().get(str(uid), DEFAULT_TZ)

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

def parse_time(text, tz):
    t = text.strip()
    # dateparser treats today's own weekday name as next week ("tuesday 8pm" on a tuesday -> +7 days).
    # swap it for "today" and only roll forward a week if that time has already passed.
    same_day = False
    m = re.match(r"^(?:this\s+)?(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b", t, re.I)
    if m:
        try: today = dt.datetime.now(zoneinfo.ZoneInfo(tz)).weekday()
        except Exception: today = dt.datetime.now(zoneinfo.ZoneInfo(DEFAULT_TZ)).weekday()
        if WEEKDAYS[m.group(1).lower()] == today:
            t = "today" + t[m.end():]; same_day = True
    # bare "1am" / "midnight" when that time already passed today makes dateparser wander off to the 1st of the month.
    # pin it to today; the past-check below rolls it to tomorrow.
    elif re.fullmatch(r"(at\s+)?(\d{1,2}(:\d{2})?\s*(am|pm)?|noon|midnight)", t, re.I):
        t = "today " + re.sub(r"^at\s+", "", t, flags=re.I)
    d = dateparser.parse(t, settings={"TIMEZONE": tz, "RETURN_AS_TIMEZONE_AWARE": True,
                                       "PREFER_DATES_FROM": "future"})
    if d is None: return None
    now = dt.datetime.now(dt.timezone.utc)
    if d < now - dt.timedelta(minutes=5):
        if same_day: d += dt.timedelta(days=7)                 # "tuesday 8pm" typed tuesday 9pm -> next tuesday
        elif now - d < dt.timedelta(hours=24): d += dt.timedelta(days=1)   # "9pm" typed at 9:30pm -> tomorrow 9pm
    return d

# ---------- embed <-> state ----------
def meta(embed):
    """host id (from host field), max (from footer 'n/m'), locked (from footer)."""
    host = None
    for f in embed.fields:
        if f.name == "host":
            m = re.search(r"<@!?(\d+)>", f.value); host = m.group(1) if m else None
    m = re.search(r"(\d+)/(\d+)", embed.footer.text or "")
    maxp = int(m.group(2)) if m else 99
    locked = "locked" in (embed.footer.text or "") or "cancelled" in (embed.footer.text or "")
    return host, maxp, locked

def field_map(embed):
    """return {role: [mention,...]} for signup fields."""
    out = {}
    for f in embed.fields:
        name = f.name.split(" (")[0]
        if name in ROLES or name == "Party":
            out[name] = [ln.strip() for ln in f.value.split("\n") if ln.strip() and ln.strip() != "nobody yet"]
    for r in ROLES if embed.title.startswith("⚔") else ["Party"]:
        out.setdefault(r, [])
    return out

WL = "waitlist"
def waitlist(embed):
    """entries on the waitlist field. expo entries look like '<@id> (Bishop)', grind just '<@id>'."""
    for f in embed.fields:
        if f.name.split(" (")[0] == WL:
            return [ln.strip() for ln in f.value.split("\n") if ln.strip()]
    return []

def wl_strip(entry): return re.sub(r"\s*\([^()]+\)\s*$", "", entry)

def wl_role(entry, roles):
    if "Party" in roles: return "Party"
    m = re.search(r"\(([^()]+)\)\s*$", entry)
    r = m.group(1) if m else ""
    return r if r in roles else ("Other / Fill" if "Other / Fill" in roles else next(iter(roles)))

def promote(roles, wl, maxp):
    """move waitlisters into open slots, in order. returns [(mention, role)] of who got in."""
    moved = []
    while wl and sum(len(v) for v in roles.values()) < maxp:
        e = wl.pop(0); r = wl_role(e, roles)
        roles.setdefault(r, []).append(wl_strip(e)); moved.append((wl_strip(e), r))
    return moved

async def announce_promoted(channel, embed, moved):
    for m, r in moved:
        await channel.send(f"🎟 {m} a spot opened in **{embed.title}**, you're in" + (f" as **{r}**" if r != "Party" else "") + f" · <t:{card_ts(embed)}:R>")

def card_ts(embed):
    m = re.search(r"<t:(\d+):F>", embed.description or ""); return int(m.group(1)) if m else 0

def rebuild(embed, roles, maxp, wl=None):
    """rewrite the signup fields in place. wl=None keeps whatever waitlist is already on the card."""
    if wl is None: wl = waitlist(embed)
    keep = [f for f in embed.fields if f.name.split(" (")[0] not in roles and f.name != "signups" and f.name.split(" (")[0] != WL]
    embed.clear_fields()
    for f in keep: embed.add_field(name=f.name, value=f.value, inline=f.inline)
    total = sum(len(v) for v in roles.values())
    shown = False
    for r, members in roles.items():
        if members:
            embed.add_field(name=f"{r} ({len(members)})", value="\n".join(members), inline=True); shown = True
    if not shown:
        embed.add_field(name="signups", value="nobody yet", inline=False)
    if wl:
        embed.add_field(name=f"{WL} ({len(wl)})", value="\n".join(wl), inline=False)
    locked = "locked" in (embed.footer.text or "")
    embed.set_footer(text=f"{total}/{maxp} signed up" + (" · locked" if locked else ""))
    return embed

async def toggle(inter: discord.Interaction, role: str, deferred=False, label=None, message=None):
    """returns 'joined' | 'waitlisted' | 'left' | 'locked' | 'none'."""
    send = (lambda msg: inter.followup.send(msg, ephemeral=True)) if deferred else (lambda msg: inter.response.send_message(msg, ephemeral=True))
    message = message or inter.message
    embed = message.embeds[0]
    host, maxp, locked = meta(embed)
    if locked: await send("this one is locked"); return "locked"
    roles = field_map(embed); wl = waitlist(embed)
    me = inter.user.mention
    mine = lambda x: x == me or x.startswith(me + " ")
    was_in = None
    for r, v in roles.items():
        for x in list(v):
            if mine(x): was_in = r; v.remove(x)
    was_wl = any(mine(x) for x in wl)
    wl = [x for x in wl if not mine(x)]
    status = "left" if (was_in or was_wl) else "none"
    if role != "leave" and role != was_in:          # clicking your own role again = leave
        if sum(len(v) for v in roles.values()) >= maxp:
            wl.append(me if role == "Party" else f"{me} ({role})"); status = "waitlisted"
        else:
            roles.setdefault(role, []).append(label or me); status = "joined"
    moved = promote(roles, wl, maxp) if status == "left" else []
    rebuild(embed, roles, maxp, wl)
    if deferred or message is not inter.message: await message.edit(embed=embed)
    else: await inter.response.edit_message(embed=embed)
    if status == "waitlisted" and not deferred:
        note = f"full ({maxp}/{maxp}) — you're **#{len(wl)}** on the waitlist. you'll be moved in and pinged if a spot opens."
        if inter.response.is_done(): await inter.followup.send(note, ephemeral=True)
        else: await inter.response.send_message(note, ephemeral=True)
    if moved: await announce_promoted(message.channel, embed, moved)
    return status

CLASS_ALIASES = {"dk": "Dark Knight", "drk": "Dark Knight", "bm": "Bowmaster", "mm": "Marksman", "nl": "Night Lord",
                 "shad": "Shadower", "bucc": "Buccaneer", "sair": "Corsair", "bish": "Bishop", "fp": "F/P Mage", "il": "I/L Mage",
                 "pally": "Paladin", "pala": "Paladin", "fill": "Other / Fill", "other": "Other / Fill", "party": "Party"}
def resolve_class(text, roles):
    t = text.strip().lower()
    if not t: return None
    if t in CLASS_ALIASES and CLASS_ALIASES[t] in roles: return CLASS_ALIASES[t]
    for r in roles:
        if r.lower() == t or r.lower().startswith(t) or t.replace(" ", "") == r.lower().replace(" ", "").replace("/", ""):
            return r
    return None

class AddModal(discord.ui.Modal, title="add / remove someone"):
    who = discord.ui.TextInput(label="IGN or @mention", placeholder="e.g. Dizno", max_length=40)
    cls = discord.ui.TextInput(label="class", placeholder="dk, bish, nl, bucc, sair, bm, mm, shad, hero, pally...", max_length=30)
    def __init__(self, message): super().__init__(); self.message = message
    async def on_submit(self, inter: discord.Interaction):
        embed = self.message.embeds[0]
        host, maxp, locked = meta(embed)
        roles = field_map(embed)
        who = self.who.value.strip()
        # drop existing entry for this name (case-insensitive)
        for v in roles.values():
            for x in list(v):
                if x.lower() == who.lower(): v.remove(x)
        if self.cls.value.strip():
            role = "Party" if "Party" in roles else resolve_class(self.cls.value, list(roles.keys()))
            if not role: return await inter.response.send_message(f"don't know class `{self.cls.value}`. try dk / bish / nl / bucc / sair / bm / mm / shad / hero / pally / fp / il / fill", ephemeral=True)
            if sum(len(v) for v in roles.values()) >= maxp:
                return await inter.response.send_message(f"full ({maxp}/{maxp})", ephemeral=True)
            roles.setdefault(role, []).append(who)
            msg = f"added **{who}** as {role}"
        else:
            msg = f"removed **{who}**"  # unreachable now that class is required; kept for safety
        rebuild(embed, roles, maxp)
        await self.message.edit(embed=embed)
        await inter.response.send_message(msg, ephemeral=True)

async def host_add(inter: discord.Interaction):
    embed = inter.message.embeds[0]
    host, maxp, locked = meta(embed)
    if str(inter.user.id) != host and not inter.user.guild_permissions.manage_messages:
        return await inter.response.send_message("only the host can add people", ephemeral=True)
    await inter.response.send_modal(AddModal(inter.message))

def load_classes():
    try: return json.load(open(CLASS_FILE))
    except FileNotFoundError: return {}
def remember_class(uid, cls):
    d = load_classes(); d[str(uid)] = cls; json.dump(d, open(CLASS_FILE, "w"))
def last_class(uid): return load_classes().get(str(uid))

class ClassPick(discord.ui.View):
    """private class dropdown. remembers the pick for next time."""
    def __init__(self, card, current=None):
        super().__init__(timeout=120); self.card = card
        sel = discord.ui.Select(placeholder="pick your class", options=[discord.SelectOption(label=c, description=d, value=c, default=(c == current)) for c, d in CLASSES])
        async def cb(i):
            await i.response.defer()
            remember_class(i.user.id, sel.values[0])
            st = await toggle(i, sel.values[0], deferred=True, message=self.card)
            if st == "waitlisted": txt = f"run is full — you're on the **waitlist** as **{sel.values[0]}**. you'll be moved in and pinged if a spot opens."
            else: txt = f"signed up as **{sel.values[0]}**. I'll remember that for next time."
            await i.edit_original_response(content=txt, view=None)
        sel.callback = cb; self.add_item(sel)

class ChangeClass(discord.ui.View):
    def __init__(self, card, current):
        super().__init__(timeout=120); self.card = card; self.current = current
    @discord.ui.button(label="change class", style=discord.ButtonStyle.secondary)
    async def b(self, inter, btn):
        await inter.response.edit_message(content="pick a class:", view=ClassPick(self.card, self.current))

async def signup(inter: discord.Interaction):
    embed = inter.message.embeds[0]
    host, maxp, locked = meta(embed)
    if locked: return await inter.response.send_message("this one is locked", ephemeral=True)
    cls = last_class(inter.user.id)
    if cls and cls in ROLES:
        roles = field_map(embed); me = inter.user.mention
        mine = lambda x: x == me or x.startswith(me + " ")
        already = any(mine(x) for v in roles.values() for x in v)
        on_wl = next((x for x in waitlist(embed) if mine(x)), None)
        if not already and not on_wl:
            await inter.response.defer()
            st = await toggle(inter, cls, deferred=True, message=inter.message)
            if st == "waitlisted":
                return await inter.followup.send(f"run is full — you're on the **waitlist** as **{cls}** (your last pick). you'll be moved in and pinged if a spot opens. not right?", view=ChangeClass(inter.message, cls), ephemeral=True)
            return await inter.followup.send(f"signed up as **{cls}** (your last pick). not right?", view=ChangeClass(inter.message, cls), ephemeral=True)
        if on_wl:
            wc = wl_role(on_wl, roles)
            return await inter.response.send_message(f"you're on the **waitlist** for this run as **{wc}**. hit leave to drop off it.", view=ChangeClass(inter.message, wc), ephemeral=True)
        return await inter.response.send_message(f"you're already on this run as **{cls}**.", view=ChangeClass(inter.message, cls), ephemeral=True)
    await inter.response.send_message("pick a class:", view=ClassPick(inter.message), ephemeral=True)

class ExpoView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="sign up", style=discord.ButtonStyle.success, custom_id="expo:signup")
    async def b1(self, i, b): await signup(i)
    @discord.ui.button(label="leave", style=discord.ButtonStyle.secondary, custom_id="expo:leave")
    async def b2(self, i, b): await toggle(i, "leave")
    @discord.ui.button(label="host", style=discord.ButtonStyle.secondary, custom_id="expo:host")
    async def b3(self, i, b): await host_panel(i)

class GrindView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="join", style=discord.ButtonStyle.success, custom_id="grind:Party")
    async def b1(self, i, b): await toggle(i, "Party")
    @discord.ui.button(label="leave", style=discord.ButtonStyle.secondary, custom_id="grind:leave")
    async def b2(self, i, b): await toggle(i, "leave")
    @discord.ui.button(label="host", style=discord.ButtonStyle.secondary, custom_id="grind:host")
    async def b3(self, i, b): await host_panel(i)

async def close(inter: discord.Interaction):
    embed = inter.message.embeds[0]
    host, maxp, locked = meta(embed)
    if str(inter.user.id) != host and not inter.user.guild_permissions.manage_messages:
        return await inter.response.send_message("only the host can lock this", ephemeral=True)
    await inter.response.defer()
    embed.set_footer(text=(embed.footer.text or "") + " · locked")
    embed.color = GREY
    roles = field_map(embed)
    everyone = [m.split(" ")[0] for v in roles.values() for m in v if m.startswith("<@")]
    await inter.message.edit(embed=embed, view=AgainView())
    if everyone:
        await inter.followup.send(f"**{embed.title}** is locked in, see you there {' '.join(everyone)}")

async def cancel(inter: discord.Interaction):
    embed = inter.message.embeds[0]
    host, maxp, locked = meta(embed)
    if str(inter.user.id) != host and not inter.user.guild_permissions.manage_messages:
        return await inter.response.send_message("only the host can cancel this", ephemeral=True)
    await inter.response.defer()
    roles = field_map(embed)
    everyone = [m.split(" ")[0] for v in list(roles.values()) + [waitlist(embed)] for m in v if m.startswith("<@") and not m.startswith(inter.user.mention)]
    # remove the linked scheduled event, if any
    m = re.search(r"discord\.com/events/\d+/(\d+)", embed.description or "")
    if m:
        try:
            ev = await inter.guild.fetch_scheduled_event(int(m.group(1)))
            await ev.delete(reason=f"cancelled by {inter.user}")
        except Exception as ex: print("event delete skipped:", ex)
    embed.title = "❌ cancelled · " + embed.title
    embed.description = re.sub(r"\n\[📅[^\n]*", "", embed.description or "")
    embed.color = GREY
    embed.set_footer(text="cancelled · " + (embed.footer.text or ""))
    untrack(inter.message.id)
    await inter.message.edit(embed=embed, view=AgainView())
    if everyone:
        await inter.followup.send(f"**{embed.title.replace('❌ cancelled · ', '')}** is cancelled, sorry {' '.join(everyone)}")

class RetimeModal(discord.ui.Modal, title="change time"):
    when = discord.ui.TextInput(label="New time?", placeholder="friday 9pm · tomorrow 8:30pm · in 1h", max_length=60)
    def __init__(self, card): super().__init__(); self.card = card
    async def on_submit(self, inter: discord.Interaction):
        embed = self.card.embeds[0]
        host, maxp, locked = meta(embed)
        if locked: return await inter.response.send_message("this one is locked / cancelled, can't move it", ephemeral=True)
        when = parse_time(self.when.value, user_tz(inter.user.id))
        if not when:
            return await inter.response.send_message(f"couldn't understand `{self.when.value}`. try something like `friday 8pm` or `tomorrow 9:30pm`", ephemeral=True)
        if when < dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5):
            return await inter.response.send_message(f"<t:{int(when.timestamp())}:F> is in the past", ephemeral=True)
        ts = int(when.timestamp())
        await inter.response.defer()                      # calendar edit + card edit can take >3s; keep the interaction alive
        old = re.search(r"<t:(\d+):F>", embed.description or "")
        embed.description = re.sub(r"\*\*<t:\d+:F>\*\*  ·  <t:\d+:R>", f"**<t:{ts}:F>**  ·  <t:{ts}:R>", embed.description or "", count=1)
        # keep reminders / auto-lock in sync
        d = load_tracked(); t = d.get(str(self.card.id))
        if t: t["when"] = ts; t["reminded"] = False; save_tracked(d)
        # move the linked calendar event too
        m = re.search(r"discord\.com/events/\d+/(\d+)", embed.description or "")
        if m:
            try:
                ev = await inter.guild.fetch_scheduled_event(int(m.group(1)))
                await ev.edit(start_time=when, end_time=when + dt.timedelta(hours=2), reason=f"retimed by {inter.user}")
            except Exception as ex: print("event edit skipped:", ex)
        await self.card.edit(embed=embed)
        roles = field_map(embed)
        people = [x.split(" ")[0] for v in list(roles.values()) + [waitlist(embed)] for x in v if x.startswith("<@") and not x.startswith(inter.user.mention)]
        was = f" (was <t:{old.group(1)}:f>)" if old else ""
        await inter.followup.send(f"⏰ **{embed.title}** moved to **<t:{ts}:F>** · <t:{ts}:R>{was} {' '.join(people)}")

class RemoveView(discord.ui.View):
    """host-only dropdown of current signups (and waitlist); pick one to remove them."""
    def __init__(self, card, opts):
        super().__init__(timeout=120); self.card = card
        sel = discord.ui.Select(placeholder="who to remove", options=opts, min_values=1, max_values=1)
        async def cb(i):
            embed = self.card.embeds[0]
            host, maxp, locked = meta(embed)
            if locked: return await i.response.edit_message(content="this one is locked", view=None)
            roles = field_map(embed); wl = waitlist(embed); gone = None
            for r, v in roles.items():
                for x in list(v):
                    if x == sel.values[0]: v.remove(x); gone = (x, r)
            if not gone and sel.values[0] in wl:
                wl.remove(sel.values[0]); gone = (sel.values[0], WL)
            if not gone: return await i.response.edit_message(content="they're not on the card anymore", view=None)
            moved = promote(roles, wl, maxp)
            rebuild(embed, roles, maxp, wl)
            await self.card.edit(embed=embed)
            await i.response.edit_message(content=f"removed {gone[0]} ({gone[1]})", view=None)
            if moved: await announce_promoted(self.card.channel, embed, moved)
        sel.callback = cb; self.add_item(sel)

async def host_remove(inter: discord.Interaction, card):
    embed = card.embeds[0]; roles = field_map(embed); opts = []
    entries = [(x, r) for r, v in roles.items() for x in v] + [(x, WL) for x in waitlist(embed)]
    for x, r in entries[:25]:
        m = re.match(r"<@!?(\d+)>", x)
        if m:
            uid = int(m.group(1)); mem = inter.guild.get_member(uid)
            if not mem:
                try: mem = await inter.guild.fetch_member(uid)
                except Exception: mem = None
            name = mem.display_name if mem else x
            if r == WL: name += " · " + wl_role(x, roles)
        else: name = x
        opts.append(discord.SelectOption(label=name[:100], description=r[:100], value=x[:100]))
    if not opts: return await inter.response.send_message("nobody signed up yet", ephemeral=True)
    await inter.response.send_message("pick who to remove:", view=RemoveView(card, opts), ephemeral=True)

class SizeModal(discord.ui.Modal, title="change size"):
    size = discord.ui.TextInput(label="How many people?", placeholder="12", max_length=3)
    def __init__(self, card): super().__init__(); self.card = card; self.size.default = str(meta(card.embeds[0])[1])
    async def on_submit(self, inter: discord.Interaction):
        embed = self.card.embeds[0]
        host, maxp, locked = meta(embed)
        if locked: return await inter.response.send_message("this one is locked / cancelled", ephemeral=True)
        try: new = int(self.size.value)
        except ValueError: return await inter.response.send_message("needs to be a number, like 12", ephemeral=True)
        roles = field_map(embed); wl = waitlist(embed)
        cap = 6 if "Party" in roles else 99
        new = max(1, min(cap, new))
        count = sum(len(v) for v in roles.values())
        if new < count:
            return await inter.response.send_message(f"{count} already signed up — remove people first if you want it smaller than that", ephemeral=True)
        moved = promote(roles, wl, new)
        rebuild(embed, roles, new, wl)
        await self.card.edit(embed=embed)
        await inter.response.send_message(f"size is now **{new}** (was {maxp})" + (f", moved {len(moved)} in from the waitlist" if moved else ""), ephemeral=True)
        if moved: await announce_promoted(self.card.channel, embed, moved)

class HandoffView(discord.ui.View):
    """pick a new host. host field on the card is the source of truth for permissions."""
    def __init__(self, card):
        super().__init__(timeout=120); self.card = card
        sel = discord.ui.UserSelect(placeholder="who's hosting now?", min_values=1, max_values=1)
        async def cb(i):
            new = sel.values[0]
            embed = self.card.embeds[0]
            host, maxp, locked = meta(embed)
            if locked: return await i.response.edit_message(content="this one is locked / cancelled", view=None)
            if new.bot: return await i.response.edit_message(content="not a bot", view=None)
            fields = [(f.name, f.value, f.inline) for f in embed.fields]
            embed.clear_fields()
            for n, v, inl in fields: embed.add_field(name=n, value=("<@" + str(new.id) + ">" if n == "host" else v), inline=inl)
            await self.card.edit(embed=embed)
            await i.response.edit_message(content=f"{new.mention} is now hosting", view=None)
            await self.card.channel.send(f"👑 {new.mention} is now hosting **{embed.title}** (handed off by {i.user.mention})")
        sel.callback = cb; self.add_item(sel)

class HostPanel(discord.ui.View):
    def __init__(self, card):
        super().__init__(timeout=300); self.card = card
    async def _swap(self, inter):
        # make handlers act on the card, not the ephemeral panel
        inter.message = self.card
    @discord.ui.button(label="lock", style=discord.ButtonStyle.primary)
    async def b1(self, inter, b): inter.message = self.card; await close(inter)
    @discord.ui.button(label="cancel run", style=discord.ButtonStyle.danger)
    async def b2(self, inter, b): inter.message = self.card; await cancel(inter)
    @discord.ui.button(label="change time", style=discord.ButtonStyle.secondary)
    async def b4(self, inter, b): await inter.response.send_modal(RetimeModal(self.card))
    @discord.ui.button(label="change size", style=discord.ButtonStyle.secondary)
    async def b7(self, inter, b): await inter.response.send_modal(SizeModal(self.card))
    @discord.ui.button(label="add someone", style=discord.ButtonStyle.secondary, row=1)
    async def b3(self, inter, b): await inter.response.send_modal(AddModal(self.card))
    @discord.ui.button(label="remove someone", style=discord.ButtonStyle.secondary, row=1)
    async def b6(self, inter, b): await host_remove(inter, self.card)
    @discord.ui.button(label="hand off host", style=discord.ButtonStyle.secondary, row=1)
    async def b8(self, inter, b): await inter.response.send_message("pick the new host:", view=HandoffView(self.card), ephemeral=True)
    @discord.ui.button(label="auto-cancel rule", style=discord.ButtonStyle.secondary, row=2)
    async def b5(self, inter, b): await inter.response.send_modal(AutoCancelModal(self.card))

async def host_panel(inter: discord.Interaction):
    embed = inter.message.embeds[0]
    host, maxp, locked = meta(embed)
    if str(inter.user.id) != host and not inter.user.guild_permissions.manage_messages:
        return await inter.response.send_message("host only. the host is <@" + str(host) + ">", ephemeral=True)
    await inter.response.send_message("host controls for **" + embed.title + "**", view=HostPanel(inter.message), ephemeral=True)

def base_embed(title, when, host, maxp, color, extra):
    ts = int(when.timestamp())
    e = discord.Embed(title=title, color=color,
                      description=f"**<t:{ts}:F>**  ·  <t:{ts}:R>\n-# sign up → pick your class · leave to drop · full? you go on the waitlist\n-# reminder 30 min before · signups lock automatically at start time")
    for k, v in extra:
        if v: e.add_field(name=k, value=v, inline=True)
    e.add_field(name="host", value=host.mention, inline=True)
    e.set_footer(text=f"0/{maxp} signed up")
    return e

async def make_event(guild, who, title, when, note, msg):
    """create a discord scheduled event so runs show in the Events tab. silently skip if bot lacks Manage Events."""
    try:
        start = max(when, dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=2))   # "now" runs: discord rejects start times in the past
        ev = await guild.create_scheduled_event(
            name=title[:100],
            start_time=start,
            end_time=start + dt.timedelta(hours=2),
            entity_type=discord.EntityType.external,
            location=f"#{msg.channel.name}",
            description=(note + "\n\n" if note else "") + f"sign up: {msg.jump_url}",
            privacy_level=discord.PrivacyLevel.guild_only,
            reason=f"{who} via slime scheduler")
        return ev
    except Exception as ex:
        print("event skipped:", ex); return None

# ---------- modals ----------
def card_defaults(embed, tz):
    """pull boss/map, time (same weekday + time, next future week), size, min level, note off an old card for 'post again'."""
    title = embed.title.replace("❌ cancelled · ", "")
    kind = "expo" if title.startswith("⚔") else "grind"
    name = title[1:].strip()
    host, maxp, locked = meta(embed)
    when_txt = ""
    ts = card_ts(embed)
    if ts:
        t = dt.datetime.fromtimestamp(ts, dt.timezone.utc); now = dt.datetime.now(dt.timezone.utc)
        while t < now: t += dt.timedelta(days=7)
        try: loc = t.astimezone(zoneinfo.ZoneInfo(tz))
        except Exception: loc = t.astimezone(zoneinfo.ZoneInfo(DEFAULT_TZ))
        when_txt = f"{loc.strftime('%a %b')} {loc.day} {loc.strftime('%I:%M%p').lstrip('0').lower()}"
    f = {x.name: x.value for x in embed.fields}
    return {"kind": kind, "name": name, "when": when_txt, "max": str(maxp) if maxp != 99 else "",
            "minlvl": f.get("min level", ""), "note": f.get("note", "")}

class AgainView(discord.ui.View):
    """single 'post again' button left on locked / cancelled cards. whoever clicks becomes the new host."""
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="post again", style=discord.ButtonStyle.secondary, emoji="🔁", custom_id="card:again")
    async def b(self, inter: discord.Interaction, btn: discord.ui.Button):
        d = card_defaults(inter.message.embeds[0], user_tz(inter.user.id))
        await inter.response.send_modal(ExpoModal(d) if d["kind"] == "expo" else GrindModal(d))

class ExpoModal(discord.ui.Modal, title="Boss Run"):
    boss = discord.ui.TextInput(label="What boss?", placeholder="Zakum · Horntail · Papulatus · Pianus", max_length=80)
    when = discord.ui.TextInput(label="When?", placeholder="friday 8pm · tomorrow 9:30pm · sat 3pm", max_length=60)
    maxp = discord.ui.TextInput(label="How many people?", placeholder="12", max_length=3)
    minlvl = discord.ui.TextInput(label="Min level (optional)", placeholder="120", max_length=3, required=False)
    note = discord.ui.TextInput(label="Anything else? (optional)", placeholder="bring pots, VC optional", style=discord.TextStyle.paragraph, required=False, max_length=300)
    def __init__(self, d=None):
        super().__init__()
        if d:
            self.boss.default = d["name"][:80]; self.when.default = d["when"][:60]; self.maxp.default = d["max"][:3]
            self.minlvl.default = d["minlvl"][:3]; self.note.default = d["note"][:300]
    async def on_submit(self, inter: discord.Interaction):
        when = parse_time(self.when.value, user_tz(inter.user.id))
        if not when:
            return await inter.response.send_message(f"couldn't understand `{self.when.value}`. try something like `friday 8pm` or `tomorrow 9:30pm`", ephemeral=True)
        if when < dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5):
            return await inter.response.send_message(f"<t:{int(when.timestamp())}:F> is in the past. try `tomorrow 9pm` or a day name", ephemeral=True)
        try: maxp = max(1, int(self.maxp.value))
        except ValueError: return await inter.response.send_message("how many people needs to be a number, like 12", ephemeral=True)
        e = base_embed(f"⚔ {self.boss.value}", when, inter.user, maxp, GOLD,
                       [("min level", self.minlvl.value.strip() or None), ("note", self.note.value.strip() or None)])
        rebuild(e, {r: [] for r in ROLES}, maxp)
        msg = await post_card(inter, e, ExpoView())
        if not msg: return
        track(msg, when)
        ev = await make_event(inter.guild, inter.user, self.boss.value, when, self.note.value.strip(), msg)
        if ev:
            e.description += f"\n[📅 on the events calendar]({ev.url})"
            await msg.edit(embed=e)

class GrindModal(discord.ui.Modal, title="Training Party"):
    where = discord.ui.TextInput(label="Where?", placeholder="Himes · Ulu 2 · Skeles · Petris", max_length=80)
    when = discord.ui.TextInput(label="When?", placeholder="now · 8pm · sat 3pm", max_length=60)
    slots = discord.ui.TextInput(label="Party size (max 6)", placeholder="6", max_length=2)
    note = discord.ui.TextInput(label="Anything else? (optional)", placeholder="lvl 110-140, have HS", style=discord.TextStyle.paragraph, required=False, max_length=300)
    def __init__(self, d=None):
        super().__init__()
        if d:
            self.where.default = d["name"][:80]; self.when.default = d["when"][:60]; self.slots.default = d["max"][:2]
            self.note.default = d["note"][:300]
    async def on_submit(self, inter: discord.Interaction):
        when = parse_time(self.when.value, user_tz(inter.user.id))
        if not when:
            return await inter.response.send_message(f"couldn't understand `{self.when.value}`. try something like `8pm` or `sat 3pm`", ephemeral=True)
        if when < dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5):
            return await inter.response.send_message(f"<t:{int(when.timestamp())}:F> is in the past. try `tomorrow 9pm` or a day name", ephemeral=True)
        try: slots = max(1, min(6, int(self.slots.value)))
        except ValueError: return await inter.response.send_message("party size needs to be a number, like 6", ephemeral=True)
        e = base_embed(f"⛏ {self.where.value}", when, inter.user, slots, GREEN,
                       [("note", self.note.value.strip() or None)])
        rebuild(e, {"Party": [inter.user.mention]}, slots)
        msg = await post_card(inter, e, GrindView())
        if not msg: return
        track(msg, when)
        ev = await make_event(inter.guild, inter.user, f"grind: {self.where.value}", when, self.note.value.strip(), msg)
        if ev:
            e.description += f"\n[📅 on the events calendar]({ev.url})"
            await msg.edit(embed=e)

# ---------- bot ----------
class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default(), heartbeat_timeout=120.0, max_ratelimit_timeout=60.0)
        self.tree = app_commands.CommandTree(self)
    async def on_message(self, m: discord.Message):
        """keep the runs channel cards-only: anything a human types there is removed, with a short self-deleting nudge.
        slash commands aren't messages, so /expo and /grind still work. needs Manage Messages in that channel."""
        if m.author.bot or not m.guild: return
        if m.channel.id not in runs_channel_ids(m.guild.id): return
        try: await m.delete()
        except discord.Forbidden:
            print(f"can't delete in #{m.channel.name}: give me Manage Messages there"); return
        except discord.NotFound: return
        try: await m.channel.send(f"{m.author.mention} this channel is cards only — use `/expo` or `/grind` here, chat elsewhere", delete_after=8)
        except Exception: pass

    async def setup_hook(self):
        self.add_view(ExpoView()); self.add_view(GrindView()); self.add_view(AgainView())
        gid = os.environ.get("GUILD_ID")
        if gid:
            g = discord.Object(id=int(gid))
            self.tree.copy_global_to(guild=g)
            synced = await self.tree.sync(guild=g)
            print(f"synced {len(synced)} commands to guild {gid}: {[c.name for c in synced]}")
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
            print("cleared global commands")
        else:
            synced = await self.tree.sync()
            print(f"synced {len(synced)} global commands: {[c.name for c in synced]}")

bot = Bot()

DAY_OPTS = [("today", "today"), ("tomorrow", "tomorrow")] + [(d, d) for d in ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]]
TIME_OPTS = [(f"{(h % 12) or 12}{'am' if h < 12 else 'pm'}", f"{(h % 12) or 12}{'am' if h < 12 else 'pm'}") for h in range(24)]

class PrePick(discord.ui.View):
    """optional first step for /expo and /grind: day + time dropdowns, one big button. boss/map and anything left blank go in the form."""
    def __init__(self, kind):
        super().__init__(timeout=180); self.kind = kind; self.day = None; self.time = None
        self.ds = discord.ui.Select(placeholder="1. day", min_values=0, max_values=1, row=0, options=[discord.SelectOption(label=a, value=b) for a, b in DAY_OPTS])
        self.ts = discord.ui.Select(placeholder="2. time (your /tz)", min_values=0, max_values=1, row=1, options=[discord.SelectOption(label=a, value=b) for a, b in TIME_OPTS])
        self.ds.callback = self._day; self.ts.callback = self._time
        self.add_item(self.ds); self.add_item(self.ts)
    async def _day(self, i): self.day = self.ds.values[0] if self.ds.values else None; await i.response.defer()
    async def _time(self, i): self.time = self.ts.values[0] if self.ts.values else None; await i.response.defer()
    @discord.ui.button(label="open the form →", style=discord.ButtonStyle.success, row=2)
    async def go(self, inter: discord.Interaction, b):
        when = " ".join(x for x in (self.day, self.time) if x)
        d = {"name": "", "when": when, "max": "", "minlvl": "", "note": ""}
        await inter.response.send_modal(ExpoModal(d) if self.kind == "expo" else GrindModal(d))

class TzGate(discord.ui.View):
    """shown to people who haven't set /tz. picking one saves it, then the normal flow opens."""
    def __init__(self, kind):
        super().__init__(timeout=180); self.kind = kind
        sel = discord.ui.Select(placeholder="where are you?", options=[discord.SelectOption(label=n, value=v) for n, v in TZ_CHOICES])
        async def cb(i):
            d = load_tz(); d[str(i.user.id)] = sel.values[0]; save_tz(d)
            name = next(n for n, v in TZ_CHOICES if v == sel.values[0])
            await i.response.edit_message(content=f"timezone set to **{name}** ✓ (change later with `/tz`). now, what are you running?", view=PrePick(self.kind))
        sel.callback = cb; self.add_item(sel)

async def start_post(inter, kind):
    if str(inter.user.id) not in load_tz():
        return await inter.response.send_message("first time posting — set your timezone so the time you type lands right for everyone:", view=TzGate(kind), ephemeral=True)
    what = "**pick day and time (or skip), then hit the green button.** the boss / map and everything else go in the form."
    await inter.response.send_message(what, view=PrePick(kind), ephemeral=True)

@bot.tree.command(name="expo", description="post a boss run people can sign up for")
async def expo(inter: discord.Interaction): await start_post(inter, "expo")

@bot.tree.command(name="grind", description="post a training party people can join")
async def grind(inter: discord.Interaction): await start_post(inter, "grind")

runs_group = app_commands.Group(name="runs-channel", description="(admin) which channels run cards live in", default_permissions=discord.Permissions(manage_guild=True))

def _bot_ok(ch, guild):
    p = ch.permissions_for(guild.me); return p.send_messages and p.embed_links and p.manage_messages

@runs_group.command(name="add", description="make a channel a runs channel: cards route there, anything else typed there gets removed")
@app_commands.describe(channel="the channel")
async def runs_add(inter: discord.Interaction, channel: discord.TextChannel):
    if not _bot_ok(channel, inter.guild):
        return await inter.response.send_message(f"I need Send Messages + Embed Links + Manage Messages in {channel.mention} first", ephemeral=True)
    d = load_settings(); ids = d.setdefault("runs_channels", {}).setdefault(str(inter.guild_id), [])
    if channel.id not in ids: ids.append(channel.id)
    save_settings(d)
    await inter.response.send_message(f"ok — {channel.mention} is a runs channel. `/expo` typed in it posts there; typed elsewhere it goes to the runs channel in the same category (or the first one if none matches). now: " + ", ".join(f"<#{i}>" for i in ids), ephemeral=True)

@runs_group.command(name="remove", description="stop treating a channel as a runs channel")
async def runs_remove(inter: discord.Interaction, channel: discord.TextChannel):
    d = load_settings(); ids = d.setdefault("runs_channels", {}).setdefault(str(inter.guild_id), [])
    if channel.id in ids: ids.remove(channel.id)
    save_settings(d)
    await inter.response.send_message(f"removed {channel.mention}. now: " + (", ".join(f"<#{i}>" for i in ids) or "none — cards post wherever the command is typed"), ephemeral=True)

@runs_group.command(name="list", description="show the current runs channels")
async def runs_list(inter: discord.Interaction):
    ids = runs_channel_ids(inter.guild_id)
    await inter.response.send_message("runs channels: " + (", ".join(f"<#{i}>" for i in ids) or "none set"), ephemeral=True)

bot.tree.add_command(runs_group)

TZ_CHOICES = [
    ("US Eastern (New York)", "America/New_York"),
    ("US Central (Chicago)", "America/Chicago"),
    ("US Mountain (Denver)", "America/Denver"),
    ("US Pacific (Los Angeles)", "America/Los_Angeles"),
    ("Hawaii", "Pacific/Honolulu"),
    ("Canada Atlantic (Halifax)", "America/Halifax"),
    ("Brazil (Sao Paulo)", "America/Sao_Paulo"),
    ("UK / Ireland (London)", "Europe/London"),
    ("Central Europe (Berlin, Paris)", "Europe/Berlin"),
    ("Eastern Europe (Athens, Kyiv)", "Europe/Kyiv"),
    ("Turkey / Moscow", "Europe/Istanbul"),
    ("India (Kolkata)", "Asia/Kolkata"),
    ("Thailand / Vietnam (Bangkok)", "Asia/Bangkok"),
    ("Singapore / Malaysia", "Asia/Singapore"),
    ("Philippines (Manila)", "Asia/Manila"),
    ("Hong Kong / China", "Asia/Hong_Kong"),
    ("Korea (Seoul)", "Asia/Seoul"),
    ("Japan (Tokyo)", "Asia/Tokyo"),
    ("Australia East (Sydney)", "Australia/Sydney"),
    ("Australia West (Perth)", "Australia/Perth"),
    ("New Zealand (Auckland)", "Pacific/Auckland"),
]

@bot.tree.command(name="tz", description="set your timezone so times you type are read correctly")
@app_commands.describe(zone="pick the one closest to you")
@app_commands.choices(zone=[app_commands.Choice(name=n, value=v) for n, v in TZ_CHOICES])
async def tz(inter: discord.Interaction, zone: app_commands.Choice[str]):
    d = load_tz(); d[str(inter.user.id)] = zone.value; save_tz(d)
    now = dt.datetime.now(zoneinfo.ZoneInfo(zone.value)).strftime("%I:%M %p").lstrip("0")
    await inter.response.send_message(f"timezone set to **{zone.name}**. your local time right now: {now}", ephemeral=True)

@bot.tree.command(name="when", description="show a time that everyone sees in their own timezone")
@app_commands.describe(time="like: friday 8pm, tomorrow 9:30pm")
async def when(inter: discord.Interaction, time: str):
    d = parse_time(time, user_tz(inter.user.id))
    if not d: return await inter.response.send_message("couldn't read that time", ephemeral=True)
    ts = int(d.timestamp())
    await inter.response.send_message(f"<t:{ts}:F> (<t:{ts}:R>)  —  copy: `<t:{ts}:F>`")

@bot.tree.command(name="upcoming", description="list scheduled runs and parties")
async def upcoming(inter: discord.Interaction):
    evs = sorted(inter.guild.scheduled_events, key=lambda e: e.start_time)
    evs = [e for e in evs if e.status in (discord.EventStatus.scheduled, discord.EventStatus.active)]
    if not evs:
        return await inter.response.send_message("nothing scheduled. post one with /expo or /grind", ephemeral=True)
    lines = [f"**{e.name}** · <t:{int(e.start_time.timestamp())}:F> (<t:{int(e.start_time.timestamp())}:R>) · [open]({e.url})" for e in evs[:15]]
    emb = discord.Embed(title="upcoming", description="\n".join(lines), color=GREEN)
    await inter.response.send_message(embed=emb)


# ---------- availability board ----------
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
BLOCKS = [(str(i + 1), h, h + 2) for i, h in enumerate(range(0, 24, 2))]  # 2h blocks, hours in DEFAULT_TZ
BOARD_TITLE = "📅 who's usually on"

def block_label(name, h1, h2):
    """label a block with timestamps so each reader sees their own local hours."""
    base = dt.datetime.now(zoneinfo.ZoneInfo(DEFAULT_TZ)).replace(hour=0, minute=0, second=0, microsecond=0)
    a = int((base + dt.timedelta(hours=h1)).timestamp()); b = int((base + dt.timedelta(hours=h2)).timestamp())
    return f"`{name}` <t:{a}:t>–<t:{b}:t>"

BLOCK_IDS = [b[0] for b in BLOCKS]

def parse_board(embed):
    """{user_mention: {day: set(blocks)}} from 'who' fields. line format: <@id> mon:10,11 sat:5,6"""
    out = {}
    for f in embed.fields:
        if not f.name.startswith("who"): continue
        for line in f.value.split("\n"):
            m = re.match(r"(<@!?\d+>)\s+(.*)", line.strip())
            if not m: continue
            sched = {}
            for tok in m.group(2).split():
                if ":" not in tok: continue
                d, bl = tok.split(":", 1)
                blocks = set(x for x in bl.split(",") if x in BLOCK_IDS)
                if d in DAYS and blocks: sched[d] = blocks
            if sched: out[m.group(1)] = sched
    return out

def fmt_sched(sched):
    return " ".join(f"{d}:{','.join(sorted(sched[d], key=BLOCK_IDS.index))}" for d in DAYS if d in sched)

def render_board(data):
    e = discord.Embed(title=BOARD_TITLE, color=GREEN)
    e.description = ("set yours with `/avail` · pick a run time where the numbers are highest\n"
                     + "\n".join("   ".join(block_label(*BLOCKS[i + j]) for j in range(3) if i + j < len(BLOCKS)) for i in range(0, len(BLOCKS), 3)))
    hdr = "    " + "".join(f"{n:>3}" for n, _, _ in BLOCKS)
    rows = [hdr]; best = []
    for d in DAYS:
        cells = []
        for n, _, _ in BLOCKS:
            c = sum(1 for sched in data.values() if n in sched.get(d, ()))
            cells.append(c); best.append((c, d, n))
        rows.append(f"{d:<4}" + "".join(f"{c:>3}" for c in cells))
    e.add_field(name="people free", value="```\n" + "\n".join(rows) + "\n```", inline=False)
    best = [b for b in sorted(best, key=lambda x: (-x[0], DAYS.index(x[1]), BLOCK_IDS.index(x[2]))) if b[0] > 0][:3]
    if best:
        lines = []
        for c, d, n in best:
            who = [u for u, sched in data.items() if n in sched.get(d, ())]
            lines.append(f"**{d} block {n}** ({c}): " + " ".join(who))
        e.add_field(name="best windows", value="\n".join(lines), inline=False)
    lines = [f"{u} {fmt_sched(sched)}" for u, sched in data.items()]
    chunk, i = [], 1
    for ln in lines:
        if sum(len(x) + 1 for x in chunk) + len(ln) > 1000:
            e.add_field(name=f"who ({i})", value="\n".join(chunk), inline=False); chunk, i = [], i + 1
        chunk.append(ln)
    e.add_field(name=f"who ({i})" if i > 1 else "who", value="\n".join(chunk) or "nobody yet", inline=False)
    return e

def _is_board(m, me):
    return m.author == me and m.embeds and m.embeds[0].title == BOARD_TITLE

_board_cache = {}
def board_channel(inter):
    cid = os.environ.get("BOARD_CHANNEL_ID")
    if cid:
        ch = inter.guild.get_channel(int(cid))
        if ch: return ch
    return inter.channel

async def find_board(channel):
    me = channel.guild.me
    mid = _board_cache.get(channel.id)
    if mid:
        try: return await channel.fetch_message(mid)
        except Exception: _board_cache.pop(channel.id, None)
    try:
        for m in await channel.pins():
            if _is_board(m, me): _board_cache[channel.id] = m.id; return m
    except Exception as ex:
        print("pins unavailable:", ex)
    async for m in channel.history(limit=300):
        if _is_board(m, me):
            _board_cache[channel.id] = m.id; return m
    return None

def local_block_hours(name, h1, h2, tzname):
    base = dt.datetime.now(zoneinfo.ZoneInfo(DEFAULT_TZ)).replace(hour=0, minute=0, second=0, microsecond=0)
    z = zoneinfo.ZoneInfo(tzname)
    a = (base + dt.timedelta(hours=h1)).astimezone(z); b = (base + dt.timedelta(hours=h2)).astimezone(z)
    f = lambda t: t.strftime("%I:%M %p").lstrip("0")
    day_note = "" if a.date() == base.date() else (" (next day)" if a.date() > base.date() else " (prev day)")
    return f"{f(a)} – {f(b)}{day_note}"

def sched_text(sched):
    if not sched: return "nothing set yet"
    return "\n".join(f"**{d}**: blocks {', '.join(sorted(sched[d], key=BLOCK_IDS.index))}" for d in DAYS if d in sched)

class AvailView(discord.ui.View):
    def __init__(self, sched, tzname):
        super().__init__(timeout=600)
        self.sched = {d: set(b) for d, b in sched.items()}
        self.day = None; self.blocks = set()
        self.ds = discord.ui.Select(placeholder="1. pick a day", min_values=1, max_values=1,
                                    options=[discord.SelectOption(label=d, value=d) for d in DAYS])
        self.bs = discord.ui.Select(placeholder="2. pick the times you're on that day", min_values=0, max_values=len(BLOCKS),
                                    options=[discord.SelectOption(label=f"block {n}", value=n, description=local_block_hours(n, a, b, tzname)) for n, a, b in BLOCKS])
        async def on_day(i):
            self.day = self.ds.values[0]; self.blocks = set(self.sched.get(self.day, ()))
            await i.response.defer()
        async def on_blocks(i): self.blocks = set(self.bs.values); await i.response.defer()
        self.ds.callback = on_day; self.bs.callback = on_blocks
        self.add_item(self.ds); self.add_item(self.bs)

    def content(self, msg=""):
        return (msg + "\n\n" if msg else "") + "**your schedule**\n" + sched_text(self.sched) + "\n-# pick a day, pick its times, hit set day. repeat. then done."

    @discord.ui.button(label="3. set day", style=discord.ButtonStyle.primary, row=2)
    async def setday(self, inter: discord.Interaction, b):
        if not self.day: return await inter.response.send_message("pick a day first", ephemeral=True)
        if self.blocks: self.sched[self.day] = set(self.blocks)
        else: self.sched.pop(self.day, None)
        await inter.response.edit_message(content=self.content(f"{self.day} set"), view=self)

    @discord.ui.button(label="clear all", style=discord.ButtonStyle.secondary, row=2)
    async def clearall(self, inter: discord.Interaction, b):
        self.sched = {}
        await inter.response.edit_message(content=self.content("cleared"), view=self)

    @discord.ui.button(label="done", style=discord.ButtonStyle.success, row=2)
    async def done(self, inter: discord.Interaction, b):
        await inter.response.defer()
        ch = board_channel(inter)
        board = await find_board(ch)
        data = parse_board(board.embeds[0]) if board else {}
        if self.sched: data[inter.user.mention] = self.sched
        else: data.pop(inter.user.mention, None)
        e = render_board(data)
        if board: await board.edit(embed=e)
        else:
            m = await ch.send(embed=e); _board_cache[ch.id] = m.id; board = m
            try: await m.pin()
            except Exception as ex: print("pin failed:", ex)
        await inter.edit_original_response(content=f"saved. board: {board.jump_url}", view=None)

@bot.tree.command(name="avail", description="set when you're usually free so runs get planned around real people")
async def avail(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    board = await find_board(board_channel(inter))
    cur = parse_board(board.embeds[0]).get(inter.user.mention, {}) if board else {}
    tzname = user_tz(inter.user.id)
    note = "" if str(inter.user.id) in load_tz() else f"\n-# times assume **{DEFAULT_TZ}**. run `/tz` first if that's not you."
    v = AvailView(cur, tzname)
    await inter.followup.send(v.content() + note, view=v, ephemeral=True)

@bot.tree.command(name="board", description="show the availability board")
async def board_cmd(inter: discord.Interaction):
    await inter.response.defer()
    board = await find_board(board_channel(inter))
    if not board: return await inter.followup.send("no board yet. run `/avail` to start it.", ephemeral=True)
    await inter.followup.send(f"board: {board.jump_url}", embed=board.embeds[0])



# ---------- tracking: reminders + auto-cancel ----------
def load_tracked():
    try: return json.load(open(TRACK_FILE))
    except FileNotFoundError: return {}
def save_tracked(d): json.dump(d, open(TRACK_FILE, "w"))

def track(msg, when, minp=0, before=15):
    d = load_tracked()
    d[str(msg.id)] = {"ch": msg.channel.id, "when": int(when.timestamp()), "min": minp, "before": before, "reminded": False}
    save_tracked(d)

def untrack(msg_id):
    d = load_tracked(); d.pop(str(msg_id), None); save_tracked(d)

async def auto_cancel(msg, embed, why):
    host, maxp, locked = meta(embed)
    roles = field_map(embed)
    people = [m.split(" ")[0] for v in list(roles.values()) + [waitlist(embed)] for m in v if m.startswith("<@")]
    m = re.search(r"discord\.com/events/\d+/(\d+)", embed.description or "")
    if m:
        try: ev = await msg.guild.fetch_scheduled_event(int(m.group(1))); await ev.delete(reason="auto-cancel: not enough signups")
        except Exception as ex: print("event delete skipped:", ex)
    title = embed.title
    embed.title = "❌ cancelled · " + title
    embed.description = re.sub(r"\n\[📅[^\n]*", "", embed.description or "")
    embed.color = GREY; embed.set_footer(text=f"cancelled · {why} · " + (embed.footer.text or ""))
    await msg.edit(embed=embed, view=AgainView())
    await msg.channel.send(f"**{title}** auto-cancelled: {why}. <@{host}> {' '.join(p for p in people if p != f'<@{host}>')}")

async def tick():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            d = load_tracked(); now = int(dt.datetime.now(dt.timezone.utc).timestamp()); changed = False
            for mid, t in list(d.items()):
                ch = bot.get_channel(t["ch"])
                if not ch: d.pop(mid); changed = True; continue
                try: msg = await ch.fetch_message(int(mid))
                except Exception: d.pop(mid); changed = True; continue
                if not msg.embeds: d.pop(mid); changed = True; continue
                embed = msg.embeds[0]
                host, maxp, locked = meta(embed)
                cancelled = "cancelled" in (embed.footer.text or "")
                if cancelled or now > t["when"] + 3 * 3600:
                    d.pop(mid); changed = True; continue
                roles = field_map(embed); count = sum(len(v) for v in roles.values())
                people = [m.split(" ")[0] for v in roles.values() for m in v if m.startswith("<@")]
                # reminder 30 min before
                if not t.get("reminded") and t["when"] - 30 * 60 <= now < t["when"]:
                    if people: await ch.send(f"**{embed.title}** in <t:{t['when']}:R> {' '.join(people)}")
                    t["reminded"] = True; changed = True
                # auto-cancel at cutoff (only if the host set a minimum)
                if not locked and t.get("min", 0) > 0 and t["when"] - t["before"] * 60 <= now < t["when"] and count < t["min"]:
                    await auto_cancel(msg, embed, f"{count}/{t['min']} needed by {t['before']} min before")
                    d.pop(mid); changed = True; continue
                # auto-lock at start time
                if not locked and now >= t["when"]:
                    embed.set_footer(text=(embed.footer.text or "") + " · locked"); embed.color = GREY
                    await msg.edit(embed=embed, view=AgainView())
                    if people: await ch.send(f"**{embed.title}** is starting, roster locked. {' '.join(people)}")
                    d.pop(mid); changed = True
            if changed: save_tracked(d)
        except Exception as ex:
            print("tick error:", ex)
        try: await post_recurring()
        except Exception as ex: print("recurring error:", ex)
        await asyncio.sleep(60)

class AutoCancelModal(discord.ui.Modal, title="auto-cancel rule"):
    minp = discord.ui.TextInput(label="cancel if fewer than this many signed up (0 = off)", placeholder="0", max_length=2)
    before = discord.ui.TextInput(label="check this many minutes before start", placeholder="30", max_length=3)
    def __init__(self, card): super().__init__(); self.card = card
    async def on_submit(self, inter: discord.Interaction):
        try: minp = max(0, int(self.minp.value)); before = max(0, int(self.before.value))
        except ValueError: return await inter.response.send_message("numbers only", ephemeral=True)
        d = load_tracked(); t = d.get(str(self.card.id))
        if not t: return await inter.response.send_message("this run isn't tracked (posted before the update?)", ephemeral=True)
        t["min"] = minp; t["before"] = before; save_tracked(d)
        await inter.response.send_message(f"ok: cancel if under **{minp}** signed up at **{before}** min before start" if minp else "auto-cancel off for this run", ephemeral=True)

# ---------- price book ----------
def load_prices():
    try: return json.load(open(PRICE_FILE))
    except FileNotFoundError: return {"next": 1, "entries": []}
def save_prices(d): json.dump(d, open(PRICE_FILE, "w"))

def parse_mesos(text):
    t = text.lower().replace(",", "").replace(" ", "").replace("mesos", "").replace("meso", "")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kmb]?)", t)
    if not m: return None
    n = float(m.group(1)); n *= {"": 1, "k": 1e3, "m": 1e6, "b": 1e9}[m.group(2)]
    return int(n)

def fmt_mesos(n):
    if n >= 1e9: return f"{n/1e9:g}b"
    if n >= 1e6: return f"{n/1e6:g}m"
    if n >= 1e3: return f"{n/1e3:g}k"
    return str(n)

ITEM_ALIASES = {
    "cs": "chaos scroll", "chaos": "chaos scroll", "ws": "white scroll", "white": "white scroll",
    "zhelm": "zakum helmet", "zak helm": "zakum helmet", "zakum helm": "zakum helmet", "z helm": "zakum helmet",
    "gfa": "gloves for attack", "gfa 10": "gloves for attack 10%", "gfa 60": "gloves for attack 60%",
    "pac": "pink adventurer cape", "pgc": "pink gaia cape", "tatt": "tattoo", "2att pac": "2 att pac",
    "onyx": "onyx apple", "apple": "onyx apple", "mw20": "maple warrior 20", "mw 20": "maple warrior 20",
    "sfa": "shoes for attack", "ees": "equip enhancement scroll", "htp": "horntail pendant", "hb": "horntail pendant",
    "dep star": "deputy star", "ilbi": "ilbi throwing star", "ilbis": "ilbi throwing star", "ibli": "ilbi throwing star", "iblis": "ilbi throwing star",
    "tobi": "tobi throwing star", "tobis": "tobi throwing star", "steely": "steely throwing knife", "steelies": "steely throwing knife",
    "ss": "stone tooth sword", "fs": "flame shoes",
}
STRIP_WORDS = {"sold", "bought", "seen", "wts", "wtb", "wtt", "for", "at", "a", "an", "the", "x1", "each", "ea"}

def norm(s):
    """canonical grouping key for an item name."""
    t = re.sub(r"[^a-z0-9%/ ]+", " ", s.lower()).strip()
    t = re.sub(r"\s+", " ", t)
    # drop a trailing price ("chaos scroll sold 40", "gfa 60 12m")
    # drop a trailing price only when it's clearly one: has k/m/b, or follows sold/bought/seen/for/at ("genesis 20" stays)
    t = re.sub(r"\s+\d+(?:\.\d+)?\s*[kmb]$", "", t).strip()
    t = re.sub(r"\s+(?:sold|bought|seen|for|at)\s+\d+(?:\.\d+)?$", "", t).strip()
    words = [w for w in t.split() if w not in STRIP_WORDS]
    t = " ".join(words)
    if t in ITEM_ALIASES: t = ITEM_ALIASES[t]
    # singularize simple plurals: scrolls -> scroll, stars -> star (not 'ss', not 'us')
    words = [(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "os", "is")) else w) for w in t.split()]
    t = " ".join(words)
    if t in ITEM_ALIASES: t = ITEM_ALIASES[t]
    return t

def display_name(entries):
    """most common raw spelling in a group, cleaned of strip words / trailing price."""
    names = {}
    for e in entries:
        raw = e["item"].strip()
        raw = re.sub(r"\s+\d+(?:\.\d+)?\s*[kmb]$", "", raw, flags=re.I)
        raw = re.sub(r"\s+(?:sold|bought|seen|for|at)\s+\d+(?:\.\d+)?$", "", raw, flags=re.I)
        raw = " ".join(w for w in raw.split() if w.lower() not in STRIP_WORDS)
        names[raw] = names.get(raw, 0) + 1
    return max(names, key=names.get) if names else entries[0]["item"]

def match_entries(entries, item):
    q = norm(item)
    exact = [e for e in entries if norm(e["item"]) == q]
    return exact or [e for e in entries if q and q in norm(e["item"])]


PRICE_BOARD_TITLE = "💰 price board"
_price_board_cache = {}

def price_channel(inter):
    cid = os.environ.get("PRICE_CHANNEL_ID") or os.environ.get("BOARD_CHANNEL_ID")
    if cid:
        ch = inter.guild.get_channel(int(cid))
        if ch: return ch
    return inter.channel

def item_stats(entries):
    """group entries by normalized item -> (display name, typical, low, high, n, last_ts)"""
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    groups = {}
    for e in entries:
        groups.setdefault(norm(e["item"]), []).append(e)
    out = []
    for k, es in groups.items():
        es.sort(key=lambda e: -e["ts"])
        recent = [e["price"] for e in es if now - e["ts"] < 30 * 86400] or [e["price"] for e in es]
        recent.sort()
        name = display_name(es)
        out.append((name, recent[len(recent) // 2], recent[0], recent[-1], len(es), es[0]["ts"]))
    out.sort(key=lambda x: x[0].lower())
    return out

def render_price_board(entries):
    e = discord.Embed(title=PRICE_BOARD_TITLE, color=GOLD,
                      description="typical = middle price from the last 30 days · `/price check item` for details · `/price add` to log")
    stats = item_stats(entries)
    if not stats:
        e.add_field(name="items", value="nothing yet", inline=False); return e
    lines = []
    for name, mid, lo, hi, n, last in stats:
        rng = f" ({fmt_mesos(lo)}–{fmt_mesos(hi)})" if lo != hi else ""
        lines.append(f"**{name}** · {fmt_mesos(mid)}{rng} · {n}× · <t:{last}:d>")
    chunk, i = [], 1
    for ln in lines:
        if sum(len(x) + 1 for x in chunk) + len(ln) > 1000:
            e.add_field(name=f"items ({i})", value="\n".join(chunk), inline=False); chunk, i = [], i + 1
        chunk.append(ln)
    e.add_field(name=f"items ({i})" if i > 1 else "items", value="\n".join(chunk), inline=False)
    e.set_footer(text=f"{len(entries)} entries · {len(stats)} items")
    return e

def _is_price_board(m, me):
    return m.author == me and m.embeds and m.embeds[0].title == PRICE_BOARD_TITLE

async def find_price_board(channel):
    me = channel.guild.me
    mid = _price_board_cache.get(channel.id)
    if mid:
        try: return await channel.fetch_message(mid)
        except Exception: _price_board_cache.pop(channel.id, None)
    try:
        for m in await channel.pins():
            if _is_price_board(m, me): _price_board_cache[channel.id] = m.id; return m
    except Exception as ex: print("pins unavailable:", ex)
    async for m in channel.history(limit=300):
        if _is_price_board(m, me): _price_board_cache[channel.id] = m.id; return m
    return None

async def update_price_board(inter, entries):
    ch = price_channel(inter)
    board = await find_price_board(ch)
    emb = render_price_board(entries)
    if board:
        await board.edit(embed=emb); return board
    m = await ch.send(embed=emb); _price_board_cache[ch.id] = m.id
    try: await m.pin()
    except Exception as ex: print("pin failed:", ex)
    return m

price = app_commands.Group(name="price", description="shared price book: log what things sell for, look them up")

async def item_ac(inter: discord.Interaction, current: str):
    d = load_prices(); groups = {}
    for e in d["entries"]: groups.setdefault(norm(e["item"]), []).append(e)
    names = sorted({display_name(es) for es in groups.values()}, key=str.lower)
    q = current.lower().strip()
    hits = [n for n in names if q in n.lower() or q in norm(n)] if q else names
    return [app_commands.Choice(name=n[:100], value=n[:100]) for n in hits[:25]]

@price.command(name="add", description="log a price you sold at, bought at, or saw")
@app_commands.describe(item="item name, e.g. zhelm, chaos scroll, 10% oa dex", price="mesos, e.g. 12m, 850k, 1.2b",
                       kind="sold / bought / seen", note="optional: stats, where, who")
@app_commands.choices(kind=[app_commands.Choice(name="sold", value="sold"), app_commands.Choice(name="bought", value="bought"),
                            app_commands.Choice(name="seen (listed / fm)", value="seen")])
@app_commands.autocomplete(item=item_ac)
async def price_add(inter: discord.Interaction, item: str, price: str, kind: app_commands.Choice[str] = None, note: str = None):
    n = parse_mesos(price)
    if n is None: return await inter.response.send_message("couldn't read that price. try `12m`, `850k`, `1.2b`", ephemeral=True)
    d = load_prices()
    e = {"id": d["next"], "item": item.strip(), "price": n, "kind": kind.value if kind else "seen",
         "note": (note or "").strip(), "by": inter.user.id, "ts": int(dt.datetime.now(dt.timezone.utc).timestamp())}
    d["entries"].append(e); d["next"] += 1; save_prices(d)
    await inter.response.send_message(f"logged **{e['item']}** at **{fmt_mesos(n)}** ({e['kind']}) · #{e['id']}")
    try: await update_price_board(inter, d["entries"])
    except Exception as ex: print("price board update failed:", ex)

@price.command(name="check", description="look up recent prices for an item")
@app_commands.describe(item="item name or part of it")
@app_commands.autocomplete(item=item_ac)
async def price_check(inter: discord.Interaction, item: str):
    d = load_prices()
    hits = match_entries(d["entries"], item)
    if not hits: return await inter.response.send_message(f"nothing logged for **{item}** yet. add one with `/price add`", ephemeral=True)
    hits.sort(key=lambda e: -e["ts"])
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    recent = [e["price"] for e in hits if now - e["ts"] < 30 * 86400] or [e["price"] for e in hits]
    recent.sort(); mid = recent[len(recent) // 2]
    names = sorted(set(e["item"] for e in hits), key=str.lower)
    emb = discord.Embed(title=f"💰 {' / '.join(names[:3])}", color=GOLD,
                        description=f"**low** {fmt_mesos(recent[0])} · **typical** {fmt_mesos(mid)} · **high** {fmt_mesos(recent[-1])}\n-# from {len(recent)} entries in the last 30 days")
    lines = [f"<t:{e['ts']}:d> · **{fmt_mesos(e['price'])}** {e['kind']} · <@{e['by']}>" + (f" · {e['note']}" if e['note'] else "") + f" `#{e['id']}`" for e in hits[:10]]
    emb.add_field(name="recent", value="\n".join(lines), inline=False)
    await inter.response.send_message(embed=emb)

@price.command(name="recent", description="latest entries across all items")
@app_commands.describe(count="how many (default 15, max 40)")
async def price_recent(inter: discord.Interaction, count: int = 15):
    d = load_prices()
    if not d["entries"]: return await inter.response.send_message("price book is empty. `/price add` to start it", ephemeral=True)
    ents = sorted(d["entries"], key=lambda e: -e["ts"])[:max(1, min(40, count))]
    lines = [f"<t:{e['ts']}:d> · **{e['item']}** {fmt_mesos(e['price'])} ({e['kind']}) · <@{e['by']}> `#{e['id']}`" for e in ents]
    await inter.response.send_message(embed=discord.Embed(title="💰 recent prices", description="\n".join(lines), color=GOLD))

@price.command(name="remove", description="delete one of your entries by its # id")
@app_commands.describe(id="the #number shown next to the entry")
async def price_remove(inter: discord.Interaction, id: int):
    d = load_prices()
    e = next((x for x in d["entries"] if x["id"] == id), None)
    if not e: return await inter.response.send_message("no entry with that id", ephemeral=True)
    if e["by"] != inter.user.id and not inter.user.guild_permissions.manage_messages:
        return await inter.response.send_message("you can only remove your own entries", ephemeral=True)
    d["entries"].remove(e); save_prices(d)
    await inter.response.send_message(f"removed #{id} ({e['item']} {fmt_mesos(e['price'])})", ephemeral=True)
    try: await update_price_board(inter, d["entries"])
    except Exception as ex: print("price board update failed:", ex)

@price.command(name="board", description="show the price board (one line per item)")
async def price_board_cmd(inter: discord.Interaction):
    await inter.response.defer()
    d = load_prices()
    board = await update_price_board(inter, d["entries"])
    await inter.followup.send(f"board: {board.jump_url}", embed=board.embeds[0])

@price.command(name="merge", description="fold one item name into another (mods)")
@app_commands.describe(from_name="the wrong / duplicate name", into="the name to keep")
@app_commands.autocomplete(from_name=item_ac, into=item_ac)
async def price_merge(inter: discord.Interaction, from_name: str, into: str):
    if not inter.user.guild_permissions.manage_messages:
        return await inter.response.send_message("mods only", ephemeral=True)
    d = load_prices(); a, b = norm(from_name), norm(into); n = 0
    for e in d["entries"]:
        if norm(e["item"]) == a: e["item"] = into.strip(); n += 1
    save_prices(d)
    await inter.response.send_message(f"merged {n} entries into **{into}**", ephemeral=True)
    try: await update_price_board(inter, d["entries"])
    except Exception as ex: print("price board update failed:", ex)

bot.tree.add_command(price)


# ---------- recurring runs: auto-post cards on a schedule ----------
def load_recur():
    try: return json.load(open(RECUR_FILE))
    except FileNotFoundError: return {"next": 1, "items": []}
def save_recur(d): json.dump(d, open(RECUR_FILE, "w"))

DAY_PRESETS = {"weekdays": [0, 1, 2, 3, 4], "daily": [0, 1, 2, 3, 4, 5, 6], "weekends": [5, 6]}

def parse_days(text):
    t = text.strip().lower()
    if t in DAY_PRESETS: return DAY_PRESETS[t]
    out = []
    for tok in re.split(r"[,\s/]+", t):
        if not tok: continue
        k = tok[:3]
        if k not in WEEKDAYS: return None
        if WEEKDAYS[k] not in out: out.append(WEEKDAYS[k])
    return sorted(out) or None

def fmt_days(days):
    for k, v in DAY_PRESETS.items():
        if sorted(days) == v: return k
    return ",".join(DAYS[i] for i in sorted(days))

def parse_clock(text):
    """'8pm', '20:30', '9:15 am' -> (h, m) or None"""
    m = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*", text.lower())
    if not m: return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h < 12: h += 12
    if ap == "am" and h == 12: h = 0
    if h > 23 or mi > 59: return None
    return h, mi

def next_start(item, now):
    """next start datetime (aware) on or after now for this schedule."""
    try: z = zoneinfo.ZoneInfo(item["tz"])
    except Exception: z = zoneinfo.ZoneInfo(DEFAULT_TZ)
    loc = now.astimezone(z)
    for add in range(0, 8):
        day = (loc + dt.timedelta(days=add)).date()
        if day.weekday() not in item["days"]: continue
        cand = dt.datetime.combine(day, dt.time(item["h"], item["m"]), tzinfo=z)
        if cand >= loc: return cand
    return None

async def post_scheduled(item):
    ch = bot.get_channel(item["channel"])
    if not ch: print("recurring: channel gone", item["id"]); return False
    guild = ch.guild
    host = guild.get_member(item["host"])
    if not host:
        try: host = await guild.fetch_member(item["host"])
        except Exception: print("recurring: host gone", item["id"]); return False
    when = dt.datetime.fromtimestamp(item["_next"], dt.timezone.utc)
    if item["kind"] == "expo":
        e = base_embed(f"⚔ {item['name']}", when, host, item["max"], GOLD,
                       [("min level", item.get("minlvl") or None), ("note", item.get("note") or None)])
        rebuild(e, {r: [] for r in ROLES}, item["max"]); view = ExpoView(); title = item["name"]
    else:
        e = base_embed(f"⛏ {item['name']}", when, host, item["max"], GREEN, [("note", item.get("note") or None)])
        rebuild(e, {"Party": []}, item["max"]); view = GrindView(); title = f"grind: {item['name']}"
    e.description += f"\n-# 🔁 recurring · {fmt_days(item['days'])}"
    msg = await ch.send(embed=e, view=view)
    track(msg, when)
    ev = await make_event(guild, host, title, when, item.get("note", ""), msg)
    if ev:
        e.description += f"\n[📅 on the events calendar]({ev.url})"
        await msg.edit(embed=e)
    return True

async def post_recurring():
    d = load_recur(); now = dt.datetime.now(dt.timezone.utc); changed = False
    for item in d["items"]:
        nxt = next_start(item, now)
        if not nxt: continue
        key = nxt.strftime("%Y-%m-%d")
        if item.get("last") == key: continue
        if now < nxt - dt.timedelta(hours=item.get("lead", 6)): continue
        item["_next"] = int(nxt.timestamp())
        try:
            if await post_scheduled(item):
                item["last"] = key; changed = True
        finally:
            item.pop("_next", None)
    if changed: save_recur(d)

recur = app_commands.Group(name="recurring", description="runs that post themselves on a schedule")

@recur.command(name="add", description="auto-post a run card on a schedule (you'll be the host)")
@app_commands.describe(kind="boss run or training party", name="boss or map, e.g. Zakum / Himes", time="start time in your /tz, e.g. 8pm or 20:30",
                       days="weekdays · daily · weekends · or like mon,wed,fri", size="how many people", lead="post the card this many hours before start (default 6)",
                       min_level="expo only, optional", note="optional")
@app_commands.choices(kind=[app_commands.Choice(name="boss run (/expo)", value="expo"), app_commands.Choice(name="training party (/grind)", value="grind")])
async def recur_add(inter: discord.Interaction, kind: app_commands.Choice[str], name: str, time: str, days: str, size: int,
                    lead: int = 6, min_level: Optional[str] = None, note: Optional[str] = None):
    hm = parse_clock(time)
    if not hm: return await inter.response.send_message("couldn't read that time. try `8pm` or `20:30`", ephemeral=True)
    dl = parse_days(days)
    if not dl: return await inter.response.send_message("couldn't read those days. try `weekdays`, `daily`, `weekends`, or `mon,wed,fri`", ephemeral=True)
    size = max(1, min(6 if kind.value == "grind" else 99, size)); lead = max(0, min(72, lead))
    ch = pick_runs_channel(inter) or inter.channel
    d = load_recur()
    item = {"id": d["next"], "kind": kind.value, "name": name.strip()[:80], "h": hm[0], "m": hm[1], "days": dl, "max": size,
            "lead": lead, "minlvl": (min_level or "").strip()[:3], "note": (note or "").strip()[:300],
            "channel": ch.id, "host": inter.user.id, "tz": user_tz(inter.user.id), "last": None}
    d["items"].append(item); d["next"] += 1; save_recur(d)
    nxt = next_start(item, dt.datetime.now(dt.timezone.utc))
    await inter.response.send_message(
        f"ok — **{item['name']}** will post in {ch.mention} every **{fmt_days(dl)}** at **{hm[0]:02d}:{hm[1]:02d}** ({item['tz']}), "
        f"{lead}h before start. first one: <t:{int(nxt.timestamp())}:F>. `#{item['id']}` · `/recurring remove {item['id']}` to stop it", ephemeral=True)

@recur.command(name="list", description="show the recurring runs")
async def recur_list(inter: discord.Interaction):
    d = load_recur(); now = dt.datetime.now(dt.timezone.utc); lines = []
    for i in d["items"]:
        ch = bot.get_channel(i["channel"])
        if ch and ch.guild.id != inter.guild_id: continue
        nxt = next_start(i, now)
        lines.append(f"`#{i['id']}` {'⚔' if i['kind'] == 'expo' else '⛏'} **{i['name']}** · {fmt_days(i['days'])} {i['h']:02d}:{i['m']:02d} ({i['tz']}) · {i['max']}p · <#{i['channel']}> · host <@{i['host']}> · next <t:{int(nxt.timestamp())}:f>")
    if not lines: return await inter.response.send_message("nothing recurring yet. `/recurring add`", ephemeral=True)
    await inter.response.send_message(embed=discord.Embed(title="🔁 recurring runs", description="\n".join(lines), color=GREEN), ephemeral=True)

@recur.command(name="remove", description="stop a recurring run by its # id")
@app_commands.describe(id="the #number from /recurring list")
async def recur_remove(inter: discord.Interaction, id: int):
    d = load_recur(); i = next((x for x in d["items"] if x["id"] == id), None)
    if not i: return await inter.response.send_message("no recurring run with that id", ephemeral=True)
    if i["host"] != inter.user.id and not inter.user.guild_permissions.manage_messages:
        return await inter.response.send_message("only the host (or a mod) can remove it", ephemeral=True)
    d["items"].remove(i); save_recur(d)
    await inter.response.send_message(f"stopped `#{id}` **{i['name']}**. cards already posted stay up.", ephemeral=True)

bot.tree.add_command(recur)

# ---------------- roll / sui ----------------
import random

@bot.tree.command(name="roll", description="roll a number from 1 to max")
@app_commands.describe(max="highest number (default 100)")
async def roll_cmd(inter: discord.Interaction, max: int = 100):
    if max < 2: return await inter.response.send_message("max has to be at least 2", ephemeral=True)
    n = random.randint(1, max)
    await inter.response.send_message(f"🎲 **{inter.user.display_name}** rolled **{n}** (1-{max})")

SUI_COLOR = 0xE74C3C

def sui_embed(g):
    lines = [f"<@{uid}> rolled **{n}** (1-{m})" for uid, n, m in g["log"]]
    e = discord.Embed(title="💀 sui", color=SUI_COLOR,
                      description="\n".join(lines) if lines else f"roll 1-{g['max']} to start")
    if g["over"]:
        e.add_field(name="result", value=f"<@{g['winner']}> hit 1 and wins 🏆", inline=False)
    else:
        e.add_field(name="next", value=f"anyone but <@{g['last']}> rolls 1-{g['cur']}" if g["last"]
                    else f"anyone rolls 1-{g['cur']}", inline=False)
    return e

class SuiView(discord.ui.View):
    def __init__(self, g):
        super().__init__(timeout=900); self.g = g
    @discord.ui.button(label="roll", style=discord.ButtonStyle.danger, emoji="🎲")
    async def do_roll(self, inter: discord.Interaction, button: discord.ui.Button):
        g = self.g
        if g["over"]: return await inter.response.send_message("game's over", ephemeral=True)
        if inter.user.id == g["last"]:
            return await inter.response.send_message("not your turn, someone else has to roll", ephemeral=True)
        n = random.randint(1, g["cur"])
        g["log"].append((inter.user.id, n, g["cur"]))
        g["last"] = inter.user.id
        if n == 1:
            g["over"] = True; g["winner"] = inter.user.id
            button.disabled = True; self.stop()
        else:
            g["cur"] = n
        await inter.response.edit_message(embed=sui_embed(g), view=self)
    async def on_timeout(self):
        for c in self.children: c.disabled = True

@bot.tree.command(name="sui", description="deathroll: roll 1-N, next person rolls 1-(your roll), whoever hits 1 wins")
@app_commands.describe(max="starting max (default 100)")
async def sui_cmd(inter: discord.Interaction, max: int = 100):
    if max < 2: return await inter.response.send_message("max has to be at least 2", ephemeral=True)
    g = {"max": max, "cur": max, "last": None, "log": [], "over": False, "winner": None}
    await inter.response.send_message(embed=sui_embed(g), view=SuiView(g))

@bot.event
async def on_ready():
    print(f"online as {bot.user}")
    if not getattr(bot, "_tick_started", False):
        bot._tick_started = True; bot.loop.create_task(tick())

bot.run(TOKEN)
