#!/bin/bash
# For a session that edited files in the legacy trees (~/claude-workspace/duplex-sim, ~/needle-voice) before the migration:
# 3-way merge the old file into your WORKTREE copy so the change becomes a normal PR.
#
#   scripts/import_from_old_tree.sh <repo-relative-path> <base-file>
#     path       e.g. live/server.py  (the legacy tree is chosen from the path; needle-voice/... maps to ~/needle-voice/...)
#     base-file  the legacy file as it was BEFORE your edit (e.g. live/server.py.pre-responder). If you have no backup: git diff against
#                the repo's first commit is NOT a valid base (the import also scrubbed personal data); ask for help.
#
# Result: your worktree file now contains main's version + your change. Conflict markers mean you touched a line the scrub also touched
# (hosts, calendars): keep main's `household.get(...)` lines. NEVER copy a legacy file over the repo file: it would undo the scrub
# and re-introduce personal data into a public repository.
set -eu
P=$1; BASE=$2
case "$P" in needle-voice/*) OLD=$HOME/needle-voice/${P#needle-voice/};; *) OLD=$HOME/claude-workspace/duplex-sim/$P;; esac
[ -f "$OLD" ] || { echo "no legacy file $OLD"; exit 1; }
git merge-file -p "$P" "$BASE" "$OLD" > "$P.merged" && echo "clean merge" || echo "merged WITH CONFLICTS (see markers)"
mv "$P.merged" "$P"
git --no-pager diff --stat -- "$P"
echo "now run: git grep -nE 'maniktahla|10\\.10\\.10\\.|calendar\\.[a-z]+_[a-z]+' -- $P   (must show nothing new)"
