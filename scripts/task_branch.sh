#!/bin/zsh
set -euo pipefail

if ! command -v git >/dev/null 2>&1; then
	echo "git is not installed or not on PATH." >&2
	exit 1
fi

repo_root=$(git rev-parse --show-toplevel 2>/dev/null) || {
	echo "Run this inside the Boss git repository." >&2
	exit 1
}

cd "$repo_root"

if [[ $# -eq 0 ]]; then
	echo "Usage: ./scripts/task_branch.sh <task-slug>" >&2
	exit 1
fi

raw_slug="$*"
slug=$(print -r -- "$raw_slug" \
	| tr '[:upper:]' '[:lower:]' \
	| sed -E 's#^boss/##; s#[^a-z0-9]+#-#g; s#-+#-#g; s#(^-|-$)##g')

if [[ -z "$slug" ]]; then
	echo "Couldn't derive a branch slug from: $raw_slug" >&2
	exit 1
fi

if ! git rev-parse --verify HEAD >/dev/null 2>&1; then
	echo "Repository has no commits yet. Create an initial checkpoint commit on main first." >&2
	exit 1
fi

branch="boss/$slug"

if git show-ref --verify --quiet "refs/heads/$branch"; then
	git switch "$branch"
else
	git switch -c "$branch"
fi

echo "On branch $branch"
