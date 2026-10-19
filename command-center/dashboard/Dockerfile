ARG REGISTRY=docker.io
FROM ${REGISTRY}/library/node:22-alpine AS build
WORKDIR /src
COPY package.json package-lock.json ./
# See backend/Dockerfile: optional CA bundle for TLS-intercepting proxies.
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/extra_ca; fi; \
    npm ci --no-audit --no-fund
COPY . .
# See backend/Dockerfile: the copied package.json must be this release's version.
ARG APP_VERSION=""
RUN if [ -n "$APP_VERSION" ]; then node -e "const v = require('./package.json').version; if (v !== process.argv[1]) { console.error('package.json in this build says ' + v + ', but this release is ' + process.argv[1] + ': Docker used an out-of-date copy of the dashboard folder'); process.exit(1); }" "$APP_VERSION"; fi
RUN npm run build

FROM ${REGISTRY}/library/nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/dist /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s CMD wget -qO- http://127.0.0.1:8080/ >/dev/null || exit 1
