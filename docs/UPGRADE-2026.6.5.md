# OpenClaw Transition: 2026.4.2 → 2026.6.5 (plugins baked into image)

**Prepared:** 2026-06-12 · **Reviewed by:** Opus 4.8
**Current state:** OpenClaw `2026.4.2`, Python `3.13.7`, container `openclaw-custom:latest`
**Rollback image:** `openclaw-custom:2026.4.2-rollback` (current known-good build, tagged)
**Decision:** Plugins will be **baked into the image** for true portability + atomic rollback.

---

## TL;DR

Two hard breaking changes, plus a deliberate architecture change:

1. **`lobster` removed from core** — referenced in 3 config spots; we don't use it → delete the refs. *(Empirically confirmed: 2026.6.5 emits `plugins.entries.lobster: plugin not installed` and points at `openclaw plugins install @openclaw/lobster`.)*
2. **`lossless-claw 0.8.2`** loads on 2026.6.5 but predates the 2026.5.22 plugin-API bump → ship **0.12.0**.
3. **Plugins move into the image** (`/app/dist/extensions` "stock" root) instead of living in the mounted config volume. Gateway + plugins now version and roll back as one artifact.

**Revised risk: Medium.** All mechanical, all reversible in ~1 min via the rollback image.

---

## 1. Why bake plugins into the image

Today plugins live in `/opt/openclaw/config/extensions/` — the **mounted config volume**, outside the image. Consequences:

- The image alone is **not** self-contained (a fresh `docker run` has no plugins).
- An image rollback does **not** roll back plugin versions — you can strand a 0.12.0
  plugin on a 2026.4.2 gateway. That's exactly the drift we're trying to kill by
  pinning the image version.

**The clean fix (verified against the 2026.6.5 image):** OpenClaw scans two plugin
"source roots":

```
stock:  /app/dist/extensions     ← inside the IMAGE (not shadowed by the config mount)
global: /state/extensions        ← the mounted config volume
```

Dropping our three plugins into the **stock** root (`/app/dist/extensions/<id>/`) at build
time makes them image-resident. The config-volume mount (`/opt/openclaw/config →
/home/node/.openclaw`) does not touch `/app`, so there is **no shadowing and no
entrypoint copy hack** — it uses OpenClaw's own bundled-plugin mechanism.

---

## 2. Plugin stack — actions

| Plugin | Now | Target | Action |
|---|---|---|---|
| **lossless-claw** | 0.8.2 (global volume) | **0.12.0** (stock/image) | Bake 0.12.0 into `/app/dist/extensions/lossless-claw` |
| **manifest** | 5.45.1 (global volume) | 5.45.1 (stock/image) | Bake into image |
| **secureclaw** | 2.2.0 (global volume) | 2.2.0 (stock/image) | Bake into image |
| **lobster** | bundled beta, enabled | — | **Remove** 3 config refs; do not reinstall |

`manifest` stays load-bearing — it is our **model router** (`manifest/auto` → `localhost:2099`),
not just the cost DB `daily_cost.py` reads. Native usage tracking complements it, never replaces it.

---

## 3. Config changes (`openclaw.json`)

- Remove **`plugins.entries.lobster`**, the `"lobster"` items in **`plugins.allow`** and **`tools.alsoAllow`**.
- Remove the entire **`plugins.installs`** block — stock/baked plugins are not "installed"
  via the npm-install records; keep only `plugins.entries` (enabled + config) and `plugins.allow`.
- Keep all per-plugin config in `plugins.entries`: `secureclaw.riskProfile`,
  `lossless-claw.ignoreSessionPatterns`, etc. *(Stock-placed plugins read `plugins.entries`
  config by plugin id — confirmed by probe before build.)*
- Everything else (contextPruning@defaults, compaction safeguard, heartbeat, cron
  sessionTarget/staggerMs/wakeMode/agentId, telegram dmPolicy, gateway token auth, hooks)
  is schema-compatible and unchanged.
- After cutover, **empty** `/opt/openclaw/config/extensions/` so the global root doesn't
  duplicate/override the image copies.

---

## 4. Dockerfile changes

- `FROM ghcr.io/openclaw/openclaw:2026.6.5` (pin — stop tracking `:latest`, the source of drift).
- Keep the Python 3.13 layer (already in place).
- Add a reproducible step that places **pinned** plugin versions into `/app/dist/extensions/`:
  fetch each package at its exact version (lossless-claw `0.12.0`, manifest `5.45.1`,
  secureclaw `2.2.0`) and lay it out as `<id>/` with its `package.json`,
  `openclaw.plugin.json`, built `dist/`, and any runtime deps; `chown node:node`.
- Net: gateway + all 3 plugins are one versioned, reproducible image.

---

## 5. New features since 2026.4.2 — verdicts

| Feature | Verdict |
|---|---|
| **Native session-pruning** | ⭐ High value; candidate to *replace* lossless-claw later. Trial **after** this upgrade — one change at a time. |
| **Native usage tracking** | DeepSeek balance is now native (we hand-built it). Anthropic needs OAuth; we bill by API key, so keep our `daily_cost.py` tracker. |
| **memory-core / active-memory** | Defer — embeddings-backed memory vs flat markdown is its own project. |
| **cacheRetention** (per-agent/model) | Evaluate alongside any future lossless-claw drop. |
| **workboard** | Optional task tracking; explore later. |
| **Image generation** | Not needed now. |
| **Python 3.13** | ✅ Done. |

---

## 6. Execution plan

> Rollback image `openclaw-custom:2026.4.2-rollback` tagged. `safe-restart.sh` backs up the Manifest DB each restart.

1. **Validate the design** with a throwaway image (plugins in stock root, config without
   lobster/installs, no global `extensions/`) → confirm all 3 show `loaded`/enabled from
   stock and config is honoured. ✅ **PROVEN 2026-06-12:** all 3 load from `stock:/app/dist/extensions`
   with no global dir present; `lossless-claw.ignoreSessionPatterns` present in runtime config.
2. **Dockerfile:** pin to 2026.6.5; bake pinned plugins into `/app/dist/extensions/`.
3. **Config:** remove lobster (3 refs) + `plugins.installs`; back up `openclaw.json` first.
4. **Build:** `sudo docker build -t openclaw-custom:latest /opt/openclaw/`
5. **Restart:** `sudo /opt/openclaw/scripts/safe-restart.sh`
6. **Verify, in order:**
   - `docker exec openclaw node /app/openclaw.mjs plugins list` → lossless-claw 0.12.0,
     manifest, secureclaw all `loaded`/enabled from `stock:` root; **no** lobster error.
   - `docker exec openclaw cat /app/package.json` → `2026.6.5`; `docker ps` healthy.
   - Telegram round-trip with Kevin (gateway + telegram + manifest routing).
   - One cron script manually (weather) → Telegram arrives + status file written.
   - `daily_cost.py --print` → Manifest DB still queried.
   - Confirm secureclaw strict risk profile + lossless ignore patterns active.
7. **Empty** `/opt/openclaw/config/extensions/` (now superseded by image copies).
8. **Rollback if needed:** `docker tag openclaw-custom:2026.4.2-rollback openclaw-custom:latest` → `safe-restart.sh`.

---

## 6b. Implementation notes (validated 2026-06-12)

Plugins are **not** self-contained — each ships a full `node_modules`:

| Plugin | Size | Bake method |
|---|---|---|
| manifest 5.45.1 | 193 MB | **COPY** existing `/opt/openclaw/config/extensions/manifest` (probe-proven to load) |
| secureclaw 2.2.0 | 689 MB | **COPY** existing dir (probe-proven) |
| lossless-claw | **1.1 GB** | existing is 0.8.2 — **0.12.0 needs a build-time `openclaw plugins install npm:@martian-engineering/lossless-claw@0.12.0`** into a staging `OPENCLAW_STATE_DIR`, then COPY the resulting self-contained dir. **Untested — validate before relying on it.** |

Image grows ~2 GB (→ ~7 GB) if all three are baked.

**DECISION (2026-06-12): KEEP — bake lossless-claw 0.12.0.** It remains founder-recommended
and the most popular context engine on current OpenClaw. The build therefore includes a
build-time `openclaw plugins install npm:@martian-engineering/lossless-claw@0.12.0` into a
staging `OPENCLAW_STATE_DIR`, then COPY the resulting self-contained dir into
`/app/dist/extensions/lossless-claw`. Image ends ~7 GB with all three baked.

⚠️ **The lossless-claw 0.12.0 build-time install is the one untested step.** Validate during
the build: confirm the staged dir has `dist/index.js` + `node_modules`, then that `plugins list`
shows it `loaded` from the stock root at version `0.12.0` before relying on the image.

## 7. Post-upgrade follow-ups

- [ ] **Plugin / memory-stack review** — keep lossless-claw; evaluate complements. Agenda:

  | Plugin / Stack | Best for | Note |
  |---|---|---|
  | **Lossless Claw (LCM)** | Session context & compaction | **Keep** — DAG-based lossless summarization, founder-recommended, top-tier for active sessions |
  | QMD + Lossless Claw | Hybrid search + context | Most popular power-user combo; QMD = smart recall/search. *(QMD ships in 2026.6.5 `memory-core`.)* |
  | memory-lancedb | Long-term vector memory | Faster / more accurate retrieval than default; easy upgrade |
  | memU | Proactive long-term memory | Hierarchical knowledge graph; strong for complex agents |
  | Memory Wiki | Structured knowledge vault | Inspectable wiki-style pages *(bundled in 2026.6.5)* |
  | Mem0 / Hindsight / ClawXMemory | Persistent / auto-capture memory | Automatic extraction + cross-session recall; set-and-forget |

  In 2026.6.5 `memory-core` (QMD) and `memory-wiki` are **bundled**; the rest are external npm — verify availability/compat during the review.
- [ ] Trial native session-pruning + `cacheRetention` **alongside** LCM (complement, not replace).
- [ ] Weekly "new version available" checker (OpenClaw image + Python).
- [ ] Decide on `workboard`.

---

## 8. What the first (Sonnet) pass missed — for the record

- `lobster` removal (would have failed config validation on boot).
- `lossless-claw` mandatory version bump (2026.5.22 plugin-API change).
- That native usage tracking now does DeepSeek balance.
- The plugin-portability gap entirely (plugins in volume, not image) — the reason for this rework.

---

## 9. EXECUTED 2026-06-12 — as-built notes & corrections (Opus review)

**Status: ✅ Done.** Container on `2026.6.5`, healthy. All 3 plugins load from stock
`/app/dist/extensions` at pinned versions; Manifest router serves HTTP 200 on :2099 and
routed a live completion; Telegram round-trip OK; lobster gone; no duplicate warnings.
Final image **3.01 GB** (the 7 GB estimate assumed the bloated volume copies; npm installs
are far leaner).

### What changed vs the original plan
The original plan said **COPY** manifest + secureclaw from `config/extensions/`. That is
**not reproducible**: those dirs are gitignored, so `git clone && docker build` (the whole
point — TrueNAS portability) would fail. **Corrected: all three plugins are now
`openclaw plugins install npm:<spec>@<version>` at build time** — the image is reproducible
from the Dockerfile + git alone, with zero dependency on the config volume.

### ⚠️ Gotcha: npm hoisting (cost us a manifest-router outage in testing)
The OpenClaw CLI installs to `~/.openclaw/npm/projects/<proj>/node_modules/` and **hoists**
shared deps to the project-root `node_modules`. Copying only the package dir drops them —
manifest's embedded NestJS router then dies with `Cannot find module '@nestjs/core'` and
**model routing breaks** (manifest/auto is our primary model). Fix in `bake()`: copy the
package dir **and overlay the project-root `node_modules`** (`cp -rn`, which preserves any
deps a plugin nests itself, e.g. lossless-claw). Always check the logs for
`Failed to start local server` after a manifest rebuild.

### lossless-claw 0.12.0 — config schema verified
`ignoreSessionPatterns` + `statelessSessionPatterns` still honoured (confirmed in
`openclaw.plugin.json` configSchema and built `dist/index.js`). Cron anti-bloat exclusion
survives the version bump. 0.12.0 adds `skipStatelessSessions` (evaluate later).

### memory-core / QMD now bundled + auto-enabled
2026.6.5 ships `memory-core` (QMD) enabled by default — the QMD half of the widely-cited
"QMD + Lossless Claw" combo. It's tool-based recall (`memory_search`/`memory_get`) with
"dreaming" consolidation **off by default**, so no cron-bloat risk. Left enabled.
`memory-wiki` also bundled but disabled.

### ⚠️ CORRECTED ROLLBACK PROCEDURE
The rollback image `openclaw-custom:2026.4.2-rollback` predates this work and expects
plugins in the **config volume**, which we emptied. Rolling back the image alone leaves
2026.4.2 with **no plugins**. Full rollback is TWO steps:
1. `sudo cp -r /opt/openclaw/config/extensions-bak/lossless-claw-0.8.2 /opt/openclaw/config/extensions/lossless-claw && \
   sudo cp -r /opt/openclaw/config/extensions-bak/manifest-5.45.1 /opt/openclaw/config/extensions/manifest && \
   sudo cp -r /opt/openclaw/config/extensions-bak/secureclaw-2.2.0 /opt/openclaw/config/extensions/secureclaw`
   (and restore the lobster refs + `plugins.installs` block from `openclaw.json.pre-2026.6.5.bak`)
2. `docker tag openclaw-custom:2026.4.2-rollback openclaw-custom:latest && sudo /opt/openclaw/scripts/safe-restart.sh`

**Therefore `config/extensions-bak/` must be kept** until the 2026.4.2 rollback image itself
is retired. (Forward rollback within 2026.6.5 just needs a rebuild — the Dockerfile is the
source of truth now.)
