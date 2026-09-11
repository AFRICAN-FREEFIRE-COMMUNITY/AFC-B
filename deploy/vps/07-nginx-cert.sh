#!/bin/bash
# 07-nginx-cert.sh - certificate via Cloudflare DNS-01, then the nginx site. Run on the VPS as ubuntu.
#
# WHY DNS-01 (chat decision 2, 2026-09-11): the certificate is issued BEFORE DNS points at this
# box, so the hosts-file rehearsal in Chrome gets a real padlock and Cloudflare can stay on
# Full (strict). HTTP-01 through the orange cloud is unreliable (Always Use HTTPS redirects the
# challenge at the edge) and TLS-ALPN-01 cannot work behind a proxy at all. Renewal keeps working
# whatever the proxy state.
#
# Needs ONE input from the owner: a Cloudflare API token with permission "Zone / DNS / Edit" on
# this zone only. It is written to /etc/letsencrypt/cloudflare.ini (mode 600, root) by the operator
# BEFORE this script runs; the script refuses to start without it. The token is never echoed.
#
# Proven by gates D5, D6.

set -eu
KIT=/home/ubuntu/deploy-vps
INI=/etc/letsencrypt/cloudflare.ini
EMAIL="${CERT_EMAIL:-info@africanfreefirecommunity.com}"

sudo test -s "$INI" || { echo "missing $INI (dns_cloudflare_api_token = ...). Ask the owner for the token first."; exit 1; }
sudo chmod 600 "$INI"

sudo certbot certonly \
  --dns-cloudflare --dns-cloudflare-credentials "$INI" --dns-cloudflare-propagation-seconds 30 \
  -d africanfreefirecommunity.com -d www.africanfreefirecommunity.com -d api.africanfreefirecommunity.com -d www.api.africanfreefirecommunity.com \
  --cert-name africanfreefirecommunity.com \
  --non-interactive --agree-tos --email "$EMAIL" --no-eff-email

# certbot's nginx plugin ships these two; certonly with the DNS plugin does not create them.
sudo test -f /etc/letsencrypt/options-ssl-nginx.conf || sudo cp /usr/lib/python3/dist-packages/certbot_nginx/_internal/tls_configs/options-ssl-nginx.conf /etc/letsencrypt/options-ssl-nginx.conf
sudo test -f /etc/letsencrypt/ssl-dhparams.pem || sudo openssl dhparam -out /etc/letsencrypt/ssl-dhparams.pem 2048

# Ubuntu 24.04's adduser creates /home/ubuntu as 750, so nginx (www-data) cannot reach
# /home/ubuntu/AFC-B/media and every upload 403s. AWS has 755; match it. (Found 2026-09-11 15:05.)
sudo chmod 755 /home/ubuntu

# maintenance page (deploy/maintenance/README.md)
sudo install -d -o www-data -g www-data /var/www/afc-maintenance
sudo install -o www-data -g www-data -m 644 "$KIT/maintenance.html" /var/www/afc-maintenance/maintenance.html

# site
sudo cp "$KIT/nginx-afc.conf" /etc/nginx/sites-available/afc
sudo ln -sf /etc/nginx/sites-available/afc /etc/nginx/sites-enabled/afc
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx

# reload nginx after every renewal so the new cert is actually served
sudo install -d /etc/letsencrypt/renewal-hooks/deploy
printf '#!/bin/sh\nsystemctl reload nginx\n' | sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh >/dev/null
sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
sudo systemctl enable --now certbot.timer

echo "NGINX_CERT_OK"
sudo certbot certificates 2>/dev/null | grep -E "Domains|Expiry"
systemctl is-active nginx certbot.timer
curl -sk -o /dev/null -w "api via nginx (expect 400): %{http_code}\n" --resolve api.africanfreefirecommunity.com:443:127.0.0.1 https://api.africanfreefirecommunity.com/auth/connections/
curl -sk -o /dev/null -w "site via nginx (expect 200): %{http_code}\n" --resolve africanfreefirecommunity.com:443:127.0.0.1 https://africanfreefirecommunity.com/
