# Multi-stage Node build for Vite SPA
FROM node:22-alpine AS builder
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install
COPY . .
# VITE_-prefixed vars are inlined into the bundle at build time. Passed from
# docker-compose (VITE_OTTR_API_KEY=${OTTR_API_KEY}) so the dashboard can
# authenticate its state-changing API calls.
ARG VITE_OTTR_API_KEY
ENV VITE_OTTR_API_KEY=$VITE_OTTR_API_KEY
RUN npm run build

FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 3000
CMD ["nginx", "-g", "daemon off;"]
