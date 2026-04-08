#!/bin/sh
# Start nginx first — binds 0.0.0.0:8079 immediately for Fly health check
nginx -g "daemon off;" &
echo "[startup] nginx started on :8079"
# Start LND with multiple neutrino peers for reliability
lnd \
  --bitcoin.active \
  --bitcoin.mainnet \
  --bitcoin.node=neutrino \
  --neutrino.addpeer=btcd-mainnet.lightning.computer \
  --neutrino.addpeer=neutrino.noderunner.wtf \
  --neutrino.addpeer=node.eldamar.icu \
  --restlisten=0.0.0.0:8082 \
  --rpclisten=0.0.0.0:10009 \
  --listen=0.0.0.0:9735 \
  --alias=parsebit \
  --tlsextraip=0.0.0.0 \
  --fee.url=https://nodes.lightning.computer/fees/v1/btc-fee-estimates.json \
  --noseedbackup &
LND_PID=$!
echo "[startup] LND started"
# Wait for macaroon
echo "[startup] Waiting for LND macaroon..."
while [ ! -f /root/.lnd/data/chain/bitcoin/mainnet/invoice.macaroon ]; do
  sleep 2
done
echo "[startup] Macaroon ready. Waiting 5s for gRPC..."
sleep 5
# Write Aperture config
mkdir -p /root/.lnd/.aperture
mkdir -p /usr/share/nginx/html/.well-known
echo '7b2f6a669e99a4f0e6f0e7160a9f6528cdf04696457da0074ae9ec8804a24846' > /usr/share/nginx/html/.well-known/402index-verify.txt
cat > /root/.lnd/.aperture/aperture.yaml << 'CONF'
listenaddr: "127.0.0.1:8080"
debuglevel: "info"
autocert: false
insecure: true
authenticator:
  network: "mainnet"
  lndhost: "localhost:10009"
  tlspath: "/root/.lnd/tls.cert"
  macdir: "/root/.lnd/data/chain/bitcoin/mainnet/"
  disable: false
dbbackend: "sqlite"
sqlite:
  dbfile: "/root/.lnd/.aperture/aperture.db"
services:
  - name: "well-known"
    hostregexp: 'parsebit-lnd.fly.dev'
    pathregexp: '^/.well-known/.*$'
    address: "parsebit.fly.dev"
    protocol: https
    price: 0
  - name: "summarise-upload"
    hostregexp: 'parsebit-lnd.fly.dev'
    pathregexp: '^/summarise/upload.*$'
    address: "parsebit.fly.dev"
    protocol: https
    price: 50
  - name: "summarise-url"
    hostregexp: 'parsebit-lnd.fly.dev'
    pathregexp: '^/summarise/url.*$'
    address: "parsebit.fly.dev"
    protocol: https
    price: 50
  - name: "review-code"
    hostregexp: 'parsebit-lnd.fly.dev'
    pathregexp: '^/review/code.*$'
    address: "parsebit.fly.dev"
    protocol: https
    price: 2000
  - name: "review-url"
    hostregexp: 'parsebit-lnd.fly.dev'
    pathregexp: '^/review/url.*$'
    address: "parsebit.fly.dev"
    protocol: https
    price: 2000
  - name: "extract-webpage"
    hostregexp: 'parsebit-lnd.fly.dev'
    pathregexp: '^/extract/webpage.*$'
    address: "parsebit.fly.dev"
    protocol: https
    price: 100
  - name: "extract-data"
    hostregexp: 'parsebit-lnd.fly.dev'
    pathregexp: '^/extract/data.*$'
    address: "parsebit.fly.dev"
    protocol: https
    price: 200
  - name: "translate"
    hostregexp: 'parsebit-lnd.fly.dev'
    pathregexp: '^/translate.*$'
    address: "parsebit.fly.dev"
    protocol: https
    price: 150
  - name: "public"
    hostregexp: 'parsebit-lnd.fly.dev'
    pathregexp: '^/.*$'
    address: "parsebit.fly.dev"
    protocol: https
    price: 0
CONF
# Start Aperture
/usr/local/bin/aperture --configfile=/root/.lnd/.aperture/aperture.yaml &
echo "[startup] Aperture started"
wait $LND_PID