# IUDX QA Automation

Centralized repository for the IUDX automated quality assurance harness. This project contains both the Newman execution engine backend and the web management dashboard.

---

## 1. Architecture Overview

```mermaid
flowchart TD
    User([User Browser]) -->|HTTPS: 443| RevProxy[Host Reverse Proxy / Nginx / Caddy / Cloudflare]
    RevProxy -->|Proxy: 8080 or 80| Frontend[Frontend: Nginx Container]
    Frontend -->|Serve Static Files| Dashboard[SPA React Dashboard]
    Frontend -->|Reverse Proxy /api/ & /health| Backend[Backend: Node 22 API]
    Backend -->|Sequelize: 5432| Postgres[(Existing PostgreSQL Database)]
    Backend -->|Token Validation: 443| Keycloak[Keycloak Identity Provider]
    Backend -->|Reports: 587| SMTP[SMTP Server / Mail Provider]
```

```
.
├── backend/            # Node.js 22 + TypeScript API & Newman execution engine
├── frontend/           # React + Vite + TypeScript web dashboard
├── docker-compose.yml  # Unified Docker Compose orchestration
├── .env.example        # Master environment configuration template
└── README.md           # Documentation & deployment guide
```

- **[backend](backend/)**:
  - Executes Postman/Newman collections (Control Plane, Data Plane, Files Connect, Sandbox Connect).
  - Stores execution logs and historical runs in PostgreSQL (Sequelize).
  - Sends scheduled email reports with Newman HTML run summaries.
  - Secures endpoints via Keycloak bearer tokens.
  - Auto re-runs collections on startup and configurable intervals.

- **[frontend](frontend/)**:
  - React/TypeScript dashboard powered by Vite.
  - Authenticates via Keycloak PKCE flow.
  - Displays collection statuses, latest test outcomes, execution history, and manual trigger controls.
  - Reverse-proxied through internal container Nginx: all browser `/api/` calls seamlessly route to the backend container without CORS issues or needing port 4000 open publicly.

---

## 2. Quickstart with Docker Compose (Recommended)

To deploy the stack (**Frontend Dashboard** and **Backend Newman Runner**) with a single command:

1. **Clone the repository and checkout the `neuman` branch:**
   ```bash
   git clone -b neuman https://github.com/datakaveri/dx-automation-script-public.git
   cd dx-automation-script-public
   ```

2. **Copy the environment template:**
   ```bash
   cp .env.example .env
   ```

3. **Configure your `.env` file:**
   Open `.env` in an editor (`nano .env`) and set `DATABASE_URL` to point to your existing PostgreSQL database, along with your Keycloak and SMTP credentials. See the [Environment Configuration Reference](#3-environment-configuration-reference) below.

4. **Build and start the containers:**
   ```bash
   docker compose up -d --build
   ```

5. **Verify service status:**
   ```bash
   docker compose ps
   ```

6. **Access the services:**
   - **Frontend Dashboard:** [http://localhost:8080](http://localhost:8080)
   - **Backend API & Swagger Docs:** [http://localhost:4000/api/docs](http://localhost:4000/api/docs)
   - **Backend Health Check:** [http://localhost:4000/health](http://localhost:4000/health) (or via frontend proxy: [http://localhost:8080/health](http://localhost:8080/health))

---

## 3. Environment Configuration Reference

The application is completely configured through environment variables. **No credentials or domains are hardcoded**.

### 3.1 Database Configuration (Existing PostgreSQL)
```bash
# Connection string pointing to your existing PostgreSQL database server.
# Format: postgresql://<user>:<password>@<host>:<port>/<database>
# Examples:
#   - If Postgres runs directly on the host machine: postgresql://postgres:password@host.docker.internal:5432/iudxqa
#   - If Postgres runs on an external/managed server: postgresql://postgres:password@db.example.com:5432/iudxqa
DATABASE_URL=postgresql://postgres:YOUR_PASSWORD@host.docker.internal:5432/iudxqa # gitleaks:allow
```

### 3.2 Backend API Configuration
```bash
# Set to 'development' with DB_SYNC=alter on first boot to allow Sequelize to create database tables.
# Once tables exist, change to NODE_ENV=production and DB_SYNC=none.
NODE_ENV=development
DB_SYNC=alter
BACKEND_PORT=4000

# Postman collections directory inside backend container
COLLECTIONS_DIR=collections

# Allowed origins for CORS (comma-separated, include your production domain)
CORS_ORIGINS=https://iudxqa.yourdomain.com,https://iudxqa-backend.yourdomain.com,http://localhost:8080

# Public API URL used in links generated inside email reports
PUBLIC_API_URL=https://iudxqa-backend.yourdomain.com

# URL of the dashboard used in report "Back to Dashboard" buttons
DASHBOARD_URL=https://iudxqa.yourdomain.com

# Automatically trigger an API test run immediately after the server starts
AUTO_RERUN_ON_STARTUP=false
```

### 3.3 Keycloak Authentication
```bash
# Keycloak server endpoint and realm
KEYCLOAK_URL=https://v2.dev.iudx.io/auth
KEYCLOAK_REALM=iudx-v2
KEYCLOAK_CLIENT_ID=frontend-client
KEYCLOAK_FRONTEND_CLIENT_ID=frontend-client
KEYCLOAK_API_AUDIENCE=account

# Build-time parameters embedded into frontend client bundle
VITE_KEYCLOAK_URL=https://v2.dev.iudx.io/auth
VITE_KEYCLOAK_REALM=iudx-v2
VITE_KEYCLOAK_CLIENT_ID=frontend-client

# Leave VITE_API_BASE_URL empty so the dashboard uses relative /api/ proxied by Nginx
VITE_API_BASE_URL=
```

### 3.4 Email (SMTP) & Automated Reports
```bash
# SMTP credentials for sending automated Newman execution reports
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your-email@yourdomain.com
SMTP_PASS= # gitleaks:allow
MAIL_FROM=IUDX QA Bot <your-email@yourdomain.com>

# Comma-separated list of recipient emails
QA_RECIPIENTS=team@yourdomain.com,qa-lead@yourdomain.com
```
*(If SMTP is not needed immediately, leave `SMTP_HOST` empty. The backend will start normally with email delivery disabled).*

### 3.5 Host Port
```bash
# Host port for the frontend dashboard
# Use 80 if binding directly to the host, or 8080 if running behind a host reverse proxy
FRONTEND_PORT=8080
```

> [!WARNING]
> Never commit real `.env` files into version control. Only `.env.example` should be tracked in git.

---

## 4. Production Server Deployment & HTTPS Setup

In production, run a reverse proxy (such as Nginx or Caddy) on the host machine to handle SSL/TLS termination via Let's Encrypt certificates.

### Server Prerequisites
- **OS:** Ubuntu 22.04 / 24.04 LTS, Debian 12, Rocky Linux 9, or modern Linux.
- **Hardware:** Minimum 2 vCPU, 4 GB RAM, 20 GB SSD.
- **Software:** Docker Engine $\ge 24.0$, Docker Compose plugin $\ge 2.20$, Git.
- **Firewall:** Inbound TCP ports `80` and `443` allowed.

### Option A: Host Nginx with Certbot (Let's Encrypt)

1. **Install Nginx and Certbot:**
   ```bash
   sudo apt update
   sudo apt install -y nginx certbot python3-certbot-nginx
   ```

2. **Create `/etc/nginx/sites-available/iudxqa`:**
   ```nginx
   server {
       server_name iudxqa.yourdomain.com;

       location / {
           proxy_pass http://127.0.0.1:8080;
           proxy_http_version 1.1;
           proxy_set_header Host $host;
           proxy_set_header X-Real-IP $remote_addr;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
           proxy_set_header Upgrade $http_upgrade;
           proxy_set_header Connection "upgrade";
       }
   }
   ```

3. **Enable configuration and issue SSL certificate:**
   ```bash
   sudo ln -s /etc/nginx/sites-available/iudxqa /etc/nginx/sites-enabled/
   sudo nginx -t && sudo systemctl reload nginx
   sudo certbot --nginx -d iudxqa.yourdomain.com
   ```

### Option B: Caddy (Automatic HTTPS)

Add to `/etc/caddy/Caddyfile`:
```caddy
iudxqa.yourdomain.com {
    reverse_proxy 127.0.0.1:8080
}
```
Reload Caddy:
```bash
sudo systemctl reload caddy
```

---

## 5. Operations & Maintenance

### 5.1 Viewing Container Logs
```bash
# Follow all container logs:
docker compose logs -f

# Follow backend logs specifically:
docker compose logs -f backend

# Follow frontend Nginx access/error logs:
docker compose logs -f frontend
```

### 5.2 Database Backups & Restores
Since the backend connects to your existing PostgreSQL database instance, manage backups using your standard PostgreSQL tooling:

- **Create a database backup:**
  ```bash
  pg_dump -h <host> -U postgres iudxqa > backup_$(date +%F_%T).sql
  ```
- **Restore a database backup:**
  ```bash
  psql -h <host> -U postgres -d iudxqa < backup_file.sql
  ```

### 5.3 Pulling Updates & Re-deploying
To update the deployment without downtime or data loss:
```bash
git pull origin neuman
docker compose up -d --build
```
*(Your persistent data volume `backend_data` storing runtime Newman logs is preserved).*

### 5.4 Stopping the Stack
```bash
# Stop containers:
docker compose down

# Stop and remove persistent data volumes (CAUTION: deletes database data):
# docker compose down -v
```

---

## 6. Troubleshooting Checklist

| Issue | Root Cause | Solution |
|---|---|---|
| `401 Unauthorized` on API calls | Keycloak bearer token missing or expired | Ensure the user signs in to the dashboard through the Keycloak PKCE flow. |
| `Keycloak JWKS error` | Backend cannot connect to `KEYCLOAK_URL` | Check server outbound HTTPS connectivity and firewall rules to `KEYCLOAK_URL`. |
| Port `8080` already in use | Conflicting host service | Change `FRONTEND_PORT=8081` in `.env` and restart with `docker compose up -d`. |
| Backend crash with `DB_SYNC` error | `NODE_ENV=production` with `DB_SYNC=alter` | For production with an existing schema, set `DB_SYNC=none`. |
| SMTP connection timeout | Firewall blocks port 587 or bad password | Verify SMTP host/port and use a Mail App Password instead of account password. |

---

## 7. Local Development (Without Docker)

### 7.1 Backend Setup
```bash
cd backend
npm install

# Copy environment template and adjust values
cp .env.example .env

# Run development server
npm run dev
```
The backend server listens on port `4000` by default. Swagger documentation is available at `http://localhost:4000/api/docs`.

### 7.2 Frontend Setup
```bash
cd frontend
npm install

# Copy environment template and adjust values
cp .env.example .env

# Run development server
npm run dev
```
The dashboard will be available at `http://localhost:5173`.
