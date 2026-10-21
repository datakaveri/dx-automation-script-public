import Keycloak from "keycloak-js";

const url = import.meta.env.VITE_KEYCLOAK_URL;
const realm = import.meta.env.VITE_KEYCLOAK_REALM;
const clientId = import.meta.env.VITE_KEYCLOAK_CLIENT_ID;

export const keycloakConfigured = Boolean(url && realm && clientId);
export const keycloak = keycloakConfigured
  ? new Keycloak({ url, realm, clientId })
  : null;

let initPromise: Promise<boolean> = Promise.resolve(false);
export function initializeKeycloak(): Promise<boolean> {
  if (!keycloak) return initPromise;
  keycloak.onTokenExpired = () => {
    keycloak.updateToken(5).catch(() => {
      void keycloak.logout({ redirectUri: globalThis.location.origin });
    });
  };
  initPromise = keycloak.init({
    onLoad: "check-sso",
    pkceMethod: "S256",
    checkLoginIframe: false,
  });
  return initPromise;
}

export function keycloakReady(): Promise<boolean> {
  return initPromise;
}
