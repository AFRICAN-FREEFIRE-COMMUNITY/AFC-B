#!/bin/bash
# probe-old-chunk.sh - prove that a chunk of the PREVIOUS build is still served after a swap.
#
# 1. read the live build's id from its container; its /_next/static/<buildId>/_buildManifest.js
#    exists in that build only
# 2. swap to the other image tag given as $1 (default: the sha tag that is NOT live), which
#    retires the current container
# 3. request the OLD manifest again through nginx: with static retention it answers 200 from
#    /var/www/afc-next (X-AFC-Static: disk); without it, 404
# 4. swap back so the live tag is unchanged afterwards
# Gate S2 in GATES-hardening-2026-09-11.md.
set -u
res="africanfreefirecommunity.com:443:127.0.0.1"
live_name=$(docker ps --format '{{.Names}}' | grep '^afc-frontend-' | head -1)
live_tag=$(docker inspect -f '{{.Config.Image}}' "$live_name" | sed 's/.*://')
# every build has a unique id directory under .next/static holding its manifests; that file
# exists ONLY in that build, so it is the strictest possible "old chunk"
build_id=$(docker exec "$live_name" sh -c 'ls .next/static | grep -vE "^(chunks|media)$" | head -1')
old="/_next/static/$build_id/_buildManifest.js"
[ -n "$build_id" ] || { echo "no build id dir in the live container"; exit 1; }
other="${1:-}"
if [ -z "$other" ]; then
  other=$(docker images afctech/afc-frontend --format '{{.Tag}}' | grep -E '^sha-' | grep -v "^$live_tag$" | head -1)
fi
[ -n "$other" ] || { echo "no other sha- image available locally to swap to"; exit 1; }
echo "live tag $live_tag, old manifest $old, swapping to $other"
bash /home/ubuntu/AFC-B/deploy/vps/deploy-frontend.sh "$other" >/dev/null 2>&1 || { echo "swap failed"; exit 1; }
code=$(curl -sk -o /dev/null -w '%{http_code}' --resolve "$res" "https://africanfreefirecommunity.com$old")
src=$(curl -skI --resolve "$res" "https://africanfreefirecommunity.com$old" | grep -i x-afc-static | tr -d '\r')
echo "old chunk after swap: $code ${src:-(not from disk)}"
bash /home/ubuntu/AFC-B/deploy/vps/deploy-frontend.sh "$live_tag" >/dev/null 2>&1 && echo "swapped back to $live_tag"
[ "$code" = "200" ]
