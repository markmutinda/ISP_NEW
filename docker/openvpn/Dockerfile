FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    openvpn iptables iproute2 curl ca-certificates openssl \
    && rm -rf /var/lib/apt/lists/*
COPY docker/openvpn/entrypoint.sh /usr/local/bin/ovpn-entrypoint.sh
COPY docker/openvpn/auth.sh /etc/openvpn/auth.sh
RUN sed -i 's/\r$//' /usr/local/bin/ovpn-entrypoint.sh /etc/openvpn/auth.sh \
    && chmod 755 /usr/local/bin/ovpn-entrypoint.sh /etc/openvpn/auth.sh
EXPOSE 1194/tcp
ENTRYPOINT ["/usr/local/bin/ovpn-entrypoint.sh"]