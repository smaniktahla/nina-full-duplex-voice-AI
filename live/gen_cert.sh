#!/bin/bash
# Self-signed cert valid for the LAN IP / hostnames. Browsers need HTTPS (secure context) to allow microphone access.
cd "$(dirname "$0")"
IP=${1:-$(hostname -I | awk '{print $1}')}
openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem -days 825 -subj "/CN=duplex-live" \
  -addext "subjectAltName=IP:$IP,IP:127.0.0.1,DNS:localhost,DNS:$(hostname)" 2>/dev/null
chmod 600 key.pem
echo "cert for $IP written (cert.pem, key.pem)"
