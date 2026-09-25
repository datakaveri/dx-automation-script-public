#!/usr/bin/env python3
"""
Keycloak: user provisioning, role assertions, and end-user tokens.

ControlPlane has no create-user API — users originate in Keycloak — so the
Admin API is a hard dependency for setup. Deletion then comes free with the same
client, which matters because `DELETE /iudx/v2/auth/user/delete` refuses to
remove org admins (AdminHandler.java:417) and the org delete is DB-only.

The admin client is the deployment's own service account
(keycloakAdminClientId / keycloakAdminClientSecret), not a second credential
minted for testing.
"""

import time

import requests

from .client import ApiClient

# Roles the org-create approval assigns, per
# OrganizationLifecycleServiceImpl.java:168-169.
ROLES_AFTER_ORG_APPROVAL = ("org_admin", "provider")


class Keycloak:
    def __init__(self, config, recorder):
        self.config = config["keycloak"]
        self.realm = self.config["realm"]
        self.client = ApiClient(
            self.config["url"],
            recorder,
            timeout=config["control_plane"]["timeout_seconds"],
        )
        self._admin_token = None
        self._admin_expiry = 0.0

    # ---------------------------------------------------------------- tokens

    def admin_token(self):
        """Client-credentials token for the Admin API, refreshed when stale."""
        if self._admin_token and time.monotonic() < self._admin_expiry:
            return self._admin_token

        payload = self._form_post(
            f"/realms/{self.realm}/protocol/openid-connect/token",
            "keycloak admin token",
            {
                "grant_type": "client_credentials",
                "client_id": self.config["admin_client_id"],
                "client_secret": self.config["admin_client_secret"],
            },
        )
        self._admin_token = payload["access_token"]
        # Refresh a little early so a long phase never runs on an expiring token.
        self._admin_expiry = time.monotonic() + max(payload.get("expires_in", 60) - 30, 10)
        return self._admin_token

    def user_token(self, username, password, client_id=None):
        """Password-grant token for one of the test users.

        Call this again after any role change: roles live in the token, so a
        token minted before an approval will not carry the role it granted.

        `client_id` overrides `keycloak.user_client_id` for this one call. The
        token's `azp` claim is the client it was minted with, and services that
        check it accept only their own — the sandbox refuses anything whose azp
        is not its API_KEYCLOAK_CLIENT_ID. So a service on a different client
        needs its token minted with that client, not merely re-signed.
        """
        payload = self._form_post(
            f"/realms/{self.realm}/protocol/openid-connect/token",
            f"token for {username}" + (f" ({client_id})" if client_id else ""),
            {
                "grant_type": "password",
                "client_id": client_id or self.config["user_client_id"],
                **(
                    {"client_secret": self.config["user_client_secret"]}
                    if self.config.get("user_client_secret")
                    else {}
                ),
                "username": username,
                "password": password,
            },
        )
        return payload["access_token"]

    def _form_post(self, path, label, form):
        """Token endpoints take form encoding, not JSON, so they bypass ApiClient.

        Failures are still recorded, so a run that cannot even reach Keycloak
        shows the attempt in its report rather than an empty call list.
        """
        url = f"{self.client.base_url}/{path.lstrip('/')}"
        try:
            response = self.client.session.post(
                url,
                data=form,
                timeout=self.client.timeout,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except requests.RequestException as err:
            self.client.recorder.add(label, "POST", url, 0, False, 0, str(err))
            raise RuntimeError(f"{label} failed: {err}") from err

        ok = response.status_code == 200
        self.client.recorder.add(
            label, "POST", path, response.status_code, ok, 0,
            "" if ok else response.text[:400],
        )
        if not ok:
            raise RuntimeError(f"{label} failed ({response.status_code}): {response.text[:400]}")
        return response.json()

    # ------------------------------------------------------------ admin api

    def _admin(self, method, path, label, **kw):
        return self.client.request(
            method,
            f"/admin/realms/{self.realm}{path}",
            label,
            token=self.admin_token(),
            **kw,
        )

    def create_user(self, username, email, password, first_name="E2E", last_name="Test"):
        """Create an enabled, email-verified user and return its id."""
        self.client.request(
            "POST",
            f"/admin/realms/{self.realm}/users",
            f"create user {username}",
            token=self.admin_token(),
            json_body={
                "username": username,
                "email": email,
                "firstName": first_name,
                "lastName": last_name,
                "enabled": True,
                "emailVerified": True,
                "credentials": [
                    {"type": "password", "value": password, "temporary": False}
                ],
            },
            expect=(201,),
        )
        found = self.find_user(username)
        if not found:
            raise RuntimeError(f"created user {username} but could not read it back")
        return found["id"]

    def find_user(self, username):
        """Exact-username lookup, or None."""
        matches = self._admin(
            "GET", "/users", f"find user {username}",
            params={"username": username, "exact": "true", "max": 2},
        )
        return matches[0] if matches else None

    def find_users_by_prefix(self, prefix):
        """Every user whose username starts with prefix — the teardown sweep."""
        matches = self._admin(
            "GET", "/users", f"find users like {prefix}",
            params={"username": prefix, "max": 500},
        )
        return [u for u in matches if u.get("username", "").startswith(prefix)]

    def delete_user(self, user_id, label="user"):
        self._admin("DELETE", f"/users/{user_id}", f"delete {label}", expect=(204,))

    def realm_roles(self, user_id):
        """Realm role names currently mapped to the user."""
        mapped = self._admin(
            "GET", f"/users/{user_id}/role-mappings/realm", "read realm roles"
        )
        return sorted(role["name"] for role in mapped)

    def attributes(self, user_id):
        user = self._admin("GET", f"/users/{user_id}", "read user attributes")
        return user.get("attributes") or {}

    def assign_realm_role(self, user_id, role_name):
        role = self._admin("GET", f"/roles/{role_name}", f"read role {role_name}")
        self._admin(
            "POST", f"/users/{user_id}/role-mappings/realm",
            f"assign {role_name}",
            json_body=[{"id": role["id"], "name": role["name"]}],
            expect=(204,),
        )

    def set_attributes(self, user_id, attributes):
        """Merge attributes onto a user (values must be lists of strings)."""
        user = self._admin("GET", f"/users/{user_id}", "read user")
        merged = {**(user.get("attributes") or {}), **attributes}
        self._admin(
            "PUT", f"/users/{user_id}", "update user attributes",
            json_body={"attributes": merged}, expect=(204,),
        )

    # ----------------------------------------------------------- assertions

    def await_roles(self, user_id, expected, timeout=30, interval=2):
        """Wait for roles to appear, since approval writes to Keycloak async.

        Returns the roles seen. Raises if they have not appeared in time.
        """
        expected = set(expected)
        deadline = time.monotonic() + timeout
        seen = []
        while time.monotonic() < deadline:
            seen = self.realm_roles(user_id)
            if expected.issubset(set(seen)):
                return seen
            time.sleep(interval)
        missing = ", ".join(sorted(expected - set(seen)))
        raise AssertionError(
            f"Keycloak did not gain role(s) {missing} within {timeout}s; has: {', '.join(seen)}"
        )