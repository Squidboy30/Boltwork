# LNbits Demo Wallet Setup

LNbits instance for the Boltwork demo page — pre-loaded wallet for zero-friction L402 testing.

## Deploy

```bash
cd lnbits
fly apps create boltwork-lnbits --org personal
fly vol create lnbits_data --region lhr --size 1 -a boltwork-lnbits
fly secrets set LND_REST_MACAROON=<admin_macaroon_hex> -a boltwork-lnbits
fly secrets set LND_REST_CERT=<tls_cert_base64> -a boltwork-lnbits
fly secrets set LNBITS_ADMIN_USERS=<your-user-id> -a boltwork-lnbits
fly deploy -a boltwork-lnbits
```

## After deploy

1. Visit https://boltwork-lnbits.fly.dev
2. Create a demo wallet called "Boltwork Demo"
3. Note the wallet API key (Invoice/read key)
4. Set as Fly secret on parsebit: `fly secrets set DEMO_LNBITS_KEY=<key> -a parsebit`
5. Top up the wallet with ~10,000 sats via LNbits UI

## Rate limiting

The demo page limits each IP to 1 free payment per 24h via a counter in the 
parsebit SQLite database. The demo wallet balance is checked before each payment.
If balance drops below 1000 sats, the free demo is disabled with a message to 
use their own wallet.
