#!/bin/sh
# Continuous deployment, run on the VM by cron every 5 min (see crontab.txt).
# GitHub Actions moves the `deploy` branch only after the tests pass; this script pulls it.
# Nothing to do (the usual case): one small `git fetch`, no output.
set -e
cd "$HOME/woodlands-checkpoint-bus-study"

git fetch -q origin deploy
# old = the last commit whose deploy finished (pip and crontab included), kept in git config. Not HEAD: if pip or
# crontab failed after the merge, HEAD is already new, and comparing with it would skip the failed step for good.
# (First run: start the record at HEAD, before merging, so even that first deploy can be retried.)
old=$(git config --get deploy.done) || { old=$(git rev-parse HEAD); git config deploy.done "$old"; }
new=$(git rev-parse origin/deploy)
[ "$old" = "$new" ] && exit 0

changed=$(git diff --name-only "$old" "$new")
git merge -q --ff-only origin/deploy
echo "$(date '+%F %T') deployed $(git rev-parse --short "$old") -> $(git rev-parse --short "$new"): $(git log -1 --format=%s)"

# New Python packages: install them into the venv.
if echo "$changed" | grep -qx requirements.txt; then
  .venv/bin/pip install -q -r requirements.txt
  echo "  requirements installed"
fi
# Schedule changed: reinstall the crontab.
if echo "$changed" | grep -qx deploy/crontab.txt; then
  crontab deploy/crontab.txt
  echo "  crontab reinstalled"
fi
# Every step worked (set -e stops above on any failure, so a failed step is retried in 5 min)
git config deploy.done "$new"
