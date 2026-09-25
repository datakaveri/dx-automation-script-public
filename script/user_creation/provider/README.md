# provider — creation and deletion

Two standalone scripts, one per phase:

```
provider/
  creation/  provider_creation.py   provider_creation_config.json[.example]
  deletion/  provider_deletion.py   provider_deletion_config.json[.example]
```

Neither imports the other or anything else in this repo. They meet only at the
handoff file the creation script writes and the deletion script reads.

```bash
pip install -r ../requirements.txt

cd creation && cp provider_creation_config.json.example provider_creation_config.json
python provider_creation.py --dry-run      # plan only, no calls
python provider_creation.py                # do it

cd ../deletion && cp provider_deletion_config.json.example provider_deletion_config.json
python provider_deletion.py --dry-run
python provider_deletion.py
```

Each script defaults to the config sitting beside it, so the path is optional.
Any value may be written `${VAR}` or `${VAR:-fallback}` and is filled in from
the environment when the config loads — keep passwords there, not in the file.


## Why `provider.mode` exists

The platform never hands out the `provider` role directly. A user earns it by
**belonging to an organisation and being elevated by that organisation's admin**,
or by **having an organisation of their own approved**. Those are genuinely
different journeys through the API, with different actors and different
leftovers, so the script does not guess: `provider.mode` names the one to drive.

| mode | What it is | Who must approve | Ends up as | Deletes cleanly |
|---|---|---|---|---|
| `join_and_request` *(default)* | The full platform path — join the org, then ask for the role | org admin, twice | plain provider in an existing org | yes |
| `join_and_grant` | Join the org, then the admin grants the role outright | org admin, once | plain provider in an existing org | yes |
| `org_create` | The user asks for an organisation of their own | COS admin | provider **and org_admin** of a new org | **no** — see below |
| `keycloak_role` | Assign the realm role through the Keycloak Admin API | nobody | a token with the role, nothing more | yes |

`--mode` on the command line overrides the config, so one config can drive all
four.

### `join_and_request` — the full path, and what you want by default

This is what a real user does through the UI. Four API calls across two actors:

```
              org admin token ──► resolve_org_id            (organisation.id, or look up by name)

  user   POST /organisations/{org}/join_requests            {job_title, emp_id, official_email}
  admin  GET  /organisations/{org}/join_requests?status=pending
                                                            → find the row, take its id
  admin  PUT  /organisations/{org}/join_requests/{req}      {status: granted}
         ── the user is now a member; refresh the user token ──
  user   POST /organization/user/provider_role/requests     {}
         GET  /organization/user/provider_requests          → find the pending row's id
                (or the admin's list, per provider.role_request.lookup)
  admin  PUT  /organization/user/provider_role/requests/{req}  {status: granted}
```

Needs: an existing organisation, and that organisation's admin credentials.

The token refresh after the join is not optional housekeeping — membership is
written into Keycloak, and the token minted before the join does not carry it,
so the role request would be made by an account the platform still sees as
unaffiliated. `provider.refresh_token_after_join` turns it off only if you are
deliberately testing that.

Both approvals can be withheld: `provider.join.approve: false` stops after the
join request is filed, `provider.role_request.approve: false` stops after the
role request is filed. Either leaves a pending request for a human to act on —
useful for testing the approval UI, but the run then produces an account
*without* the provider role, so clear `create.expect_roles` or it will fail the
assertion at the end.

### `join_and_grant` — same join, no role request

Identical up to the approved join, then one call instead of two:

```
  admin  POST /organization/user/provider   {user_id, organization_id, status: granted}
```

Use it when you want a provider quickly and do not care about exercising the
request/approve path. There is no request row to find, so it cannot fail on the
listing lookups — which makes it the more robust of the two join modes.

### `org_create` — the user brings their own organisation

```
  user   POST /organisations/requests        {name, entity_type, org_sector, …, manager_email}
  cos    GET  /organisations/requests?status=pending   → find by name
  cos    POST /organisations/requests/approve          {req_id, status: granted}
         ── re-sign-in; read organisation_id from the Keycloak attributes ──
```

Needs COS admin credentials, not an org admin's, and ignores the
`organisation` block entirely — the organisation is created by this flow, not
joined.

**The approval grants `provider` and `org_admin` together.** Two consequences:

- Set `create.expect_roles` to `["provider", "org_admin"]`, or the assertion
  only half-describes what you made.
- **The account cannot self-delete.** The platform refuses to self-delete org
  admins, so `provider_deletion.py` falls back to the Keycloak Admin API, which
  removes the account and leaves the organisation and its database rows behind.
  Use the `org_admin` deletion script for that.

The name is generated per run (`name_prefix` + a timestamp) unless you set one,
and `manager_email` is generated too when blank, because the platform rejects a
request carrying a manager email another request already used.

### `keycloak_role` — not really a provider

Assigns the `provider` realm role straight through the Admin API. No
organisation, no membership, no database rows. The account will carry the role
in its token and the platform will not consider it a provider for anything that
reads its own tables. Useful for token and role-mapping checks; useless as a
fixture for provider workflows.


## The `organisation` block

Read only by the join modes.

```json
"organisation": { "id": "", "name": "Org2 for automation", "lookup_by_name": true }
```

- **`id` set** → used verbatim. It is *not* validated, and `name` is *not* read.
  If the two disagree, nothing warns you.
- **`id` blank, `lookup_by_name` true** → the organisation list is fetched with
  the **org admin's** token and matched on the exact name.
- **`id` blank and no usable name** → config error before any call is made.

Pin the id *or* use the name; carrying both invites them to drift apart.

A bad id does not fail at resolve time — it fails at the first call that uses
it, by which point the Keycloak account already exists and no handoff file has
been written. Clean the orphan up with
`python provider_deletion.py --username <name from the log>`.

The id and the `org_admin` credentials must name the same organisation. If they
do not, the join request is filed and then never found in the admin's pending
list: `no pending join request for <user_id> is visible to the org admin`.


## Creation config

| Block | What it is for |
|---|---|
| `control_plane` | `base_url`, `timeout_seconds`, `verify_tls` |
| `keycloak` | `url`, `realm`; `admin_client_id`/`admin_client_secret` mint the **admin** token (create, read roles, delete); `user_client_id`/`user_client_secret` are the public client used for **password grants** |
| `user` | The account being made — see below |
| `organisation` | The org to join, join modes only |
| `org_admin`, `cos_admin` | The approvers: a ready-made `token`, or `username`+`password` exchanged for one |
| `provider` | `mode` and its per-journey knobs |
| `create` | Sign-in, verification mail, and the role assertion |
| `endpoints` | Every API path, so a renamed route needs no code change |
| `output` | The handoff file |
| `logging` | What is printed |

**`user`.** Leave `username` blank and it is generated from `prefix` +
`username_template` (`{prefix}`, `{timestamp}`, `{random}`, `{domain}`), which
is what makes a run's accounts recognisable and sweepable later. Username and
email deliberately end up the same string: a realm with
`registrationEmailAsUsername` overwrites the username with the email on create,
so a distinct username would be silently discarded and the later sign-in would
fail. `reuse_existing` adopts an account that is already there instead of
erroring. `required_actions` and `temporary_password` both make the account owe
Keycloak something, and **an account that owes a required action cannot use the
password grant** — the run then fails at its own sign-in.

**`create`.** `fetch_token` signs in as the new account; every mode but
`keycloak_role` acts as the user, so turning it off is refused up front for
those. `touch_control_plane` proves the token works with `GET /auth/user`.
`expect_roles` is asserted with a poll (`role_timeout_seconds`,
`role_poll_seconds`) because approvals write roles asynchronously — an empty
list just logs what the account has. `verify_email` is `none` (the Admin API
mails nothing on its own), `send`, or `actions` with `email_actions`; both mail
routes need SMTP on the realm and mail whatever address the account carries.

**`logging.mask_secrets` is off by default** — request and response bodies print
verbatim, passwords, client secrets and access tokens included. Turn it on
before sharing a log.


## The handoff file

On success the creation script writes `output.file` (default
`creation/provider_created.json`):

```json
{"kind": "provider", "username": "...", "email": "...", "user_id": "...",
 "mode": "join_and_request", "org_id": "...", "org_name": null,
 "join_request_id": "...", "provider_request_id": "...",
 "roles": ["provider"], "created_at": "...", "password": "..."}
```

The password is there because the self-delete route signs in as the account;
`output.include_password: false` omits it, and the deletion config then needs
`target.password`. **Add this file to `.gitignore`.**


## Deletion config

`target` decides who: `--username`, then `target.username`/`target.user_id`,
then `target.input_file` (default `../creation/provider_created.json`), which
also supplies the password and the org id. If an explicit username or user_id
names a *different* account than the file does, **the file is dropped whole**
rather than merged — otherwise a teardown would inherit somebody else's id and
password. `remove_input_file` clears the record afterwards, but only when it
names the account that was just deleted.

`delete.mode`:

| mode | Route |
|---|---|
| `auto` *(default)* | ControlPlane self-delete, then the Keycloak Admin API for the account itself; also falls back to the Admin API if the platform refuses |
| `api` | Self-delete only; fail if refused |
| `keycloak` | Admin API only; platform rows are left behind |

The self-delete (`DELETE /iudx/v2/auth/user/delete`, called **as the account**)
unwinds the platform side — the provider role, the organisation membership, the
join requests and the compute requests — and answers *"User deleted
successfully from Keycloak and DB"*.

**Do not believe the Keycloak half of that message.** On this deployment the
account survives it: roles stripped back to `default-roles-*`, organisation
attributes cleared, but still present and still enabled. `auto` therefore
checks afterwards and removes the account through the Admin API. Under `api`
the account stays, and `delete.verify` will report it as still present — which
is accurate, not a bug.

The other reason `auto` falls back is an `org_create` provider, which is an org
admin and is refused outright.

`delete.remove_from_organisation` asks the org admin to drop the member first
(needs the `org_admin` block and an `org_id`). Off by default, because
self-delete already unwinds membership; it is there for the Keycloak route. A
failure is warned, not fatal.

`delete.verify` polls Keycloak until the account is gone and **fails the run**
if it is still there.


## Environment

```bash
export KC_ADMIN_SECRET=...     # keycloak.admin_client_secret
export ORG_ADMIN_PASS=...      # join modes, and remove_from_organisation
export COS_ADMIN_PASS=...      # org_create only
```


## When it fails

| Message | Meaning |
|---|---|
| `user ... already exists ...; set user.reuse_existing` | The name is taken. Generate a fresh one, or adopt it. |
| `no organisation named X is visible to this account` | Name lookup found nothing in the org admin's listing — wrong name, or wrong admin. |
| `no pending join request for <id> is visible to the org admin` | The `org_admin` credentials do not administer `organisation.id`, or the request was already acted on. |
| `the user has no pending provider role request` | The role request did not land — usually a token that predates the join. Check `refresh_token_after_join`. |
| `pending org request <name> is not visible to the COS admin` | `org_create`: wrong COS admin, or the name was truncated by `name_max_length`. |
| `Keycloak did not gain role(s) ... within Ns` | The approval succeeded but the role has not propagated. Raise `create.role_timeout_seconds`. |
| `Self-delete did not work (...); using the Keycloak Admin API` | Expected for an `org_create` provider. The organisation is left behind. |
| A 4xx on the first `/organisations/{org}/...` call | Bad `organisation.id`. The Keycloak account already exists — delete it by `--username`. |
