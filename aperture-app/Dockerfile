FROM lightninglabs/aperture:v0.5.0
COPY aperture.yaml /root/.aperture/aperture.yaml
COPY start.sh /start.sh
RUN chmod +x /start.sh
EXPOSE 8081
ENTRYPOINT ["/start.sh"]
