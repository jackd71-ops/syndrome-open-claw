import os
import sys

# Always use env var — never read secrets.json directly
key = os.environ.get('COMPOSIO_API_KEY', '')
if not key:
    print("ERROR: COMPOSIO_API_KEY env var not set", file=sys.stderr)
    sys.exit(1)
print(key, end='')
