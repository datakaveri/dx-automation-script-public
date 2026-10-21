import type Keycloak from "keycloak-js";
import type { KeycloakTokenParsed } from "keycloak-js";
import type { UserRoles } from "./types";

/**
 * The exact role required for administrative QA actions such as
 * Auto Re-run Settings.
 */
export const QA_ADMIN_ROLE = "qa_admin";

/**
 * Checks if a given role string matches the exact 'qa_admin' role.
 * Explicitly excludes 'cos_admin', 'consumer', generic admin-like names, etc.
 */
export function isMatchingQaAdminRole(role: unknown): boolean {
  if (typeof role !== "string") return false;
  const clean = role.trim().toLowerCase();
  if (clean === QA_ADMIN_ROLE) return true;

  // Handle client-qualified role strings like "frontend-client:qa_admin"
  if (clean.includes(":")) {
    const parts = clean.split(":");
    const rolePart = parts[parts.length - 1]?.trim().toLowerCase();
    if (rolePart === QA_ADMIN_ROLE) return true;
  }

  // Handle slash-qualified role strings like "/qa_admin"
  if (clean.startsWith("/")) {
    const stripped = clean.replace(/^\/+/, "").toLowerCase();
    if (stripped === QA_ADMIN_ROLE) return true;
  }

  return false;
}

/**
 * Role matching helper checking specifically for 'qa_admin'.
 */
export const isMatchingAdminRole = isMatchingQaAdminRole;

/**
 * Robustly extracts role strings from client access entries, supporting both:
 * - Object format: { roles: string[] }
 * - Array format: string[]
 */
export function extractRolesFromAccess(access: unknown): string[] {
  if (!access) return [];
  if (Array.isArray(access)) {
    return access.filter((item): item is string => typeof item === "string");
  }
  if (typeof access === "object") {
    const rolesObj = access as { roles?: unknown };
    if (Array.isArray(rolesObj.roles)) {
      return rolesObj.roles.filter((item): item is string => typeof item === "string");
    }
  }
  return [];
}

/**
 * Checks whether the user has the exact 'qa_admin' role assigned in Keycloak
 * to access administrative features such as Scheduled / Auto Re-run Settings.
 *
 * Checks:
 * 1. Keycloak instance built-in role checks (hasRealmRole / hasResourceRole)
 * 2. Keycloak realm roles (from token realm_access or auth context userRoles.realm)
 * 3. Keycloak client roles (from token resource_access or auth context userRoles.clients)
 * 4. Keycloak direct token roles claim if present
 *
 * Strictly requires the exact 'qa_admin' role claim.
 * Non-qa_admin roles (such as 'cos_admin', 'consumer', 'admin', 'realm-admin')
 * and arbitrary emails/usernames do NOT grant access.
 */
export function hasQaAdminRole(
  tokenParsed?: KeycloakTokenParsed | null,
  userRoles?: UserRoles | null,
  keycloakInstance?: Keycloak | null,
): boolean {
  // 1. Check Keycloak instance methods if authenticated
  if (keycloakInstance?.authenticated) {
    if (
      typeof keycloakInstance.hasRealmRole === "function" &&
      keycloakInstance.hasRealmRole(QA_ADMIN_ROLE)
    ) {
      return true;
    }
    if (typeof keycloakInstance.hasResourceRole === "function") {
      if (keycloakInstance.hasResourceRole(QA_ADMIN_ROLE)) return true;

      const resourceAccess = tokenParsed?.resource_access as Record<string, unknown> | undefined;
      if (resourceAccess && typeof resourceAccess === "object") {
        for (const clientName of Object.keys(resourceAccess)) {
          if (keycloakInstance.hasResourceRole(QA_ADMIN_ROLE, clientName)) {
            return true;
          }
        }
      }
    }
  }

  // 2. Check Keycloak realm roles (userRoles.realm, tokenParsed.realm_access, direct tokenParsed.roles)
  if (Array.isArray(userRoles?.realm) && userRoles.realm.some(isMatchingQaAdminRole)) {
    return true;
  }
  const realmAccessRoles = tokenParsed?.realm_access?.roles;
  if (Array.isArray(realmAccessRoles) && realmAccessRoles.some(isMatchingQaAdminRole)) {
    return true;
  }
  const directRoles = (tokenParsed as { roles?: unknown })?.roles;
  if (Array.isArray(directRoles) && directRoles.some(isMatchingQaAdminRole)) {
    return true;
  }

  // 3. Check Keycloak client roles (userRoles.clients and tokenParsed.resource_access)
  if (userRoles?.clients && typeof userRoles.clients === "object") {
    for (const [clientName, access] of Object.entries(userRoles.clients)) {
      const roles = extractRolesFromAccess(access);
      if (roles.some((role) => isMatchingQaAdminRole(role) || isMatchingQaAdminRole(`${clientName}:${role}`))) {
        return true;
      }
    }
  }

  const resourceAccess = tokenParsed?.resource_access as Record<string, unknown> | undefined;
  if (resourceAccess && typeof resourceAccess === "object") {
    for (const [clientName, access] of Object.entries(resourceAccess)) {
      const roles = extractRolesFromAccess(access);
      if (roles.some((role) => isMatchingQaAdminRole(role) || isMatchingQaAdminRole(`${clientName}:${role}`))) {
        return true;
      }
    }
  }

  return false;
}

/**
 * Checks whether the current user is authorized to access administrative features
 * such as Auto Re-run Settings.
 *
 * Checks specifically for the exact 'qa_admin' role from actual Keycloak role claims.
 * Does NOT treat 'cos_admin', 'consumer', generic admin-like names, or email/username identities as equivalent to 'qa_admin'.
 */
export function isUserAdmin(
  _currentUser?: string | null,
  userRoles?: UserRoles | null,
  tokenParsed?: KeycloakTokenParsed | null,
  keycloakInstance?: Keycloak | null,
  _currentUsername?: string | null,
): boolean {
  return hasQaAdminRole(tokenParsed, userRoles, keycloakInstance);
}
