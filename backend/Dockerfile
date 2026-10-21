FROM node:22-slim

WORKDIR /app

# Python is used by collection post-run cleanup scripts.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY package*.json ./
RUN npm ci

# Copy application code and configurations
COPY . .

# Build cleanup dependencies into the image; runtime cleanup does not need
# package-index access or write access to the collection directory.
RUN python3 -m venv /opt/post-run-venvs/control-plane \
    && /opt/post-run-venvs/control-plane/bin/python -m pip install --no-cache-dir -r "collections/Control Plane/requirements.txt" \
    && python3 -c "from pathlib import Path; import hashlib; req=Path('collections/Control Plane/requirements.txt'); marker=Path('/opt/post-run-venvs/control-plane/.requirements.sha256'); marker.write_text(hashlib.sha256(req.read_bytes()).hexdigest())"

# Environment defaults
ENV NODE_ENV=production
ENV DB_SYNC=none
ENV PORT=4000
ENV COLLECTIONS_DIR=/app/collections
ENV RESULTS_FILE=/app/data/results.json
ENV CONTROL_PLANE_PASSWORD_FILE=/app/data/control-plane-password.json
ENV POST_RUN_VENV_ROOT=/opt/post-run-venvs

EXPOSE 4000

CMD ["npx", "tsx", "src/index.ts"]
