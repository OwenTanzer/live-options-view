---
name: critical-design-theory
description: Honesty-first method for designing and critiquing interfaces. Use before mocking up or writing frontend code for a UI view, dashboard, monitor, board, multi-panel tool or data explorer, and when asked to critique or review an existing interface's design. Walks five gates (data audit, divergent mockups, one question per view, one shared texture, mockup-to-code fidelity) and ties every aesthetic choice to what the data can truthfully say. Not for backend-only work, copy edits, or one-line style fixes with no design decision in them.
---

# Critical Design Theory

An interface is an argument about what is true right now. This method makes that argument fast to read without ever letting it lie. Aesthetic choices are not decoration added at the end: each one is chosen because it carries a specific truth to the eye faster than text would, and each one must be dropped the moment it would imply a precision the data does not have.

Walk the five gates in order. Do not skip ahead to layout code. Each gate ends in a short written deliverable; put them in the chat (or the PR/design doc) so the person can see and steer the reasoning, not just the result.

The worked example is [references/case-study-trading-floor.md](references/case-study-trading-floor.md): a trading-floor wall board for paper-trading bots, built from a one-line brief through four mockups to a PR in review. Read it the first time you use this skill.

## Gate 1 — Data and constraint audit, before any drawing

Inspect the real thing first: the actual API payloads, refresh cadence, what is computed where, caps and truncation, security policy (CSP for fonts, scripts, network), performance budget, and which baselines do not exist (a start-of-day value, a calibration, a reference price).

**Deliverable: an Honesty Contract.**
- What the system can genuinely prove right now.
- What is missing, delayed, capped, stale or uncalibrated.
- How every uncertain, empty or degraded state will be labelled on screen ("pending", "since start", "no fills yet", "stale · 8 days", "truncated"). Never a fake zero, never a silent gap.
- What new data, if any, the design needs, and its cost (payload size, extra requests).

If the honest answer to the brief needs data that does not exist, say so here, before anyone falls in love with a mockup.

## Gate 2 — Divergent exploration, side by side, with real data

Never commit to the first direction. Produce 3–4 structurally different metaphors on one comparison surface (canvas, artboards, or a page of options). Vary the structure, not just the colours: for example an ambient spatial grid, a chronological timeline, a tabular inspection card, an event log or tape.

**Deliverable: the comparison, populated with real domain names and stress cases**: the longest identifiers, empty periods, extreme values, many items and very few. Placeholder labels hide the layouts that break. Label sample numbers as sample.

Let the person mix and match ("this one's structure with that one's tape"). The chosen combination becomes the guiding direction.

## Gate 3 — One question per view

Every view that survives must answer one distinct human question in one attentional mode:

| Mode | Question | Typical form |
|---|---|---|
| Ambient / peripheral | What is happening right now? | heat grid, wall board, status lights |
| Temporal | How did this unfold? | session lanes, timeline, history scrubber |
| Inspection | What exactly are the parameters? | cards, tables, detail panes |

**Deliverable: one sentence per view: the question it answers and the feeling it should produce.** Cut any view that answers its question more slowly than a neighbour. Keep two views only when merging them would blur both questions. Keep the existing detailed view as the default when people already depend on it; new views are additions, not replacements.

## Gate 4 — One guiding texture

Several views should feel like turning your head inside one room, not leaving for another tool. Name the guiding texture (the person often names it; adopt their words) and hold every view to it: palette, typography, background material, heat scale, and any live element that runs through all views (a tape, a clock, a status strip).

**Deliverable: four locked visual rules, written down before coding:**
1. **One meaning per colour channel.** Separate event polarity from value polarity: for example amber/blue for transitions (entry/exit), green/red only for gain/loss or pass/fail. Intensity scales are single-hue with monotonic lightness. Always pair colour with a glyph or word so it works for colour-blind readers.
2. **Contrast survives the effect.** Text on tinted, glowing or heat-coloured grounds must stay readable; darken the tint before you lighten the text. Keep a type scale of three or four steps with a floor (about 12px for reading text). If something only fits at 9px, it belongs one layer deeper.
3. **Motion means change.** Animate only state transitions, and let effects decay (a 15-minute glow, a fading bar) so stillness means stability and brightness means recency. Honour `prefers-reduced-motion`, and give every animated element a still, screen-reader-readable equivalent (the tape is decorative; the log is the accessible copy).
4. **Respect the platform's boundaries.** Reuse existing polling loops, derive transitions client-side when that's cheap, keep payloads capped, and use safe font fallbacks rather than loosening CSP for looks.

## Gate 5 — Mockup-to-code fidelity and cross-viewport critique

When building the chosen design, actively remove mockup-era assumptions. Mockups use convenient numbers; code must use only what Gate 1 says is provable (for example "today's P&L" becomes "P&L since start" when no start-of-day value exists).

**Deliverable: a verification note.**
- Every displayed figure carries an honest label, and degraded states render as designed (test empty, pending, stale, truncated, closed).
- Test as two instruments: a wide ambient display and a narrow handheld screen. Check for overflow, clipped labels, stray scrollbars, unreadable tints.
- A short self-critique: name any flourish that could obscure a missing or truncated datum, and fix or justify it.
- Tests for the data shaping and the rendering (escaping, labels, empty states), not just a screenshot.

## Aesthetic principles: why the forms work

Use these to explain choices, in reviews and write-ups, in terms of the experience they create, not just the mechanics.

- **Ambient awareness over reading.** The best monitoring surfaces are felt before they are read: the eye catches a flicker, a colour shift, a burst, and knows something moved before knowing what. Design the first glance (colour, position, size) to carry the state; the numbers confirm it.
- **Light means live; the past dims.** Render freshness as brightness and recency as glow. Finished things fade into history; current things emit light. This lets the interface say "how current is this" without another line of text.
- **Motion is a signal, never ambience.** If nothing happened, nothing moves. A quiet surface is information too.
- **Lead with the object.** Each page exists to show one thing; make it the largest, earliest, most central element. Summaries sit beside or above it like a scoreboard, never in front of it.
- **Truth at the right depth.** Honesty implemented as volume buries the claim. Keep short state labels in the line of sight; move methodology one interaction away (a disclosure or hover card). Relocate caveats, never delete them.
- **Bounded time has a shape.** A timeline turns a session into space: the reader sees rhythm (busy open, quiet middle, late moves) and feels the deadline as a moving "now" line.
- **Participants, not records.** Lead with names people use, not internal IDs; it turns monitoring into watching actors.
- **Trust is part of the experience.** Immersion breaks the moment one number lies. "Pending" beats a plausible-looking figure.
- **Retro or stylised is a texture, not a cage.** The look comes from colour, type, glow and motion, so it must survive reflowing to a phone.
- **One floor, many rooms.** Different tools may have different layouts, but the shell (header, navigation, tokens, status language, sign-in) is shared.

## Critique mode

When asked to critique an existing interface rather than build one:

1. Visit every page in the real environment. Measure, don't estimate: position of the primary object relative to the fold, share of width it uses, the type-size distribution, word count, how many meanings each colour carries. Note what you could not see (gated pages, signed-out states, devices not tested).
2. State a thesis: the one underlying problem most findings share.
3. Group findings into cross-cutting themes, each tied to a principle above, then go page by page: what works (name it, so it is protected) before what doesn't, then a direction.
4. Separate outright bugs (wrong labels, contradictory status, a chart that doesn't contain the price) from design debt.
5. End with principles to carry forward and a prioritised change list (priority, change, pages, size).

## Working agreements

- Show options before deciding; let the person choose and combine.
- Write the Honesty Contract and the one-sentence view questions where the person can see them.
- When the person corrects the direction ("make it consistent with A, which is the guiding texture"), restate the rule you are now holding to and apply it everywhere, not just to the one view they mentioned.
- Never publish, deploy or message anyone as part of this method without the person's approval.
