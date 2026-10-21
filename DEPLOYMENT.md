# Production Deployment Guide: IUDX QA Automation

This guide provides step-by-step instructions for deploying the complete IUDX QA Automation stack (**Frontend Dashboard**, **Newman Runner Backend**, and **PostgreSQL Database**) onto a Linux production server using Docker Compose.

---

## 1. Architecture & Port Mapping

```mermaid
flowchart TD
    User([User Browser]) -->|HTTPS: 443| RevProxy[Host Reverse Proxy / Nginx / Caddy / Cloudflare]
    RevProxy -->|Proxy: 8080 or 80| Frontend[Frontend: Nginx Container]
    Frontend -->|Serve Static Files| Dashboard[SPA React Dashboard]
    Frontend -->|Reverse Proxy /api/ & /health| Backend[Backend: Node 22 API]
    Backend -->|Sequelize: 5432| Postgres[(PostgreSQL 16 Container)]
    Backend -->|Token Validation: 443| Keycloak[Keycloak Identity Provider]
    Backend -->|Reports: 587| SMTP[SMTP Server / Gmail]
```

By default:
- **Frontend Container** listens internally on port `80` and is published to the host on port `FRONTEND_PORT` (default `8080`, or `80`).
- **Frontend Nginx** automatically proxies all `/api/` calls to the backend (`http://backend:4000/api/`), so your client browser never needs direct access to port `4000`, and zero CORS issues occur.
- **Backend Container** listens on internal port `4000` (published to host `4000` for direct API access or Swagger docs).
- **PostgreSQL Container** listens on internal port `5432` (volume stored in `postgres_data`).

---

## 2. Server Prerequisites

Ensure your host server meets the following requirements:
- **OS:** Ubuntu 22.04 / 24.04 LTS, Debian 12, RHEL / Rocky Linux 9, or any modern Linux distribution.
- **Hardware:** Minimum 2 vCPU, 4 GB RAM, 20 GB SSD storage.
- **Software:**
  - Docker Engine $\ge 24.0$
  - Docker Compose plugin $\ge 2.20$
  - Git
- **Firewall / Network:** Inbound TCP ports `80` and `443` allowed.

### Installing Docker & Docker Compose (Ubuntu/Debian)

If Docker is not yet installed:
```bash
sudo apt update
sudo apt install -y curl git ca-certificates gnupg
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker
docker --version && docker compose version
```

---

## 3. Clone Repository & Setup

1. **Clone the repository and checkout the `neuman` branch:**
   ```bash
   git clone -b neuman https://github.com/datakaveri/dx-automation-script-public.git
   cd dx-automation-script-public
   ```

2. **Create the environment file:**
   ```bash
   cp .env.example .env
   ```

---

## 4. Environment Variables (`.env`) Reference

Open `.env` in your editor (`nano .env`) and configure the settings for your environment. **No values are hardcoded**; everything is driven by this file:

### 4.1 Database Configuration
```bash
# Credentials for the PostgreSQL container
POSTGRES_USER=postgres
POSTGRES_PASSWORD=YOUR_STRONG_PASSWORD_HERE
POSTGRES_DB=iudxqa
POSTGRES_PORT=5432

# Backend connection string (points to the postgres service name inside docker network)
DATABASE_URL=postgresql://postgres:YOUR_STRONG_PASSWORD_HERE@postgres:5432/iudxqa
```
*(If you are using an external managed PostgreSQL database like AWS RDS or Azure Flexible Server, simply set `DATABASE_URL` to your external database connection string and comment out or stop the local `postgres` service).*

### 4.2 Backend Configuration
```bash
# Set to 'development' with DB_SYNC=alter on first boot to let Sequelize create database tables.
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

### 4.3 Keycloak Authentication
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

### 4.4 Email (SMTP) & Automated Reports
```bash
# SMTP credentials for sending automated Newman execution reports
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your-email@yourdomain.com
SMTP_PASS= # gitleaks:allow (your email app password)
MAIL_FROM=IUDX QA Bot <your-email@yourdomain.com>

# Comma-separated list of recipient emails
QA_RECIPIENTS=team@yourdomain.com,qa-lead@yourdomain.com
```
*(If SMTP is not needed immediately, you can leave `SMTP_HOST` empty. The backend will start normally with email delivery disabled).*

### 4.5 Ports
```bash
# Host port for the frontend dashboard
# Use 80 if binding directly to the host, or 8080 if running behind host Nginx/Caddy
FRONTEND_PORT=8080
```

---

## 5. Starting the Application

Build the Docker images and start all services in detached mode:

```bash
docker compose up -d --build
```

Verify all containers are up and healthy:
```bash
docker compose ps
```
Expected output:
```text
NAME                                     STATUS          PORTS
dx-automation-script-public-postgres-1   Up (healthy)    0.0.0.0:5432->5432/tcp
dx-automation-script-public-backend-1    Up (healthy)    0.0.0.0:4000->4000/tcp
dx-automation-script-public-frontend-1   Up              0.0.0.0:8080->80/tcp
```

Test health endpoints:
```bash
# Test backend directly:
curl -f http://localhost:4000/health

# Test via Nginx reverse proxy:
curl -f http://localhost:8080/health
```

---

## 6. Setting Up SSL & Domain Name (HTTPS)

In production, run Nginx or Caddy on the host to provide SSL certificates via Let's Encrypt.

### Option A: Host Nginx with Certbot (Recommended)

1. **Install Nginx & Certbot on the host:**
   ```bash
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

3. **Enable site and obtain SSL certificate:**
   ```bash
   sudo ln -s /etc/nginx/sites-available/iudxqa /etc/nginx/sites-enabled/
   sudo nginx -t && sudo systemctl reload nginx
   sudo certbot --nginx -d iudxqa.yourdomain.com
   ```

### Option B: Caddy (Automatic HTTPS)

If you use Caddy, simply add to `/etc/caddy/Caddyfile`:
```caddy
iudxqa.yourdomain.com {
    reverse_proxy 127.0.0.1:8080
}
```
Run `sudo systemctl reload caddy`. Caddy will obtain and renew Let's Encrypt certificates automatically.

---

## 7. Operations & Maintenance

### 7.1 Viewing Logs
```bash
# Follow all container logs:
docker compose logs -f

# Follow backend logs specifically:
docker compose logs -f backend

# Follow frontend Nginx access/error logs:
docker compose logs -f frontend
```

### 7.2 Database Backups & Restores
Data is stored persistently in the named volume `postgres_data`.

- **Backup database:**
  ```bash
  docker compose exec -T postgres pg_dump -U postgres iudxqa > backup_$(date +%F_%T).sql
  ```
- **Restore database:**
  ```bash
  cat backup_file.sql | docker compose exec -T postgres psql -U postgres -d iudxqa
  ```

### 7.3 Pulling Updates & Re-deploying
To update to a new revision without data loss:
```bash
git pull origin neuman
docker compose up -d --build
```
*(Your PostgreSQL database and stored Newman results in Docker volumes are preserved).*

### 7.4 Stopping the Stack
```bash
# Stop containers:
docker compose down

# Stop and remove persistent data volumes (CAUTION: deletes database data):
# docker compose down -v
```

---

## 8. Troubleshooting Checklist

| Issue | Root Cause | Solution |
|---|---|---|
| `401 Unauthorized` on API calls | Keycloak bearer token missing or expired | Ensure user logs into dashboard through Keycloak PKCE flow. |
| `Keycloak JWKS error` | Backend cannot connect to `KEYCLOAK_URL` | Check server outbound HTTPS connectivity to `KEYCLOAK_URL`. |
| Port `8080` already in use | Conflicting host application | Change `FRONTEND_PORT=8081` in `.env` and restart with `docker compose up -d`. |
| Backend crash with `DB_SYNC` error | `NODE_ENV=production` with `DB_SYNC=alter` | For production, set `DB_SYNC=none`. |
| SMTP connection timeout | Firewall blocks port 587 or bad app password | Verify SMTP host/port and use a Gmail App Password instead of your account password. |
