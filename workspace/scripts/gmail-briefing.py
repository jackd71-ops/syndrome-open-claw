#!/usr/bin/env python3
"""
Daily Gmail briefing for Darren.
Runs the processor and formats a readable summary.
"""

import json, subprocess, os, sys
from datetime import datetime

def run_processor():
    """Run the gmail-daily processor and return parsed summary."""
    cmd = [
        sys.executable, "-c",
        "import sys; sys.path.insert(0, '.'); exec(open('scripts/gmail-daily.py').read())"
    ]
    result = subprocess.run(cmd, cwd="/home/node/.openclaw/workspace",
                          capture_output=True, text=True)
    try:
        return json.loads(result.stdout)
    except:
        return {"error": result.stderr or "Failed to parse output"}

def format_briefing(summary):
    """Convert JSON summary to human-readable briefing."""
    lines = []
    lines.append("📬 **Daily Gmail Briefing**")
    lines.append(f"_{datetime.now().strftime('%A, %d %B %Y')}_")
    lines.append("")
    
    # Delivery today (most important)
    if summary.get("delivery_today"):
        lines.append("🚚 **Expected deliveries today:**")
        for item in summary["delivery_today"][:5]:
            subj = item.get("subject", "")[:60]
            lines.append(f"  • {subj}")
        lines.append("")
    
    # Shipped (on the way)
    if summary.get("shipped"):
        lines.append("📦 **Shipped / on the way:**")
        for item in summary["shipped"][:3]:
            subj = item.get("subject", "")[:60]
            lines.append(f"  • {subj}")
        lines.append("")
    
    # Delivered (recent)
    if summary.get("delivered"):
        lines.append("✅ **Recently delivered:**")
        for item in summary["delivered"][:3]:
            subj = item.get("subject", "")[:60]
            lines.append(f"  • {subj}")
        lines.append("")
    
    # Watches (interests)
    if summary.get("watches"):
        lines.append("⌚ **Watch alerts:**")
        for item in summary["watches"][:3]:
            subj = item.get("subject", "")[:60]
            lines.append(f"  • {subj}")
        lines.append("")
    
    # Finance & Utilities
    if summary.get("finance"):
        lines.append("💰 **Financial updates:**")
        for item in summary["finance"][:2]:
            subj = item.get("subject", "")[:60]
            lines.append(f"  • {subj}")
        lines.append("")
    
    if summary.get("utilities"):
        lines.append("⚡ **Utility updates:**")
        for item in summary["utilities"][:2]:
            subj = item.get("subject", "")[:60]
            lines.append(f"  • {subj}")
        lines.append("")
    
    # Stats
    total_processed = (
        len(summary.get("delivery_today", [])) +
        len(summary.get("shipped", [])) +
        len(summary.get("delivered", [])) +
        len(summary.get("ordered", [])) +
        len(summary.get("watches", [])) +
        len(summary.get("finance", [])) +
        len(summary.get("utilities", [])) +
        len(summary.get("promos", []))
    )
    
    lines.append("📊 **Summary:**")
    lines.append(f"  • Processed: {total_processed} emails")
    if summary.get("promos"):
        lines.append(f"  • Promotions archived: {len(summary['promos'])}")
    if summary.get("filtered"):
        lines.append(f"  • Security emails filtered: {len(summary['filtered'])}")
    if summary.get("unlabeled"):
        lines.append(f"  • Unlabeled (needs review): {len(summary['unlabeled'])}")
    if summary.get("errors"):
        lines.append(f"  • Errors: {len(summary['errors'])}")
    
    # Unlabeled items (if any)
    if summary.get("unlabeled"):
        lines.append("")
        lines.append("❓ **Unlabeled emails (check these):**")
        for item in summary["unlabeled"][:5]:
            sender = item.get("sender", "")[:40]
            subj = item.get("subject", "")[:50]
            lines.append(f"  • {sender}: {subj}")
    
    return "\n".join(lines)

def main():
    summary = run_processor()
    if "error" in summary:
        print(f"❌ Error running processor: {summary['error']}")
        sys.exit(1)
    
    briefing = format_briefing(summary)
    print(briefing)

if __name__ == "__main__":
    main()
