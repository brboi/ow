#!/usr/bin/env bash
# PROTOTYPE — build a scratch fleet under $OW3_PROTO_HOME: two upstream repos, their ow bare
# repos, and two workspaces (`voip` on a branch with unpushed work, `stock` detached).
# Then move both upstreams ahead so `ow reset` has something to do.
set -euo pipefail
H=${OW3_PROTO_HOME:-/tmp/ow3-proto-WIPE-ME}
rm -rf "$H" && mkdir -p "$H"/{upstream,repos,ws}
export GIT_AUTHOR_NAME=p GIT_AUTHOR_EMAIL=p@p GIT_COMMITTER_NAME=p GIT_COMMITTER_EMAIL=p@p

commit() { echo "$2" >> "$1/f" && git -C "$1" add f && git -C "$1" commit -qm "$2"; }

for r in community enterprise; do
  git init -qb main "$H/upstream/$r" && commit "$H/upstream/$r" "$r 1"
  b="$H/repos/$r.git"
  git init -q --bare "$b"
  git -C "$b" remote add origin "$H/upstream/$r"
  git -C "$b" config core.logAllRefUpdates true
  git -C "$b" fetch -q origin main && git -C "$b" remote set-head origin main
done

for ws in voip stock; do
  mkdir -p "$H/ws/$ws/.ow"
  printf '[bases]\ncommunity = "origin/main"\nenterprise = "origin/main"\n' > "$H/ws/$ws/.ow/config.toml"
  for r in community enterprise; do
    git -C "$H/repos/$r.git" worktree add -q --detach "$H/ws/$ws/$r" origin/main
  done
done
for r in community enterprise; do
  git -C "$H/ws/voip/$r" switch -q --no-track -c 18.0-voip origin/main
  git -C "$H/ws/voip/$r" config branch.18.0-voip.ow-base origin/main
done
commit "$H/ws/voip/enterprise" "local voip work (unpushed)"

for r in community enterprise; do commit "$H/upstream/$r" "$r 2"; done

printf '["%s", "%s", "%s"]\n' "$H/ws/voip" "$H/ws/stock" "$H/ws/gone" > "$H/index.json"
echo "fleet ready in $H"
