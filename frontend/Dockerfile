# --- build: compile the Angular app ---
FROM node:24.22.2-alpine AS build
WORKDIR /src
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npx ng build

# --- runtime: static files + reverse proxy to the gateway, as a non-root user ---
# nginx-unprivileged listens on 8080 and keeps its writable paths group-0 writable,
# so it also runs under OpenShift's arbitrary UID (which is always in group 0).
FROM nginxinc/nginx-unprivileged:1.30.6-alpine
# Where /api/* is forwarded; override per environment (compose, Helm values, OpenShift).
ENV GATEWAY_URL=http://gateway:8002
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /src/dist/frontend/browser /usr/share/nginx/html
EXPOSE 8080
USER 101
