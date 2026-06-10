#!/bin/bash
# SecureClaw Network Hardening - iptables rules
# Review carefully before applying!
# Generated: 2026-04-05T20:57:39.877Z

# Block known C2 IPs
iptables -A OUTPUT -d 91.92.242.30 -j DROP

# Egress allowlist (uncomment to enforce)
# WARNING: This will restrict ALL outbound traffic to only allowed destinations
# iptables -A OUTPUT -d api.anthropic.com -p tcp --dport 443 -j ACCEPT
# iptables -A OUTPUT -d api.openai.com -p tcp --dport 443 -j ACCEPT
# iptables -A OUTPUT -d generativelanguage.googleapis.com -p tcp --dport 443 -j ACCEPT
# iptables -A OUTPUT -d api.together.xyz -p tcp --dport 443 -j ACCEPT
# iptables -A OUTPUT -d openrouter.ai -p tcp --dport 443 -j ACCEPT
# iptables -A OUTPUT -p tcp --dport 443 -j DROP  # Block all other HTTPS