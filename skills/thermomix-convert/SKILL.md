---
name: thermomix-convert
description: Convert standard recipes to Thermomix TM7 guided cooking format. Use when a user asks to adapt, convert, or "Thermomix-ify" a recipe. Handles ingredient prep, numbered guided-cooking steps, speed/time/temperature mapping, Varoma setup, and Cookidoo integration.
metadata: {"clawdbot":{"emoji":"🌀","requires":{"bins":[],"python":"3.12"}}}
---

# Thermomix TM7 Recipe Converter

Converts conventional recipes to Thermomix TM7 guided cooking format and saves them to the local recipe collection. Can optionally push to Cookidoo or add ingredients to the Cookidoo shopping list via Home Assistant.

## TM7 vs TM5/TM6 — Key Differences

| Feature | TM5/TM6 | TM7 |
|---------|---------|-----|
| Interface | Dial + display | Full touchscreen |
| Guided cooking | Optional | Native, default format |
| Cookidoo sync | Manual | Automatic (WiFi) |
| Max temp | 120°C | 130°C |
| Bowl capacity | 2.0–2.2 L | 2.2 L |
| Speed max | 10 + Turbo | 10 + Turbo (same scale) |
| Weighing | Yes | Yes (improved sensor) |
| "Dough mode" | Closed lid symbol | Still supported |

**TM7 guided cooking format uses numbered steps.** Each step must be self-contained with a single action. No compound steps. Aim for 1 TM operation per step.

## Speed Scale (TM7 — same as TM6)

| Speed | Use |
|-------|-----|
| 1 | Gentle stirring without splashing |
| 2 | Slow mix, keeping delicate shapes |
| 3 | Folding, gentle kneading assist |
| 4 | Mixing sauces, béchamel, custard |
| 5 | Rough chop (large vegetables) |
| 6 | Medium chop (onions, peppers) |
| 7 | Fine chop, coarse grinding |
| 8 | Blending soft foods, smooth sauces |
| 9 | Pureeing, smooth soups |
| 10 | Highest blend — nut butters, very smooth |
| Turbo | Pulse — ice crushing, hard cheese, dry nuts |

## Temperature Reference (TM7)

| Setting | Notes |
|---------|-------|
| 37°C | Body temp — chocolate tempering, yeast proofing |
| 50°C | Melting chocolate, butter |
| 60°C | Warming milk, gentle custard |
| 80°C | Custard, egg-based sauces (safe from curdling) |
| 90°C | Pastry cream, thickening starches |
| 100°C | Boiling — soups, stocks, pasta sauces |
| 110°C | Jam/preserve setting point |
| 120°C | Caramel (use with MC out) |
| 130°C | TM7 max — high-heat sauces (NEW vs TM6) |
| Varoma | Steam mode — equivalent to ~100–115°C in steam environment |

## Bowl Capacity
- Liquid max: 2.2 L — warn if recipe exceeds this, suggest batching
- Varoma tray/dish: use for steaming above bowl simultaneously

## Common Conversions

| Conventional technique | TM7 guided step |
|-----------------------|----------------|
| Dice onion | 5 sec / Speed 6 / Scrape down |
| Sauté onion + oil | 3 min / 120°C / Speed 1 |
| Sweat vegetables | 5 min / 100°C / Speed 1 |
| Boil potatoes | 20 min / 100°C / Speed 1 (add water to cover) |
| Steam vegetables | Varoma / 20 min / Speed 1 |
| Make béchamel | 7 min / 90°C / Speed 4 |
| Make custard | 8 min / 80°C / Speed 4 |
| Blend soup | 30 sec / Speed 9 (MC in, hand on lid) |
| Whip cream | 3 min / Speed 3.5 (butterfly, watch closely) |
| Knead dough | 2 min / Dough mode |
| Melt chocolate | 5 min / 50°C / Speed 2 |
| Make caramel | 10 min / 120°C / Speed 2 (MC out) |
| Steam fish | Varoma / 15 min / Speed 1 (Varoma dish) |
| Cook rice | 13 min / 100°C / Speed 4 (simmering basket) |
| Grind spices | 30 sec / Speed 10 |
| Chop nuts | 5 sec / Turbo × 2–3 |

## Guided Cooking Format (TM7 Native)

TM7 displays one numbered step at a time. Each step must be:
- **One action** only
- **Self-contained** — user reads it, executes, presses start or confirms
- Written in imperative: "Add", "Cook", "Blend", "Weigh", "Scrape down"

### Step Types

**Weigh step** (no machine action):
```
Step N — Weigh ingredients
Add [ingredient list] to the bowl. Weigh to [X]g.
```

**Cook step**:
```
Step N — [Descriptive label]
[X] min / [temp]°C / Speed [X]
```

**Manual step** (oven, hob, rest):
```
Step N — [Label] ⚠️ Hob/Oven
[Instruction for conventional step]
```

**Interim prep step**:
```
Step N — Scrape down / Set aside / Insert butterfly
[One-line instruction]
```

## Conversion Process

1. **Identify TM-replaceable steps** — chopping, sautéing, blending, steaming, cooking wet sauces
2. **Identify non-TM steps** — grilling, deep frying, oven baking, resting — mark as Manual
3. **Explode into single-action steps** — one TM operation per step, no compound steps
4. **Determine Varoma need** — if steaming + cooking simultaneously, set up Varoma
5. **Order correctly** — what goes in the bowl last? What gets set aside?
6. **Bowl wash decision** — flag if recipe needs a mid-cook rinse (e.g., sweet then savoury)

## Output Format

Save to `/home/node/.openclaw/workspace/data/recipes/[recipe-name]-tm7.md`

```markdown
# [Recipe Name] — TM7 Guided Cooking

*Converted from: [original source]*
*Servings: X | Prep: Xmin | Total: Xmin*
*Equipment: Bowl + [Varoma / Butterfly / Simmering basket if needed]*

## Ingredients

**In bowl:**
- [ingredient + quantity]

**In Varoma (if used):**
- [ingredient + quantity]

**Conventional (set aside):**
- [ingredient + quantity]

---

## Guided Steps

**Step 1 — Weigh and prep**
Add [X] to bowl. Weigh to [X]g.

**Step 2 — [Label]**
[X] min / [temp]°C / Speed [X]

**Step 3 — Scrape down**
Scrape down sides of bowl.

**Step 4 — [Label]**
[X] min / [temp]°C / Speed [X]

**Step N — [Manual step label] ⚠️ Hob/Oven**
[Conventional instruction while TM runs or rests]

---

## Notes
- [Adaptation notes, timing tips, substitutions tried]
```

## Cookidoo Integration

### Save to Cookidoo (Custom Recipe)

Uses `scripts/save-to-cookidoo.py` with the `cookidoo-api` library.

```bash
python3.12 /home/node/.openclaw/workspace/skills/thermomix-convert/scripts/save-to-cookidoo.py \
  --file /home/node/.openclaw/workspace/data/recipes/my-recipe-tm7.md
```

Requires `COOKIDOO_EMAIL` and `COOKIDOO_PASSWORD` in secrets.json. Currently PLACEHOLDER — ask Darren for credentials.

Dry run (no save):
```bash
python3.12 .../save-to-cookidoo.py --file recipe.md --dry-run
```

### Shopping List via Home Assistant

The Cookidoo HA integration is active. Use these entities to add recipe ingredients to the Cookidoo shopping list:

| Entity | Purpose |
|--------|---------|
| `todo.cookidoo_shopping_list` | Main Cookidoo shopping list |
| `todo.cookidoo_additional_purchases` | Secondary/extras list |
| `sensor.cookidoo_subscription` | Subscription status (premium) |

**Add ingredient to shopping list:**
```bash
export PATH=/home/node/.openclaw/workspace/.bin:$PATH
export HA_CONFIG=/mnt/homeassistant/config.json

# Add a single item
/home/node/.openclaw/workspace/skills/home-assistant/scripts/ha.sh call \
  todo create_item \
  '{"entity_id": "todo.cookidoo_shopping_list", "item": "[ingredient]"}'

# Check list status
/home/node/.openclaw/workspace/skills/home-assistant/scripts/ha.sh state todo.cookidoo_shopping_list
```

**When user asks to "add to shopping list" from a recipe:**
1. Extract the ingredient list from the TM7 recipe
2. For each ingredient, call `todo.create_item` on `todo.cookidoo_shopping_list`
3. Confirm: "Added X ingredients to your Cookidoo shopping list"

**Check subscription:**
```bash
/home/node/.openclaw/workspace/skills/home-assistant/scripts/ha.sh state sensor.cookidoo_subscription
```

## Rules

- **One action per step** — TM7 guided cooking is sequential, single-step
- **Always specify** time / temperature / speed for every TM cook step
- **Bold the TM parameters** for readability
- Note when MC (measuring cup) should be in or out
- Flag bowl capacity issues (>2.2L liquid)
- Flag if a mid-cook bowl wash is needed
- If recipe has no benefit from Thermomix (e.g., simple salad, cold dip), say so
- Preserve original flavours — don't substitute without asking
- When saving to Cookidoo, always dry-run first and confirm with user before pushing
