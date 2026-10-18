ARG REGISTRY=docker.io
FROM ${REGISTRY}/library/node:22-alpine AS build
WORKDIR /src
COPY package.json package-lock.json ./
# See backend/Dockerfile: optional CA bundle for TLS-intercepting proxies.
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/extra_ca; fi; \
    npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM ${REGISTRY}/library/nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/dist /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s CMD wget -qO- http://127.0.0.1:8080/ >/dev/null || exit 1
