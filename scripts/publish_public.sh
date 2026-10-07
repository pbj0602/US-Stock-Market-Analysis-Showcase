#!/bin/bash
# Publish a snapshot of this (private) repo to the public showcase repo.
#
# The public repo gets one commit per release — never this repo's history,
# which contains secrets from early commits. Only files tracked at HEAD are
# exported (git archive), so .env, data/ and reports can't leak; the export
# is then scanned for real .env secret values and personal identifiers, and
# the publish aborts on any hit.
#
# Usage:
#   scripts/publish_public.sh "Release note title" [--push]
#
# Env:
#   PUBLIC_REMOTE  git URL of the public repo (needed on first run)
#   PUBLIC_DIR     local working copy of the public repo
#                  (default: ../stock_analysis_public)

set -euo pipefail

MSG="${1:-}"
PUSH="${2:-}"
if [ -z "$MSG" ]; then
    echo "Usage: $0 \"Release note title\" [--push]" >&2
    exit 1
fi

SRC="$(cd "$(dirname "$0")/.." && pwd)"
PUBLIC_DIR="${PUBLIC_DIR:-$SRC/../stock_analysis_public}"
SHA="$(git -C "$SRC" rev-parse --short HEAD)"

if [ -n "$(git -C "$SRC" status --porcelain --untracked-files=no)" ]; then
    echo "⚠️  Uncommitted changes are NOT published — only HEAD ($SHA) is."
fi

# ── 1. Public working copy ────────────────────────────────────────────
if [ ! -d "$PUBLIC_DIR/.git" ]; then
    if [ -z "${PUBLIC_REMOTE:-}" ]; then
        echo "First run: set PUBLIC_REMOTE=<git url of the public repo>" >&2
        exit 1
    fi
    mkdir -p "$PUBLIC_DIR"
    git -C "$PUBLIC_DIR" init -q -b main
    git -C "$PUBLIC_DIR" remote add origin "$PUBLIC_REMOTE"
fi

# ── 2. Replace its contents with HEAD of this repo ────────────────────
find "$PUBLIC_DIR" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
git -C "$SRC" archive HEAD | tar -x -C "$PUBLIC_DIR"

# Showcase notice, public copy only: inserted after each README's
# title + language-switch line (line 3).
insert_notice() {  # $1 = file, $2 = notice text
    awk -v n="$2" 'NR==3 {print; print ""; print n; next} {print}' "$1" > "$1.tmp" \
        && mv "$1.tmp" "$1"
}
insert_notice "$PUBLIC_DIR/README.md" \
    "> 📌 **展示版。** 本仓库是定期发布的快照，日常开发在私有仓库进行，因此这里的提交记录对应版本发布，而非日常迭代。"
insert_notice "$PUBLIC_DIR/README.en.md" \
    "> 📌 **Showcase snapshot.** This repository is released periodically from a private one where active development happens, so its commit history reflects releases rather than day-to-day work."

# ── 3. Leak scan ──────────────────────────────────────────────────────
HITS=0
scan() {  # $1 = label, $2 = fixed string
    if grep -rIlF --exclude-dir=.git -e "$2" "$PUBLIC_DIR" >/dev/null; then
        echo "❌ $1 found in:" >&2
        grep -rIlF --exclude-dir=.git -e "$2" "$PUBLIC_DIR" | sed "s|$PUBLIC_DIR/|   |" >&2
        HITS=$((HITS + 1))
    fi
}

# Real secret values from the local .env
if [ -f "$SRC/.env" ]; then
    while IFS='=' read -r key val; do
        [[ "$key" =~ ^[[:space:]]*# || -z "$key" ]] && continue
        [[ "$key" =~ (KEY|TOKEN|SECRET|PASSWORD|PASS|USER|EMAIL|CHAT_ID|SCKEY) ]] || continue
        val="$(echo "${val%%#*}" | xargs)"
        [ ${#val} -lt 6 ] && continue
        scan "value of $key" "$val"
    done < "$SRC/.env"
fi

# Personal identifiers and common key formats
scan "home path" "/Users/$(whoami)"
scan "local username" "$(whoami)"
EMAIL="$(git -C "$SRC" config user.email || true)"
[ -n "$EMAIL" ] && [ "$EMAIL" != "your.email@example.com" ] && scan "git email" "$EMAIL"
if grep -rIEl --exclude-dir=.git \
        -e '(sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}|[0-9]{8,10}:AA[A-Za-z0-9_-]{30,})' \
        "$PUBLIC_DIR" >/dev/null; then
    echo "❌ API-key-shaped string found" >&2
    HITS=$((HITS + 1))
fi

if [ "$HITS" -gt 0 ]; then
    echo "Aborted: $HITS leak check(s) failed. Nothing was committed." >&2
    exit 1
fi
echo "✅ Leak scan passed"

# ── 4. Commit (and optionally push) ───────────────────────────────────
git -C "$PUBLIC_DIR" add -A
if git -C "$PUBLIC_DIR" diff --cached --quiet; then
    echo "No changes since the last release."
    exit 0
fi
git -C "$PUBLIC_DIR" commit -q -m "$MSG" -m "Snapshot of private repo @ $SHA"
echo "Committed: $(git -C "$PUBLIC_DIR" log --oneline -1)"

if [ "$PUSH" = "--push" ]; then
    git -C "$PUBLIC_DIR" push -u origin main
else
    echo "Review in $PUBLIC_DIR, then push with: git -C \"$PUBLIC_DIR\" push -u origin main"
fi
