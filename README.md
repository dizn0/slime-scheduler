# slime scheduler

discord bot for maplestory private server guilds (built for gumo on SlimeTale). schedules boss runs and training parties, tracks availability, keeps a price book, and does a few dumb games.

single file (`bot.py`), python 3.12, discord.py. runs on one 256mb fly.io machine.

## commands

**runs**
- `/expo` - post a boss run. optional day / time dropdowns, then a form for boss, time, size, min level, note. posts a signup card with class buttons, waitlist, reminder 30 min before, auto-lock at start, and a discord scheduled event.
- `/grind` - same for training parties, single join button.
- `/upcoming` - list scheduled runs.
- `/recurring add|list|remove` - runs that post themselves (e.g. weekday zakum at 9pm, card goes up 6h before).
- `/runs-channel set|clear` - (admin) force cards into specific channels.
- host panel on each card: change time, lock, cancel, add someone, auto-cancel rule, post again.

**time**
- `/tz` - set your timezone. required before posting; the bot asks the first time.
- `/when fri 9pm` - paste a timestamp everyone sees in their own zone.
- `/avail`, `/board` - weekly availability grid.

**misc**
- `/price add|lookup|recent|remove|board|merge` - shared price book.
- `/roll [max]` - d(max).
- `/sui [max]` - deathroll. roll 1-N, next person rolls 1-(your roll), whoever hits 1 loses.

## how it stores things

no database. signup state lives in the card embed itself (parsed back out on every click), so a restart loses nothing. small json files on the `/data` volume hold timezones, prices, tracked runs, and recurring schedules.

## running it

```
pip install -r requirements.txt
export DISCORD_TOKEN=... GUILD_ID=... DEFAULT_TZ=America/New_York
python bot.py
```

`GUILD_ID` makes slash commands sync instantly to one server. leave it unset for global (takes up to an hour).

discord side: create an app, bot token, no privileged intents needed. install with scopes `applications.commands` + `bot`, permissions: send messages, embed links, read message history, manage events.

## deploying (fly.io)

```
fly launch --no-deploy --copy-config
fly secrets set DISCORD_TOKEN=... GUILD_ID=... DEFAULT_TZ=America/New_York
fly deploy
fly scale count 1
```

the `/data` volume in `fly.toml` persists the json files across deploys.

## layout of bot.py

roughly top to bottom: config → timezone store → signup card parsing/rebuild → class pick / host panel views → expo & grind modals and the day/time pre-pick → availability board → price book → tracking (reminders, auto-cancel, auto-lock) → recurring → roll / sui → `on_ready`.
