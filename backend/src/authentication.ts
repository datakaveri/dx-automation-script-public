import { createPublicKey, verify as verifySignature } from "node:crypto";
import type { NextFunction, Request, Response } from "express";

export interface KeycloakClaims {
  sub: string;
  email?: string;
  preferred_username?: string;
  name?: string;
  exp: number;
  nbf?: number;
  iss: string;
  aud?: string | string[];
  azp?: string;
  realm_access?: { roles?: string[] };
  resource_access?: Record<string, { roles?: string[] }>;
  groups?: string[];
  [claim: string]: unknown;
}

declare global {
  namespace Express {
    interface Request {
      user?: KeycloakClaims;
    }
  }
}

function keycloakSettings() {
  const issuer = process.env.KEYCLOAK_URL && process.env.KEYCLOAK_REALM
    ? `${process.env.KEYCLOAK_URL.replace(/\/$/, "")}/realms/${process.env.KEYCLOAK_REALM}`
    : "";
  return {
    issuer,
    apiAudience: process.env.KEYCLOAK_API_AUDIENCE ?? process.env.KEYCLOAK_CLIENT_ID ?? "",
    frontendClientId: process.env.KEYCLOAK_FRONTEND_CLIENT_ID ?? process.env.KEYCLOAK_CLIENT_ID ?? "",
    jwksUrl: issuer ? `${issuer}/protocol/openid-connect/certs` : "",
  };
}

let cachedKeys = new Map<string, ReturnType<typeof createPublicKey>>();
let keysExpireAt = 0;
let refreshPromise: Promise<void> | null = null;

class JwksUnavailable extends Error {
  constructor(public readonly jwksUrl: string, options?: ErrorOptions) {
    super("Unable to retrieve Keycloak signing keys", options);
  }
}

async function refreshKeys(jwksUrl: string, force = false): Promise<void> {
  if (!force && cachedKeys.size > 0 && Date.now() < keysExpireAt) return;
  if (refreshPromise) return refreshPromise;
  refreshPromise = (async () => {
    try {
      const response = await fetch(jwksUrl, { signal: AbortSignal.timeout(10_000) });
      if (!response.ok) throw new Error(`Keycloak JWKS returned ${response.status}`);
      const body: unknown = await response.json();
      const keys = (body as { keys?: unknown }).keys;
      if (!Array.isArray(keys)) throw new Error("Keycloak JWKS response has no keys array");
      const nextKeys = new Map<string, ReturnType<typeof createPublicKey>>();
      for (const value of keys) {
        if (!value || typeof value !== "object") continue;
        const jwk = value as { kid?: unknown; use?: unknown; kty?: unknown };
        if (typeof jwk.kid !== "string" || jwk.use === "enc" || jwk.kty !== "RSA") continue;
        nextKeys.set(jwk.kid, createPublicKey({ key: value as import("node:crypto").JsonWebKey, format: "jwk" }));
      }
      cachedKeys = nextKeys;
      keysExpireAt = Date.now() + 5 * 60_000;
    } catch (error) {
      throw new JwksUnavailable(jwksUrl, { cause: error });
    }
  })().finally(() => { refreshPromise = null; });
  return refreshPromise;
}

function decodePart<T>(part: string): T {
  return JSON.parse(Buffer.from(part, "base64url").toString("utf8")) as T;
}

async function validateToken(token: string): Promise<KeycloakClaims> {
  const { issuer, apiAudience, frontendClientId, jwksUrl } = keycloakSettings();
  if (!issuer || !apiAudience || !frontendClientId) throw new Error("Keycloak authentication is not configured");
  const parts = token.split(".");
  if (parts.length !== 3) throw new Error("Malformed JWT");
  const header = decodePart<{ alg?: string; kid?: string }>(parts[0]);
  if (header.alg !== "RS256" || !header.kid) throw new Error("Unsupported JWT signing key");

  await refreshKeys(jwksUrl);
  let key = cachedKeys.get(header.kid);
  if (!key) {
    await refreshKeys(jwksUrl, true);
    key = cachedKeys.get(header.kid);
  }
  if (!key || !verifySignature("RSA-SHA256", Buffer.from(`${parts[0]}.${parts[1]}`), key, Buffer.from(parts[2], "base64url"))) {
    throw new Error("Invalid JWT signature");
  }

  const claims = decodePart<KeycloakClaims>(parts[1]);
  const now = Math.floor(Date.now() / 1000);
  if (claims.iss !== issuer || typeof claims.sub !== "string" || !claims.sub ||
      typeof claims.exp !== "number" || claims.exp <= now ||
      (typeof claims.nbf === "number" && claims.nbf > now + 5)) {
    throw new Error("Invalid JWT claims");
  }
  const audiences = typeof claims.aud === "string" ? [claims.aud] : claims.aud;
  if (!Array.isArray(audiences) || !audiences.includes(apiAudience) || claims.azp !== frontendClientId) {
    throw new Error("JWT client or audience is not allowed");
  }
  return claims;
}

export async function requireAuthentication(request: Request, response: Response, next: NextFunction): Promise<void> {
  const authorization = request.header("authorization");
  const match = authorization?.match(/^Bearer\s+([^\s]+)$/i);
  if (!match) {
    response.status(401).json({ error: { code: "UNAUTHORIZED", message: "A valid bearer token is required." } });
    return;
  }
  try {
    request.user = await validateToken(match[1]);
    next();
  } catch (error) {
    const configurationError = error instanceof Error && error.message === "Keycloak authentication is not configured";
    if (configurationError) {
      response.status(503).json({ error: { code: "AUTHENTICATION_UNAVAILABLE", message: "Keycloak authentication is not configured." } });
      return;
    }
    if (error instanceof JwksUnavailable) {
      const cause = error.cause;
      console.error("Unable to retrieve Keycloak signing keys", {
        jwksUrl: error.jwksUrl,
        cause: cause instanceof Error ? cause.message : String(cause ?? "Unknown JWKS error")
      });
      response.status(503).json({ error: { code: "AUTHENTICATION_UNAVAILABLE", message: "Could not verify the access token." } });
      return;
    }
    response.status(401).json({ error: { code: "UNAUTHORIZED", message: "The access token is invalid or expired." } });
  }
}

export function requireRealmRole(role: string) {
  return (request: Request, response: Response, next: NextFunction): void => {
    if (!request.user) {
      response.status(401).json({ error: { code: "UNAUTHORIZED", message: "A valid bearer token is required." } });
      return;
    }
    if (!request.user.realm_access?.roles?.includes(role)) {
      response.status(403).json({ error: { code: "FORBIDDEN", message: "You do not have permission to perform this action." } });
      return;
    }
    next();
  };
}
