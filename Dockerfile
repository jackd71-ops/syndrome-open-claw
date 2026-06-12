FROM ghcr.io/openclaw/openclaw:2026.6.5

USER root

# System deps
RUN apt-get update -qq && apt-get install -y jq python3-dateutil && apt-get clean && rm -rf /var/lib/apt/lists/*

# Python 3.13 standalone (no system deps, self-contained)
# To upgrade: update version tag from https://github.com/astral-sh/python-build-standalone/releases
RUN curl -fsSL https://github.com/astral-sh/python-build-standalone/releases/download/20260610/cpython-3.13.14+20260610-x86_64-unknown-linux-gnu-install_only.tar.gz \
    | tar -xz -C /opt \
    && ln -sf /opt/python/bin/python3.13 /usr/local/bin/python3 \
    && ln -sf /opt/python/bin/python3.13 /usr/local/bin/python3.13 \
    && ln -sf /opt/python/bin/python3.13 /usr/local/bin/python3.12 \
    && ln -sf /opt/python/bin/python3.13 /opt/python/bin/python3 \
    && /opt/python/bin/pip3 install --no-cache-dir cookidoo-api openpyxl python-dateutil flights croniter

# ── Plugin bake-in (stock root: /app/dist/extensions) ──────────────────────────
# All three plugins are fetched from npm at PINNED versions and copied into the
# image's stock plugin root. This makes the image fully self-contained and
# reproducible from the Dockerfile alone — no dependency on the config volume,
# so `git clone && docker build` works anywhere (e.g. TrueNAS migration).
# The config volume's extensions/ dir is kept empty so these stock copies win.
#
# Note: the OpenClaw CLI installs to ~/.openclaw/npm/projects/<proj>/node_modules/
# and HOISTS shared deps to that project-root node_modules (e.g. manifest's
# embedded NestJS server needs @nestjs/core, which lives at the project root, not
# inside the package dir). So bake() copies the package dir AND overlays the
# project-root node_modules into it — otherwise manifest's router fails with
# "Cannot find module '@nestjs/core'". cp -n preserves any deps the package
# already nests (e.g. lossless-claw bundles its own node_modules).
RUN set -eux; \
    export HOME=/tmp/pluginstage; mkdir -p "$HOME/.openclaw"; \
    bake() { \
      spec="$1"; pkgpath="$2"; id="$3"; \
      node /app/openclaw.mjs plugins install "npm:$spec"; \
      dir="$(find "$HOME/.openclaw/npm/projects" -maxdepth 6 -type d -path "*/node_modules/$pkgpath" | head -1)"; \
      test -n "$dir"; \
      test -f "$dir/openclaw.plugin.json"; \
      ls "$dir/dist/"*.js >/dev/null; \
      rel="${dir#$HOME/.openclaw/npm/projects/}"; projroot="$HOME/.openclaw/npm/projects/${rel%%/*}"; \
      cp -r "$dir" "/app/dist/extensions/$id"; \
      mkdir -p "/app/dist/extensions/$id/node_modules"; \
      cp -rn "$projroot/node_modules/." "/app/dist/extensions/$id/node_modules/"; \
      chown -R node:node "/app/dist/extensions/$id"; \
      echo "BAKED $id <- $dir (+ hoisted deps)"; \
    }; \
    bake "manifest@5.45.1"                                "manifest"                              "manifest"; \
    bake "@adversa/secureclaw@2.2.0"                      "@adversa/secureclaw"                   "secureclaw"; \
    bake "@martian-engineering/lossless-claw@0.12.0"      "@martian-engineering/lossless-claw"    "lossless-claw"; \
    rm -rf /tmp/pluginstage

USER node
