#!/bin/bash
# deploy-frontend.sh <image-tag> - blue/green swap of the Next.js container, zero downtime.
#
# Called by run-deploy.sh with the tag the frontend workflow just pushed (sha-<commit>), or by
# hand: `bash deploy/vps/deploy-frontend.sh latest`.
#
# How the swap works. nginx does not proxy to a port; it proxies to `upstream afc_frontend`,
# whose single `server 127.0.0.1:PORT;` line lives in /etc/nginx/afc-frontend-upstream.conf and
# is the only thing this script rewrites. Two ports alternate, 3000 and 3001:
#   1. read which port is live from that file
#   2. pull the image, start the NEW container on the OTHER port (container afc-frontend-<port>)
#   3. poll it until GET / answers 200 (up to 2 minutes; a cold Next.js start is ~10 s)
#   4. write the other port into the upstream file, nginx -t, systemctl reload nginx. Reload is
#      graceful: old nginx workers finish their in-flight requests, new ones use the new port
#   5. stop whatever container was publishing the old port (30 s grace), remove it, prune images
# A container that never answers 200 is removed and the script exits 1 with its logs; the
# upstream file is untouched, so the old container keeps serving and the GitHub run goes red.
#
# What a user sees: nothing. A tab already open keeps its current page; on the next navigation
# it gets the new build. If it asks the new container for a chunk that only the old build had,
# Next.js hard-reloads that tab once (its standard ChunkLoadError handling).
#
# AFC_FAKE_BAD_IMAGE=1 replaces the pull with a container that never speaks HTTP, to prove the
# failure path (gate P7 in GATES-push-to-deploy.md). Never set it in a workflow.

set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
TAG="${1:?image tag required, e.g. sha-abc1234 or latest}"
IMAGE="afctech/afc-frontend:${TAG}"
UPSTREAM=/etc/nginx/afc-frontend-upstream.conf
t0=$(date +%s)

cur=$(grep -oE '300[01]' "$UPSTREAM" 2>/dev/null | head -1 || true)
cur="${cur:-3000}"
new=$(( cur == 3000 ? 3001 : 3000 ))
name="afc-frontend-$new"
echo "live port $cur -> deploying $IMAGE on $new as $name"

docker rm -f "$name" >/dev/null 2>&1 || true
if [ "${AFC_FAKE_BAD_IMAGE:-0}" = "1" ]; then
  docker run -d --name "$name" -p "127.0.0.1:$new:3000" alpine:3 sleep 300 >/dev/null
else
  docker pull -q "$IMAGE"
  docker run -d --name "$name" -p "127.0.0.1:$new:3000" --restart unless-stopped \
    --log-opt max-size=20m --log-opt max-file=5 "$IMAGE" >/dev/null
fi

code=""
for i in $(seq 1 60); do
  code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' "http://127.0.0.1:$new/" || true)
  [ "$code" = "200" ] && break
  sleep 2
done
if [ "$code" != "200" ]; then
  echo "new container never answered 200 on :$new (last: '$code'); traffic NOT moved. Its logs:"
  docker logs --tail 40 "$name" 2>&1 || true
  docker rm -f "$name" >/dev/null 2>&1 || true
  exit 1
fi

printf 'upstream afc_frontend { server 127.0.0.1:%s; }\n' "$new" | sudo tee "$UPSTREAM.tmp" >/dev/null
sudo mv "$UPSTREAM.tmp" "$UPSTREAM"
sudo nginx -t
sudo systemctl reload nginx
sleep 2
# nginx is on the new port from here; retire everything that still publishes the old one
for old in $(docker ps -q --filter "publish=$cur"); do
  echo "stopping old container $(docker inspect -f '{{.Name}}' "$old")"
  docker stop -t 30 "$old" >/dev/null && docker rm "$old" >/dev/null
done
docker image prune -f >/dev/null

echo "DEPLOYED frontend $IMAGE on :$new in $(( $(date +%s) - t0 )) s"
