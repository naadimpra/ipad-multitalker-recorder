#!/usr/bin/env bash
# Creates a locally-trusted HTTPS cert for this laptop's LAN IP (Safari needs HTTPS for the mic).
set -euo pipefail
cd "$(dirname "$0")"
command -v mkcert >/dev/null || { echo "Install mkcert first:  brew install mkcert"; exit 1; }
IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo 127.0.0.1)
HOST=$(scutil --get LocalHostName 2>/dev/null || hostname -s)
mkdir -p certs
mkcert -install
mkcert -cert-file certs/cert.pem -key-file certs/key.pem "$IP" "$HOST.local" localhost 127.0.0.1
echo
echo "Cert created for $IP / $HOST.local"
echo "If the laptop's IP changes (different Wi-Fi), run this script again."
