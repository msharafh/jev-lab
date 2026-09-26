#!/bin/zsh
# Double-click to start Jev Lab. Opens http://127.0.0.1:8765 in your browser.
# Close this window (or press Ctrl+C) to stop it.
cd "$(dirname "$0")"
[ -f "$HOME/.zshrc" ] && source "$HOME/.zshrc" >/dev/null 2>&1

# Stop any older Jev Lab still running on the port, so the latest code always loads
OLD=$(lsof -ti tcp:8765 2>/dev/null)
if [ -n "$OLD" ]; then
  echo "Stopping previous Jev Lab (pid $OLD)…"
  kill $OLD 2>/dev/null; sleep 1
fi

for PY in python3 python /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if command -v "$PY" >/dev/null 2>&1 && "$PY" -c "import typesafe_sdk" >/dev/null 2>&1; then
    exec "$PY" app.py
  fi
done

echo "Could not find a Python with typesafe-sdk installed."
echo "Run:  python3 -m pip install typesafe-sdk   then double-click start.command again."
read -k 1 "?Press any key to close."
