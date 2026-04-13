#!/usr/bin/env python3.12
"""
save-to-cookidoo.py — Save a converted TM7 recipe to Cookidoo as a custom recipe.

Usage:
    python3.12 save-to-cookidoo.py --file /path/to/recipe.md
    python3.12 save-to-cookidoo.py --title "Pasta Bake" --servings 4 --ingredients "..." --steps "..."

Credentials are read from environment variables (resolved from secrets.json):
    COOKIDOO_EMAIL
    COOKIDOO_PASSWORD
"""

import asyncio
import argparse
import json
import os
import re
import sys
from pathlib import Path


SECRETS_FILE = Path(os.environ.get("OPENCLAW_STATE_DIR", Path.home() / ".openclaw")) / "secrets.json"


def _read_secrets_json() -> dict:
    """Read secrets.json directly — fallback when env vars are unset/placeholder."""
    try:
        return json.loads(SECRETS_FILE.read_text())
    except Exception:
        return {}


def load_credentials():
    """Load Cookidoo credentials from env vars, falling back to secrets.json."""
    secrets = None

    email = os.environ.get("COOKIDOO_EMAIL", "").strip()
    if not email or email == "PLACEHOLDER":
        secrets = _read_secrets_json()
        email = secrets.get("COOKIDOO_EMAIL", "").strip()

    password = os.environ.get("COOKIDOO_PASSWORD", "").strip()
    if not password or password == "PLACEHOLDER":
        if secrets is None:
            secrets = _read_secrets_json()
        password = secrets.get("COOKIDOO_PASSWORD", "").strip()

    if not email or email == "PLACEHOLDER":
        sys.exit(
            "Error: COOKIDOO_EMAIL not set.\n"
            "Update COOKIDOO_EMAIL in /opt/openclaw/secrets.json and /opt/openclaw/.env"
        )
    if not password or password == "PLACEHOLDER":
        sys.exit(
            "Error: COOKIDOO_PASSWORD not set.\n"
            "Update COOKIDOO_PASSWORD in /opt/openclaw/secrets.json and /opt/openclaw/.env"
        )
    return email, password


def parse_markdown_recipe(md_path: Path) -> dict:
    """Parse a thermomix-convert output markdown file into recipe fields."""
    text = md_path.read_text(encoding="utf-8")
    lines = text.splitlines()

    title = ""
    servings = 4
    ingredients = []
    steps = []
    current_section = None

    for line in lines:
        stripped = line.strip()

        # Title — first H1
        if stripped.startswith("# ") and not title:
            title = stripped.lstrip("# ").split("—")[0].strip()
            continue

        # Servings from frontmatter-style line
        if "servings:" in stripped.lower():
            m = re.search(r"(\d+)", stripped)
            if m:
                servings = int(m.group(1))
            continue

        # Section headers
        if stripped.lower().startswith("## ingredient"):
            current_section = "ingredients"
            continue
        if stripped.lower().startswith("## method") or stripped.lower().startswith("## steps"):
            current_section = "steps"
            continue
        if stripped.startswith("## "):
            current_section = None
            continue

        if current_section == "ingredients" and stripped and not stripped.startswith("#"):
            # Strip leading list markers
            ingredient = re.sub(r"^[-*•]\s*", "", stripped)
            if ingredient:
                ingredients.append(ingredient)

        if current_section == "steps":
            # Collect step content — skip sub-headers, combine lines
            if stripped.startswith("### "):
                # Step label — start a new step
                steps.append(stripped.lstrip("# ").strip())
            elif stripped and not stripped.startswith("#"):
                if steps:
                    steps[-1] = steps[-1] + " " + stripped
                else:
                    steps.append(stripped)

    if not title:
        title = md_path.stem.replace("-thermomix", "").replace("-", " ").title()

    return {
        "title": title,
        "servings": servings,
        "ingredients": ingredients,
        "steps": steps,
    }


async def save_recipe(email: str, password: str, recipe: dict) -> None:
    try:
        from cookidoo_api import Cookidoo, CookidooConfig
    except ImportError:
        sys.exit(
            "Error: cookidoo-api not installed. Run: python3.12 -m pip install cookidoo-api\n"
            "Or rebuild the Docker image: sudo docker build -t openclaw-OpenClaw:latest /opt/openclaw/"
        )

    print(f"Connecting to Cookidoo as {email}...")
    config = CookidooConfig(email=email, password=password)

    async with Cookidoo(config) as cookidoo:
        await cookidoo.login()
        print("Logged in.")

        # Build recipe payload
        # cookidoo-api uses add_custom_recipe or equivalent — check installed version's API
        # The library API as of v0.17 exposes recipe management via the async client
        try:
            result = await cookidoo.add_custom_recipe(
                name=recipe["title"],
                servings=recipe["servings"],
                ingredients=recipe["ingredients"],
                preparation_steps=recipe["steps"],
            )
            print(f"Recipe saved to Cookidoo: {recipe['title']}")
            print(f"Recipe ID: {getattr(result, 'id', 'unknown')}")
        except AttributeError:
            # Fall back: list available methods if API shape differs
            methods = [m for m in dir(cookidoo) if not m.startswith("_")]
            print("Note: add_custom_recipe not found. Available methods:")
            print("  " + "\n  ".join(m for m in methods if "recipe" in m.lower() or "custom" in m.lower()))
            print("\nRecipe data ready to post:")
            print(json.dumps(recipe, indent=2, ensure_ascii=False))
            sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Save a converted TM7 recipe to Cookidoo.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", help="Path to a thermomix-convert markdown output file")
    group.add_argument("--title", help="Recipe title (use with --ingredients and --steps)")
    parser.add_argument("--servings", type=int, default=4)
    parser.add_argument("--ingredients", help="Newline-separated ingredient list")
    parser.add_argument("--steps", help="Newline-separated step list")
    parser.add_argument("--dry-run", action="store_true", help="Parse and print recipe without saving")
    args = parser.parse_args()

    email, password = load_credentials()

    if args.file:
        recipe = parse_markdown_recipe(Path(args.file))
    else:
        recipe = {
            "title": args.title,
            "servings": args.servings,
            "ingredients": [i.strip() for i in (args.ingredients or "").splitlines() if i.strip()],
            "steps": [s.strip() for s in (args.steps or "").splitlines() if s.strip()],
        }

    print("Recipe parsed:")
    print(f"  Title:       {recipe['title']}")
    print(f"  Servings:    {recipe['servings']}")
    print(f"  Ingredients: {len(recipe['ingredients'])} items")
    print(f"  Steps:       {len(recipe['steps'])} steps")

    if args.dry_run:
        print("\n-- DRY RUN -- Not saving to Cookidoo.")
        print(json.dumps(recipe, indent=2, ensure_ascii=False))
        return

    asyncio.run(save_recipe(email, password, recipe))


if __name__ == "__main__":
    main()
