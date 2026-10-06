#!/usr/bin/env bash
# mimir-fab provision + deploy. RUN FROM laptop WHEN THE HOMELAB IS REACHABLE
# (personal tailnet / home LAN — db-host resolves). Idempotent. Review before running.
#
#   ./deploy.sh            provision toolchain + deploy service + apply migrations
#   ./deploy.sh --code     redeploy code only (skip apt + migrations)
set -euo pipefail
HOST=db-host
JOB="$(cd "$(dirname "$0")" && pwd)"
LIFEOS="$HOME/Cyber/Dev/Projects/LifeOS/backend"
CODE_ONLY="${1:-}"

echo "== 0. preflight: db-host reachable? =="
ssh -o ConnectTimeout=8 "$HOST" 'hostname' || { echo "db-host unreachable — on the home tailnet/LAN?"; exit 1; }

if [ "$CODE_ONLY" != "--code" ]; then
  echo "== 1. toolchain on db-host (openscad + slicer + xvfb) =="
  ssh "$HOST" 'sudo apt-get update -qq && sudo apt-get install -y openscad xvfb || echo "apt openscad/xvfb: review"; \
    command -v prusa-slicer >/dev/null || echo "NOTE: prusa-slicer not in PATH — install via apt (prusa-slicer) or drop an OrcaSlicer/PrusaSlicer AppImage and set slicer.bin"; \
    echo "openscad: $(command -v openscad || echo MISSING)  xvfb-run: $(command -v xvfb-run || echo MISSING)"'

  echo "== 2. apply migrations 289 (fabrication trust) + 290 (fab cache) =="
  ( cd "$LIFEOS" && ./migrate.sh run 289_fabrication_trust.up.sql && ./migrate.sh run 290_fab_cache.up.sql )

  echo "== 3. fab.env (FAB_API_KEY, MESHY_API_KEY placeholder) =="
  ssh "$HOST" 'f=~/.config/fab.env; if [ ! -f "$f" ]; then \
      printf "FAB_API_KEY=%s\nMESHY_API_KEY=\n" "$(openssl rand -hex 24)" > "$f"; chmod 600 "$f"; \
      echo "created $f (FAB_API_KEY generated; add MESHY_API_KEY from meshy.ai for the decorative path)"; \
    else echo "$f exists — leaving as-is"; fi'
fi

echo "== 4. deploy code =="
ssh "$HOST" 'mkdir -p ~/mimir/fab/profiles ~/mimir/fab/work ~/bin'
scp -q "$JOB"/{db,generate,fabricate,embed,server,search,repair,lint}.py "$JOB/fab-config.json" "$HOST:~/mimir/fab/"
ssh "$HOST" 'mkdir -p ~/mimir/fab/tests/ref'
scp -q "$JOB"/tests/slice_suite.py "$HOST:~/mimir/fab/tests/"
scp -q "$JOB"/tests/ref/*.stl "$HOST:~/mimir/fab/tests/ref/"
scp -q "$JOB/profiles/README.md" "$HOST:~/mimir/fab/profiles/"
scp -q "$JOB/fab.sh" "$HOST:~/bin/fab.sh"
scp -q "$JOB/systemd/mimir-fab.service" "$HOST:~/.config/systemd/user/"
ssh "$HOST" 'chmod +x ~/bin/fab.sh'

echo "== 5. enable + (re)start service =="
ssh "$HOST" 'export XDG_RUNTIME_DIR=/run/user/$(id -u); systemctl --user daemon-reload; \
  systemctl --user enable mimir-fab.service; systemctl --user restart mimir-fab.service; sleep 2; \
  systemctl --user is-active mimir-fab.service'

echo "== 6. health =="
ssh "$HOST" 'curl -sf -m5 http://localhost:8400/health || echo "health check failed — check: journalctl --user -u mimir-fab -n30, and that a slicer profile exists at ~/mimir/fab/profiles/ender3_pla_0.2mm.ini"'
echo
echo "NEXT: drop a PrusaSlicer Ender-3 PLA 0.2mm profile at ~/mimir/fab/profiles/ender3_pla_0.2mm.ini (see profiles/README.md),"
echo "      register the fab endpoint (db-host-tailnet:8400 + FAB_API_KEY) with the Odin gateway, then run the acceptance test (README)."
