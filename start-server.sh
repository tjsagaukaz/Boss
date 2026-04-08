#!/bin/zsh
# Boss API server launcher — sources environment and starts uvicorn
source ~/.zshrc 2>/dev/null
cd /Users/tj/boss || exit 1

if [[ ! -x /Users/tj/boss/.venv/bin/python ]]; then
	echo "Missing /Users/tj/boss/.venv/bin/python" >&2
	echo "Create or refresh the venv with: cd /Users/tj/boss && python3 -m venv .venv && /Users/tj/boss/.venv/bin/python -m pip install -e ." >&2
	exit 1
fi

exec /Users/tj/boss/.venv/bin/python -m uvicorn boss.api:app --host 127.0.0.1 --port 8321
