# IUDX QA Frontend

React, Vite, Tailwind CSS, and TypeScript frontend for the IUDX API test dashboard.
The backend is maintained in a separate repository.

## Structure

```text
.
├── .github/workflows/deploy.yaml
├── src/                 React application source
├── index.html           Vite entry document
├── vite.config.ts       Vite config and local API proxy
├── package.json         Frontend dependencies and scripts
└── README.md
```

## Prerequisites

Node.js 22 or newer.

## Development

Copy `.env.example` to `.env.local` and set the public Keycloak URL, realm, and
browser client ID. The Keycloak client must enable Standard Flow, use PKCE
(`S256`), and allow the local and deployed frontend URLs as valid redirects.
The browser client must be public; never put a client secret in a `VITE_`
variable. Add the same three `VITE_KEYCLOAK_…` values to the Netlify build
environment for production; frontend values are public and are embedded in the
browser bundle.

```sh
npm ci
npm run dev
```

The Vite development server proxies `/api` requests to `http://localhost:4000`.
Start the separate backend repository locally when API data is needed.

## Build

```sh
npm run build
npm run preview
```

## Deployment

The GitHub Actions workflow builds and deploys the frontend to Netlify. It writes
the `/api` redirect from the `VITE_API_URL_V5` repository secret and keeps the
single-page application fallback redirect.

Backend deployment is intentionally outside this repository.
