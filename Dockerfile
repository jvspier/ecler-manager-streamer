# The Ecler VEO Manager. The streamer is deliberately not containerised --
# see streamer/README.md for why: its lifecycle model is systemd, and it needs
# a real network interface to send multicast from.
#
#   docker build -t eclermanager .
#   docker run -d --network host -v ./config:/etc/eclermanager eclermanager
#
# There is nothing to compile and nothing to install: the application is the
# Python standard library and these files.
FROM python:3.13-slim

LABEL org.opencontainers.image.title="Ecler VEO Manager" \
      org.opencontainers.image.description="Dashboard for Ecler VEO H.264 video-over-IP extenders" \
      org.opencontainers.image.source="https://github.com/jvspier/ecler-manager-streamer" \
      org.opencontainers.image.licenses="MIT"

# A fixed uid so a bind-mounted config directory can be chowned to something
# predictable on the host.
RUN useradd --system --uid 10001 --user-group \
            --home-dir /var/lib/eclermanager --create-home eclermanager

WORKDIR /opt/eclermanager
COPY eclermanager/ ./eclermanager/
COPY tools/ ./tools/
COPY run.py config.example.json devices.example.txt ./
COPY deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
 && install -d -o eclermanager -g eclermanager /etc/eclermanager /var/lib/eclermanager \
 && find /opt/eclermanager -name '__pycache__' -type d -prune -exec rm -rf {} +

USER eclermanager
EXPOSE 8477

# /etc/eclermanager holds config.json and, if you use one, the credentials
# file. /var/lib/eclermanager holds the event log. Mount both to keep them.
VOLUME ["/etc/eclermanager", "/var/lib/eclermanager"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD \
  python3 -c "import sys,urllib.request; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8477/api/health', timeout=4).status == 200 else 1)"

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["--config", "/etc/eclermanager/config.json", \
     "--event-log", "/var/lib/eclermanager/events.jsonl", \
     "--host", "0.0.0.0", "--port", "8477"]
