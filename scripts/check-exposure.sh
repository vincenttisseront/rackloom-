#!/usr/bin/env bash
# T28 : seul 127.0.0.1:8443 est publié ; PostgreSQL, Ollama et le launcher sont injoignables depuis l'hôte.
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PROJECT=${RACKLOOM_COMPOSE_PROJECT:-rackloom}
FAIL=0
ok()   { printf '[ OK ] %s\n' "$*"; }
ko()   { printf '[FAIL] %s\n' "$*"; FAIL=1; }

mapfile -t containers < <(docker ps --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Names}}')
[ "${#containers[@]}" -gt 0 ] || { echo "aucun conteneur du projet $PROJECT en cours d'exécution"; exit 1; }

published=$(for c in "${containers[@]}"; do
  docker inspect -f '{{range $p, $b := .NetworkSettings.Ports}}{{range $b}}{{$.Name}} {{.HostIp}}:{{.HostPort}}->{{$p}}{{"\n"}}{{end}}{{end}}' "$c"
done | sed '/^$/d')
echo "Ports publiés :"
echo "${published:-  (aucun)}" | sed 's/^/  /'
if [ "$(echo "$published" | sed '/^$/d' | wc -l)" -eq 1 ] && echo "$published" | grep -q ' 127.0.0.1:8443->8443/tcp$'; then
  ok "seul 127.0.0.1:8443 est publié"
else
  ko "ports publiés inattendus"
fi

for c in "${containers[@]}"; do
  if [ "$(docker inspect -f '{{.HostConfig.Privileged}}' "$c")" = true ]; then ko "$c est privilégié"; fi
  if [ "$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$c")" = host ]; then ko "$c utilise network_mode host"; fi
  if docker inspect -f '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}' "$c" | grep -q 'docker.sock'; then
    case "$c" in *launcher*) ;; *) ko "$c monte le socket du moteur de conteneurs";; esac
  fi
done
ok "aucun conteneur privilégié ni en réseau hôte ; socket du moteur seulement dans le launcher"

probe() {  # probe <nom> <ip> <port>
  if timeout 2 bash -c "exec 3<>/dev/tcp/$2/$3" 2>/dev/null; then ko "$1 joignable depuis l'hôte ($2:$3)"; else ok "$1 injoignable depuis l'hôte ($2:$3)"; fi
}
for svc in postgres:5432 api:8000 ollama:11434; do
  name=${svc%%:*} port=${svc##*:}
  c=$(docker ps --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=$name" \
      --format '{{.Names}}' | head -n1)
  [ -n "$c" ] || { ok "$name non démarré"; continue; }
  for ip in $(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$c"); do
    probe "$name" "$ip" "$port"
  done
done
launcher=$(docker ps --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=launcher" --format '{{.Names}}' | head -n1)
if [ -n "$launcher" ]; then
  if [ "$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$launcher")" = none ]; then
    ok "launcher sans réseau (socket Unix seulement)"
  else
    ko "launcher attaché à un réseau"
  fi
fi
exit $FAIL
