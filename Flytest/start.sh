#!/bin/sh
# Test nginx config
nginx -t 2>&1
# Start nginx
nginx -g "daemon off;" &
echo "nginx started on :8079"
# Start Flask
python /app.py
