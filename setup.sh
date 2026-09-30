#!/bin/bash
# Shared Threads - one-time setup: virtualenv, dependencies, database.
set -e
cd "$(dirname "$0")"

python3 -m venv venv
./venv/bin/pip install -q -r requirements.txt
./venv/bin/python -c "import app; app.init_db(); print('database ready')"

echo ""
echo "Next steps:"
echo "  1. Create a group:"
echo "       ./venv/bin/python add_group.py --name 'Family' --username family --members 'Pete,Kelly'"
echo "     (prints the group slug; the password is written to .password.<slug>, mode 600)"
echo "  2. Give each member a posting passcode:"
echo "       ./venv/bin/python set_member_passcode.py --group <slug> --member <name>"
echo "  3. Run the app (listens on 127.0.0.1:8471 by default; set PORT to change):"
echo "       ./venv/bin/python app.py"
echo "  4. Expose it to your people - any of: Tailscale, Cloudflare Tunnel,"
echo "     a reverse proxy, or plain localhost if everyone is on the machine."
echo "  5. Point your assistant's watcher at it (see README 'Agent wiring')."
echo ""
echo "If your assistant isn't called Muse, set ASSISTANT_NAME before running"
echo "anything, e.g.  export ASSISTANT_NAME=Aria"
