#!/usr/bin/env bash
# Enforce the "nothing proprietary" rule from CLAUDE.md.
#
# The property being held: OpenAIDR installs and runs correctly for someone who
# has never heard of any closed-source consumer of it. That rule was stated in
# CLAUDE.md as "one grep in CI" for a long time before the grep existed; this
# is the grep.
#
# Add a term here when a new closed product could plausibly be name-dropped by
# an agent or a contributor working across repos. Keep it lowercase; the search
# is case-insensitive.
set -euo pipefail

TERMS=(
  'stacktrace'
)

# This script names the terms it forbids, so it cannot search itself. Nor the
# lockfile (hashes produce false positives) or the VCS/venv/cache directories.
readonly SELF="scripts/check-no-proprietary.sh"

status=0
for term in "${TERMS[@]}"; do
  # `git grep` searches tracked files only, which is exactly the set that would
  # be published. -I skips binaries, -n gives line numbers, -i is case-insensitive.
  if matches=$(git grep -Iin -e "$term" -- \
        ':!uv.lock' \
        ":!$SELF" \
        2>/dev/null); then
    echo "::error::Forbidden proprietary reference '$term' in tracked files:"
    echo "$matches"
    status=1
  fi
done

if [ "$status" -eq 0 ]; then
  echo "No proprietary references found."
fi
exit "$status"
