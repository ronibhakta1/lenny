FROM node:20-slim AS base

# Set environment variables for PNPM
ENV PNPM_HOME="/pnpm"
ENV PATH="$PNPM_HOME:$PATH"

# Build arguments - values provided by compose.yaml or --build-arg
ARG NODE_ENV
ARG NEXT_PUBLIC_ENABLE_MANIFEST_ROUTE
ARG NEXT_PUBLIC_MANIFEST_FORCE_ENABLE
ARG NEXT_PUBLIC_MANIFEST_ALLOWED_DOMAINS
ARG MANIFEST_ROUTE_FORCE_ENABLE

# Convert ARGs to ENV so Next.js can access them during build
ENV NODE_ENV=$NODE_ENV
ENV NEXT_PUBLIC_ENABLE_MANIFEST_ROUTE=$NEXT_PUBLIC_ENABLE_MANIFEST_ROUTE
ENV NEXT_PUBLIC_MANIFEST_FORCE_ENABLE=$NEXT_PUBLIC_MANIFEST_FORCE_ENABLE
ENV NEXT_PUBLIC_MANIFEST_ALLOWED_DOMAINS=$NEXT_PUBLIC_MANIFEST_ALLOWED_DOMAINS
ENV MANIFEST_ROUTE_FORCE_ENABLE=$MANIFEST_ROUTE_FORCE_ENABLE

WORKDIR /app

RUN apt-get update && apt-get install -y git

RUN git clone https://github.com/edrlab/thorium-web.git && \
    cd thorium-web
    
RUN corepack enable

WORKDIR /app/thorium-web

RUN --mount=type=cache,id=pnpm_store,target=/pnpm/store pnpm install --frozen-lockfile

RUN pnpm build

EXPOSE 3000

CMD ["pnpm", "start"]
