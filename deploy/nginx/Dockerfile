# NetRollout's entry point: nginx in front of the app (HTTPS on 443, HTTP on
# 80 redirected), with a watcher that applies a new hostname / port / certificate
# without a restart and never trades a working config for a broken one.
#
#   docker build -t netrollout-nginx deploy/nginx
#
# Environment (set by the installer; the app's shared/site.env overrides the
# first two at run time — stage 8):
#   NETROLLOUT_HOSTNAME     canonical name; other names redirect to it (IPs don't)
#   NETROLLOUT_HTTPS_PORT   the port people type (published 443 by default)
#   APP_UPSTREAM            the app, default app:8080
# Mounts: /etc/nginx/certs (fullchain.pem + privkey.pem),
#         /etc/nginx/netrollout/shared (site.env in, status.json out)

FROM nginx:1.31-alpine

ENV NETROLLOUT_HOSTNAME="" \
    NETROLLOUT_HTTPS_PORT=443 \
    APP_UPSTREAM=app:8080

COPY nginx.conf /etc/nginx/nginx.conf
COPY site.conf.template /etc/nginx/netrollout/site.conf.template
COPY --chmod=755 netrollout-render.sh netrollout-watcher.sh /usr/local/bin/
COPY --chmod=755 40-netrollout.sh /docker-entrypoint.d/

RUN rm -f /etc/nginx/conf.d/default.conf && \
    mkdir -p /etc/nginx/certs /etc/nginx/netrollout/active \
             /etc/nginx/netrollout/staging /etc/nginx/netrollout/shared
