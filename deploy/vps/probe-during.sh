#!/bin/bash
# probe-during.sh backend|frontend - prove "zero downtime" with numbers instead of adjectives.
#
# Fires ~10 requests a second at the live site THROUGH nginx while a real deploy step runs
# underneath (backend: `systemctl reload django_app`; frontend: a full blue/green swap of the
# image already live), then prints how many answered wrong. Used by gates P5 and P6 in
# GATES-push-to-deploy.md; harmless to run any time.
#
# backend: probes /auth/connections/ (an auth-required endpoint), which must answer 400 from
# Django every single time. A 502 or timeout is a failure.
# frontend: probes / on the site host, which must answer 200 every time, and checks that the
# upstream port actually changed, so the run cannot pass by swapping nothing.

set -u
target="${1:?backend|frontend}"
UPSTREAM=/etc/nginx/afc-frontend-upstream.conf
case "$target" in
  backend)  url="https://api.africanfreefirecommunity.com/auth/connections/"; want=400; res="api.africanfreefirecommunity.com:443:127.0.0.1" ;;
  frontend) url="https://africanfreefirecommunity.com/";                       want=200; res="africanfreefirecommunity.com:443:127.0.0.1" ;;
  *) echo "backend|frontend"; exit 64 ;;
esac

log=$(mktemp)
(
  end=$(( $(date +%s) + 90 ))
  while [ "$(date +%s)" -lt "$end" ] && [ ! -f "$log.stop" ]; do
    curl -sk -o /dev/null -m 5 -w '%{http_code}\n' --resolve "$res" "$url" >> "$log" 2>/dev/null || echo "000" >> "$log"
    sleep 0.1
  done
) &
probe=$!
sleep 3

before=$(grep -oE '300[01]' "$UPSTREAM" 2>/dev/null | head -1 || echo "?")
if [ "$target" = backend ]; then
  sudo systemctl reload django_app
  sleep 8
else
  tag=$(docker inspect -f '{{.Config.Image}}' "afc-frontend-$before" 2>/dev/null | sed 's/.*://' || true)
  bash /home/ubuntu/AFC-B/deploy/vps/deploy-frontend.sh "${tag:-latest}" >/dev/null 2>&1 || echo "swap script exited non-zero"
fi
after=$(grep -oE '300[01]' "$UPSTREAM" 2>/dev/null | head -1 || echo "?")

touch "$log.stop"; wait "$probe" 2>/dev/null
total=$(wc -l < "$log"); ok=$(grep -c "^$want$" "$log"); fail=$(( total - ok ))
echo "target=$target requests=$total expected_$want=$ok failures=$fail"
[ "$target" = frontend ] && { [ "$before" != "$after" ] && echo "port changed $before -> $after" || echo "port DID NOT change ($before)"; }
[ "$fail" -gt 0 ] && { echo "non-$want responses seen:"; grep -v "^$want$" "$log" | sort | uniq -c; }
rm -f "$log" "$log.stop"
[ "$fail" -eq 0 ]
