# The browser bundle is built by a pinned Node stage and served by a pinned
# Nginx stage, so the runtime image carries no toolchain, no source and no
# package manager. Nginx is also the single public entry point: it serves the
# static assets and reverse-proxies /api to the API service (FIN-012).
FROM node:22.23.2-bookworm-slim AS build

WORKDIR /workspace

# ``npm ci`` needs both files and is only re-run when the lock changes.
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY apps/web/tsconfig.json apps/web/tsconfig.app.json apps/web/tsconfig.node.json ./
COPY apps/web/vite.config.ts apps/web/index.html ./
COPY apps/web/src ./src

RUN npm run build


FROM nginx:1.29.3-alpine AS runtime

# The image ships a default.conf that owns 0.0.0.0:80 with a catch-all; replacing
# it is what makes our server block authoritative rather than a second one.
RUN rm -f /etc/nginx/conf.d/default.conf

COPY deploy/nginx/nginx.conf /etc/nginx/nginx.conf
COPY deploy/nginx/templates/default.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /workspace/dist /usr/share/nginx/html

# Only the template is copied into deploy/nginx/templates; an unexpanded copy
# under conf.d would shadow it and serve the literal ${API_UPSTREAM}.
COPY deploy/nginx/security-headers.conf /etc/nginx/snippets/security-headers.conf

EXPOSE 8080

# The image entrypoint runs envsubst over /etc/nginx/templates/*.template and
# writes the result to /etc/nginx/conf.d/ before starting Nginx, so the API
# upstream is configurable per environment without rebuilding the image.
ENV NGINX_ENVSUBST_OUTPUT_DIR=/etc/nginx/conf.d \
    API_UPSTREAM=http://api:8000 \
    NGINX_PORT=8080

# BusyBox wget is present in the Alpine base and reaches Nginx over loopback, so
# readiness does not depend on a shell being installed in the probe path.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=10 \
    CMD wget --quiet --tries=1 --spider "http://127.0.0.1:${NGINX_PORT}/healthz" || exit 1

CMD ["nginx", "-g", "daemon off;"]
