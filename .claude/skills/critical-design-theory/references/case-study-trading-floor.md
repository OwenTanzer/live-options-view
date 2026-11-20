# Case study: the trading-floor bot board (live-options-view PR #278)

The worked example for Critical Design Theory. A one-line brief became four mockups, two revisions and a reviewed PR. Edited from Jayden's write-up for Owen and Musef; the gate labels are added to show where each step sits in the method.

## The brief

> A better view of all our bots on the site, so we can see when they enter and exit positions, inspired by how trading firms showed traders' positions on a big central screen in the 80s and 90s. Show examples to choose from before deciding.

## Gate 1: what the data could honestly say

Before any mockup, the existing roster API (`/api/bots`) was inspected. It returned only each bot's *current* positions: no trade history. Any entry/exit view therefore needed today's fills from the backend. Other constraints found up front:

- The roster already polls every 30 s, and live marks come from the shared quote service, so the new views had to reuse both: no new requests.
- There is no start-of-day equity, so "today's P&L" can't be computed honestly; only "P&L since start".
- A high-frequency bot can trade every cycle, so a fills list must be capped, and the cap must be visible (`session_fills_truncated`).
- The site's CSP has no external `font-src`, so Google-hosted pixel fonts can't load; use a fallback stack rather than loosen security.
- Accounts without a live mark already show "pending" rather than a guessed equity. The new views inherit that rule.

## Gate 2: four directions side by side

Four mockups on one canvas, all using the real bot names (some very long, such as "OI Forward Protection + Re-entry") with clearly labelled sample numbers:

- **A**: green-phosphor terminal tiles with a crawling fill tape;
- **B**: a split-flap departures board, one row per bot;
- **C**: an amber-terminal timeline of the session, one lane per bot;
- **D**: a red LED board with a P&L heat grid, counters, a big tape and an entry/exit log.

Real names mattered: an "OI Forward 35% Stop" exiting on a stop reads differently from a placeholder, and the long names showed which layouts broke.

## Revision 1: choosing a texture

> Give A the structure and colour scheme of D but keep the 8-bit ticker tape style. The heat arrangement of D is good for visibility.

Result, "A2": D's layout and palette, A's 8-bit tape, and A's per-tile detail (contract, entry, mark) folded into the heat tiles. Kept: the heat grid and the tape. Dropped: green phosphor and split-flap. Both looked good, but neither said "who's winning, who just moved" as fast.

## Revision 2 (Gates 3 and 4): why both views stay, and one texture

> Maintain C as an optional view; I like the session length for a different perspective. Both are important.
> Make it consistent with the overall style of A, which is the guiding texture.

**Floor (ambient): "What is happening on the floor right now?"** It should feel like standing in the room.

The old floor boards weren't read so much as felt. A trader across the room caught a flicker, a burst, a column changing colour, and knew the market had moved before knowing why. Every choice on the floor works on peripheral vision first:

- **The heat grid is the temperature of the floor.** You see the mood (mostly green, a few deep reds) before any number. Sorted best to worst, it has leaderboard pressure.
- **Bots are seated like traders at desks.** Tiles lead with the bot's name, not its strategy ID, so monitoring becomes watching participants.
- **The tape is the heartbeat.** It runs while there are fills and says "NO FILLS YET THIS SESSION" when there are none; a quiet tape is information, like a quiet pit. It speaks ticker grammar: time, who, verb, contract, price.
- **Light means live.** LED glow, dot-matrix ground and text shadows make it an instrument that emits light. An entry or exit is a shout that fades: the tile border flashes amber or blue for 15 minutes. Nothing else moves, so motion always means something happened.

**Session lanes (temporal): "How did the day unfold?"** It should feel like watching the session clock.

- **Time becomes space.** 09:30–16:00 across, one lane per bot; you see the shape of the day and how long each bot held risk.
- **The "now" line moves right**, the deadline every 0DTE trader feels.
- **The past dims and the present glows.** Same rule as the floor: open positions bright, closed ones faded; a bar from the left edge is a position carried in.
- **Strategy becomes choreography.** Protection and re-entry variants are visible stepping out and back in beside their baseline: an experiment you see rather than compute.

**One texture.** Both views share the palette, fonts, dot-matrix ground, heat scale and, above all, the tape, so switching views is turning your head inside one room. The existing Cards view stays the default because people already depend on it.

## Gate 4: the locked rules

- Amber = entry, blue = exit. Never red/green, which is reserved for P&L. Every colour is paired with ▲/✕ or IN/OUT text.
- The mockups' heat colours were darkened so white tile text stays readable.
- Animation only for fills and recent events. `prefers-reduced-motion` stops it, and the tape is `aria-hidden` with the log as the accessible copy.
- Reuse the 30 s poll and live marks. Entries and exits are classified client-side by replaying each book (flat→open = entry, open→flat = exit, sign flip = exit + entry). The backend adds only display fields, capped at 100 fills per bot.

## Gate 5: mockup to code

- "Today's P&L" in the mockups became "P&L since start" in code; unmarked accounts show "pending" and are left out of totals; closed accounts dim.
- Carried-in positions draw from the session open without an entry marker; truncated fill lists start at the first retained fill rather than pretending to know earlier history.
- Wide display: the whole floor at once, for a second screen. Phone: it becomes the trader's desk terminal. Tiles reflow, the log wraps below, lanes scroll sideways in their own box. Both checked; a stray lanes scrollbar was found and fixed before review.
- Tests covered the fills projection (day boundary, rejected rows, field allow-list, cap), the replay (entry/add/reduce/exit, carry-in, sign flip), rendering (escaping, recency edges, counters, pending, empty session) and the view switch.

## What to repeat

- Check what the data can honestly say before drawing anything.
- Show several clearly different directions side by side, with real names.
- Name one guiding texture and hold every view to it.
- Write down which question each view answers and what feeling it should give.
- From mockup to code, let true labels beat the mockup's numbers.
