FROM node:22-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends     curl git jq fzf ripgrep fd-find     && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

RUN curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh | sh -s -- -b /usr/local/bin

RUN useradd -u 1000 -ms /bin/bash dev && chown -R dev:dev /home/dev
USER dev
WORKDIR /workspaces
