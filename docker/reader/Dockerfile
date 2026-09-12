FROM node:22-slim AS base

# Set environment variables for PNPM
ENV PNPM_HOME="/pnpm"
ENV PATH="$PNPM_HOME:$PATH"
ENV CI=true

# Build arguments - values provided by compose.yaml or --build-arg
ARG NODE_ENV
ARG MANIFEST_ROUTE_FORCE_ENABLE
ARG MANIFEST_ALLOWED_DOMAINS

# Convert ARGs to ENV so Next.js can access them during build
ENV NODE_ENV=$NODE_ENV
ENV MANIFEST_ROUTE_FORCE_ENABLE=$MANIFEST_ROUTE_FORCE_ENABLE
ENV MANIFEST_ALLOWED_DOMAINS=$MANIFEST_ALLOWED_DOMAINS

WORKDIR /app

RUN apt-get update && apt-get install -y git

# Pin thorium-web to a specific commit (default branch: develop).
# To upgrade the reader: replace THORIUM_REF with a newer commit SHA. Changing
# this value invalidates the build cache from this line onward — deterministic
# on both BuildKit and the classic builder — so `make update` re-clones and
# checks out the new commit. No floating HEAD = no surprise upstream drift.
ARG THORIUM_REF=071c8bbf57d67396d9f7b2dca622099529a15b0a

RUN git clone https://github.com/edrlab/thorium-web.git && \
    cd thorium-web && \
    git checkout "$THORIUM_REF"
    
RUN corepack enable

WORKDIR /app/thorium-web

# pnpm now blocks build scripts for any command — even an explicit `pnpm
# rebuild <pkg>` below — unless the package is allow-listed via `allowBuilds`
# in pnpm-workspace.yaml (package.json's old `pnpm.onlyBuiltDependencies`
# field is no longer read). thorium-web ships no such list, and there's no
# TTY here for the interactive `pnpm approve-builds`, so write it directly.
RUN printf 'allowBuilds:\n  esbuild: true\n  sharp: true\n  unrs-resolver: true\n' > pnpm-workspace.yaml

RUN --mount=type=cache,id=pnpm_store,target=/pnpm/store pnpm install --frozen-lockfile --ignore-scripts
RUN pnpm rebuild esbuild sharp unrs-resolver

RUN pnpm build

EXPOSE 3000

CMD ["pnpm", "start"]
