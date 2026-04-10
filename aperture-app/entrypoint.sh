#!/bin/sh
set -e

mkdir -p /root/.lnd/data/chain/bitcoin/mainnet

if [ -n "$LND_MACAROON_BASE64" ]; then
  printf '%s' "$LND_MACAROON_BASE64" | base64 -d > /root/.lnd/data/chain/bitcoin/mainnet/invoice.macaroon
  echo "Macaroon written OK"
fi

if [ -n "$LND_TLS_CERT_BASE64" ]; then
  printf '%s' "$LND_TLS_CERT_BASE64" | base64 -d > /root/.lnd/tls.cert
  echo "TLS cert written OK"
fi

echo "Starting aperture..."
exec /bin/aperture --configfile=/root/.aperture/aperture.yaml
