FROM golang:1.24-bullseye AS aperture-builder
RUN apt-get update && apt-get install -y git make gcc
RUN git clone --depth=1 --branch v0.5.0 https://github.com/lightninglabs/aperture.git /aperture
WORKDIR /aperture
RUN GOTOOLCHAIN=auto CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o /aperture-bin -ldflags="-s -w" ./cmd/aperture/

FROM alpine:3.19
RUN apk add --no-cache nginx curl

RUN curl -L https://github.com/lightningnetwork/lnd/releases/download/v0.18.2-beta/lnd-linux-amd64-v0.18.2-beta.tar.gz \
    | tar -xz --strip-components=1 -C /usr/local/bin/ \
    lnd-linux-amd64-v0.18.2-beta/lnd \
    lnd-linux-amd64-v0.18.2-beta/lncli

COPY --from=aperture-builder /aperture-bin /usr/local/bin/aperture
COPY nginx.conf /etc/nginx/nginx.conf
COPY start.sh /start.sh
RUN chmod +x /start.sh

ENTRYPOINT ["/bin/sh", "/start.sh"]
