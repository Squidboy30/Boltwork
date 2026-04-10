#!/bin/sh
set -e

# Write LND TLS cert from secret
mkdir -p /root/.lnd/data/chain/bitcoin/mainnet
if [ -n "$LND_TLS_CERT" ]; then
  printf '%s' "$LND_TLS_CERT" > /root/.lnd/tls.cert
fi

# Write admin macaroon from secret (base64 encoded)
if [ -n "$LND_ADMIN_MACAROON_B64" ]; then
  printf '%s' "$LND_ADMIN_MACAROON_B64" | base64 -d > /root/.lnd/data/chain/bitcoin/mainnet/admin.macaroon
fi

mkdir -p /root/.aperture
exec aperture --configfile /root/.aperture/aperture.yaml
