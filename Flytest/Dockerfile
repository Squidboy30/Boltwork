FROM python:3.11-alpine

RUN apk add --no-cache nginx

RUN pip install flask

COPY app.py /app.py
COPY nginx.conf /etc/nginx/nginx.conf
COPY start.sh /start.sh
RUN chmod +x /start.sh

ENTRYPOINT ["/bin/sh", "/start.sh"]
