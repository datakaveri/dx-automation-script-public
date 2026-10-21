# IUDX QA Automation

Centralized repository for the IUDX automated quality assurance harness. This project contains both the runner backend and the management dashboard.

---

## Architecture Overview

```
.
├── backend/    # Node.js/TypeScript API & Newman execution engine
└── frontend/   # React + Vite + TypeScript web dashboard
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
  - Configures auto-rerun intervals and recipient preferences.

---

---

## Quickstart with Docker Compose (Recommended)

To deploy the complete stack (Frontend, Backend, and PostgreSQL) with a single command:

1. **Copy the unified environment template:**
   ```bash
   cp .env.example .env
   ```
2. **Review and update `.env`** with your Keycloak and SMTP credentials.
3. **Start the containers:**
   ```bash
   docker compose up -d --build
   ```
4. **Access the services:**
   - **Frontend Dashboard:** [http://localhost:8080](http://localhost:8080)
   - **Backend API & Swagger Docs:** [http://localhost:4000/api/docs](http://localhost:4000/api/docs)
   - **Backend Health Check:** [http://localhost:4000/health](http://localhost:4000/health)

To view logs or stop the stack:
```bash
docker compose logs -f
docker compose down
```

---

## Local Development (Without Docker)

### 1. Backend Setup

```bash
cd backend
npm install

# Copy environment template and adjust values
cp .env.example .env

# Run development server
npm run dev
```

The backend server listens on port `4000` by default. Swagger documentation is available at `http://localhost:4000/api/docs`.

### 2. Frontend Setup

```bash
cd frontend
npm install

# Copy environment template and adjust values
cp .env.example .env

# Run development server
npm run dev
```

The dashboard will be available at `http://localhost:5173`.

---

## Environment Configuration

- Never commit real `.env` files into version control.
- Reference `.env.example` at the root for Docker Compose, or inside `backend/` and `frontend/` respectively for standalone development.

