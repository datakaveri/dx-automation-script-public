# org_admin — creation and deletion

Two standalone scripts, one per phase:

```
org_admin/
  creation/  org_admin_creation.py   org_admin_creation_config.json[.example]
  deletion/  org_admin_deletion.py   org_admin_deletion_config.json[.example]
```

Neither imports the other or anything else in this repo. They meet only at the
handoff file the creation script writes and the deletion script reads — and
here that file matters more than it does for the other roles, because it is the
only thing that carries the **organisation id** across.

```bash
pip install -r ../requirements.txt          # psycopg2 too, for the teardown

cd creation && cp org_admin_creation_config.json.example org_admin_creation_config.json
python org_admin_creation.py --dry-run
python org_admin_creation.py

cd ../deletion && cp org_admin_deletion_config.json.example org_admin_deletion_config.json
python org_admin_deletion.py --dry-run
python org_admin_deletion.py --pg-dry-run   # print the SQL, run nothing
python org_admin_deletion.py
```

Values may be written `${VAR}` or `${VAR:-fallback}` and are filled in from the
environment when the config loads. Keep passwords there, not in the file.


## What an organisation admin is

**Not a role you assign — a role you are granted by having an organisation.**
The user asks for an organisation of their own, the COS admin approves the
request, and it is that approval which creates the organisation and writes
**both** `org_admin` and `provider` into Keycloak, along with an
`organisation_id` attribute on the account.

So the creation script needs a **COS admin's** credentials (not an org
admin's), and there is only one journey — no `mode` to choose, unlike
`provider`. It is the same journey as `provider --mode org_create`; the
difference is that this script treats the organisation as the point rather than
a side effect, and records its id for teardown.


## Creation flow

```
  admin API  create the Keycloak account            (ensure_user)
             assign user.realm_roles, mail verification if asked
  user       password grant  → signed in
  user       GET  /iudx/v2/auth/user                (create.touch_control_plane)
  user       POST /iudx/v2/auth/organisations/requests
                  {name, entity_type, org_sector, website_link, address,
                   certificate_path, pancard_path, emp_id, job_title,
                   manager_email, organisation_documents}
  cos        GET  /iudx/v2/auth/organisations/requests?status=pending&page=N
                  → match on name, take the request id
  cos        POST /iudx/v2/auth/organisations/requests/approve
                  {req_id, status: granted}
             ── the approval writes org_admin + provider + organisation_id ──
  admin API  poll the realm roles until create.expect_roles appear
  user       re-sign-in, read the organisation_id attribute from Keycloak
  user       GET  /iudx/v2/auth/organisations/{org_id}/users   (confirm_membership)
             write the handoff file
```

Two details that are easy to miss:

- **The token is re-minted after the approval.** The organisation attribute is
  written by the approval, and the token from before it does not carry the
  organisation — reading membership with the stale token would look wrong.
- **The organisation id comes from Keycloak, not from the approve response.**
  If the attribute is missing the run fails with
  `<user> has no organisation_id attribute after approval`, because without it
  the teardown has nothing to clean.


## Creation config

| Block | What it is for |
|---|---|
| `control_plane` | `base_url`, `timeout_seconds`, `verify_tls` |
| `keycloak` | `url`, `realm`; `admin_client_id`/`admin_client_secret` mint the **admin** token; `user_client_id`/`user_client_secret` are the public client used for **password grants** |
| `user` | The account being made |
| `cos_admin` | The approver: a ready-made `token`, or `username`+`password` exchanged for one |
| `organisation` | The organisation being asked for |
| `approval` | Whether and how the COS admin approves |
| `create` | Sign-in, verification mail, role assertion, membership check |
| `endpoints` | Every API path, so a renamed route needs no code change |
| `output` | The handoff file |
| `logging` | What is printed |

### `user`

| Key | Effect |
|---|---|
| `username` | Set it to name one account outright. **Blank → generated.** |
| `email` | Blank → the username if it has an `@`, else `username@email_domain`. Username and email deliberately end up the same string: a realm with `registrationEmailAsUsername` overwrites the username with the email on create, so a distinct username would be silently discarded and the later sign-in would fail. |
| `password` | **Required.** Used for the password grant, printed in the summary, and written to the handoff file if `output.include_password`. |
| `first_name`, `last_name` | Keycloak `firstName` / `lastName`. |
| `prefix`, `email_domain`, `username_template`, `timestamp_format`, `random_hex_bytes` | The generator: `{prefix}`, `{timestamp}`, `{random}`, `{domain}`. Naming a run's accounts by prefix is what makes them findable later. |
| `enabled` | Keycloak `enabled`. |
| `email_verified` | `true` = born verified. `false` to exercise the real verification path. |
| `temporary_password` | Forces a password change at first browser login — which then **breaks the password grant** this script depends on. |
| `required_actions` | e.g. `["VERIFY_EMAIL"]`. An account owing a required action **cannot use the password grant**; the run fails at its own sign-in. |
| `attributes` | Extra Keycloak attributes; scalars are wrapped in the list form Keycloak stores. |
| `realm_roles` | Extra realm roles assigned right after creation, on top of the two the approval grants. |
| `reuse_existing` | `false` (default) → hard error if the username or email is taken. `true` → adopt that account. |

### `organisation`

| Key | Effect |
|---|---|
| `name` | The organisation to ask for. **Blank → `name_prefix` + a `MMDDHHMMSS` stamp**, so repeat runs do not collide. `--org-name` overrides both. |
| `name_prefix` | The stem of a generated name. |
| `name_max_length` | The generated *or configured* name is truncated to this. Watch it: the pending-request lookup matches on the **truncated** name, so a hand-written name longer than this still works, but what you see in the log is the trimmed form. `0` disables trimming. |
| `payload` | The org-create body, sent verbatim with `name` added: `entity_type`, `org_sector`, `website_link`, `address`, `certificate_path`, `pancard_path`, `emp_id`, `job_title`, `organisation_documents`. |
| `payload.manager_email` | **Blank → generated** as `<local>-manager@<domain>` from the account's email, because the platform rejects a request carrying a manager email another request already used. Set it only if you need a specific one, and change it between runs. |

### `approval`

| Key | Effect |
|---|---|
| `enabled` | `false` files the request and **stops there**. The account exists, the request is pending, and it is **not an org admin** — the handoff file then has `org_id: null` and no roles. Clear `create.expect_roles` if you do this, or the assertion fails. |
| `status` | The body of the approve call; normally `granted`. |
| `list_page_size`, `list_max_pages` | How far the pending-request listing is paged while hunting for the request by name. Raise them on a deployment with a long backlog. |

### `create`

| Key | Effect |
|---|---|
| `fetch_token` | Sign in as the new account. The org request is submitted **as the user**, so this journey cannot run without it — `false` is refused up front rather than failing halfway. |
| `touch_control_plane` | `GET /auth/user` right after sign-in, to prove the token works against the platform. |
| `expect_roles` | Asserted at the end; the run **fails** if they do not appear. Default `["org_admin", "provider"]` — both, because the approval grants both. Empty list just logs what is there. |
| `role_timeout_seconds`, `role_poll_seconds` | The polling window for that assertion. Approvals write roles asynchronously, so this is a wait, not a single check. |
| `confirm_membership` | Read `/organisations/{org_id}/users` afterwards and log the member count. A cheap proof the organisation really exists and the account is in it. |
| `settle_seconds` | Final pause before the handoff file is written. |
| `verify_email` | `none` (the Admin API mails nothing on its own), `send` (`send-verify-email`), or `actions` (`execute-actions-email` with `email_actions`). Both mail routes need SMTP on the realm and mail whatever address the account carries. |
| `email_actions` | Actions for the `actions` route, e.g. `VERIFY_EMAIL`, `UPDATE_PASSWORD`. |
| `email_link_client_id`, `email_link_redirect_uri`, `email_link_lifespan_seconds` | Query params on the mailed link; each is sent only when non-empty / non-zero. |

`logging.mask_secrets` is **off by default** — bodies print verbatim, passwords
and tokens included. Turn it on before sharing a log.


## The handoff file

```json
{"kind": "org_admin", "username": "...", "email": "...", "user_id": "...",
 "org_name": "adm-org-0909091434", "org_request_id": "...", "org_id": "...",
 "roles": ["org_admin", "provider"], "created_at": "...", "password": "..."}
```

`org_id` is the load-bearing field: nothing else tells the deletion script which
organisation to clear. **Add this file to `.gitignore`** — it holds the password.


## Why teardown is not symmetric

Both platform routes are closed:

- **The account.** ControlPlane's self-delete refuses org admins outright, so
  the account goes through the **Keycloak Admin API**. That is why
  `delete.mode` defaults to `keycloak` here, where the other roles default to
  `auto`.
- **The organisation.** `DELETE /organisations/{id}` is a bare delete with no
  cascade. It hits a foreign key while any `organization_users` row references
  the organisation — including the admin's own row, which the platform will not
  remove. So it fails in practice.

What is left is the database. That is what `delete.organisation_mode:
"postgres"` and the `postgres` block are for, and why this is the only one of
the three deletion scripts that talks to Postgres at all.


## Deletion flow

```
  resolve_target        --username > target.username/user_id > input_file
  ensure_username       look up the name if only a user_id was given
  pause_seconds
  [organisation_mode "api" → try DELETE /organisations/{id} as the admin;
   on success the org is considered gone and the SQL below is skipped]
  mode auto|api  → self-delete (expected to be refused for an org admin)
  mode keycloak, or auto's fallback → DELETE /admin/realms/{realm}/users/{id}
  verify_gone           poll Keycloak until the account is absent
  database_cleanup      sweep postgres.user_tables  on user_id
  organisation_mode
      postgres  →       sweep postgres.org_tables   on org_id
      none      →       log that the organisation is being left
      api       →       warn that it is still there
  consume_input_file    remove the handoff record
```

Note the ordering: the Keycloak account goes **first**, then its rows. The
account being gone from Keycloak does not remove a single database row.


## Deletion config

### `target`

| Key | Effect |
|---|---|
| `input_file` | The creation record, default `../creation/org_admin_created.json`. Supplies username, user_id, password **and org_id**. |
| `require_input_file` | `true` → a missing file is a hard error instead of a shrug. |
| `remove_input_file` | Delete the record after a successful teardown — **only if it names the account just deleted**. |
| `username`, `user_id` | Explicit overrides; these win over the file. |
| `password` | Needed only by the self-delete and the `api` organisation delete, both of which sign in as the account. |
| `org_id` | The organisation to clear. Usually comes from the file; set it by hand when tearing down something the script did not create. |

If an explicit username or user_id names a **different** account than the file
does, **the file is dropped whole** rather than merged field by field —
otherwise the teardown would inherit somebody else's id, password and
organisation id. You get a warning when that happens.

### `delete`

| Key | Effect |
|---|---|
| `mode` | `keycloak` **(default here)** — Admin API only. `auto` — try the platform self-delete first, then finish through Keycloak (the self-delete leaves the account behind even when it succeeds, and refuses org admins outright anyway). `api` — self-delete only, and fail when the platform refuses, which for an org admin it will. |
| `organisation_mode` | `postgres` (default) — clear the organisation with SQL. `api` — try `DELETE /organisations/{id}` first, and warn if it fails. `none` — leave the organisation standing. |
| `database_cleanup` | Sweep the **user** rows (`postgres.user_tables`). Independent of `organisation_mode`, so you can clear the account's rows while keeping the organisation. |
| `pause_seconds` | Wait before deleting. |
| `verify` | Poll Keycloak until the account is gone, and **fail the run** if it is still there. |
| `verify_timeout_seconds`, `verify_poll_seconds` | That polling window. |

Neither `organisation_mode: "postgres"` nor `database_cleanup` does anything
while `postgres.enabled` is `false`. Both then just log that the rows are being
left in place.

### `postgres`

| Key | Effect |
|---|---|
| `enabled` | **The master switch, `false` by default.** Nothing touches the database until this is true. |
| `dry_run` | Print each `DELETE` and its parameter instead of running it. `--pg-dry-run` sets it for one run. |
| `host`, `port`, `database`, `user`, `password`, `sslmode`, `connect_timeout_seconds` | The connection. |
| `schema` | The schema the tables live in — `aaa` on this platform, not `public`. |
| `user_tables` | `{table, column}` pairs swept on the **user id**. |
| `org_tables` | `{table, column}` pairs swept on the **organisation id**. |

How the sweep behaves:

- **Ordering is yours to keep.** The statements run in list order, and
  `organizations`/`id` is last in `org_tables` for exactly that reason — the
  referencing rows have to go before the row they reference. If you add a
  table, put it before `organizations`.
- **Missing tables and columns are skipped, not fatal.** `information_schema`
  is read first, and anything the deployment does not have is logged and passed
  over. Deployments drift; one statement naming an absent column would
  otherwise abort the rest of the cleanup with it.
- **Every statement is keyed on one id**, passed as a bound parameter — the
  account's or the organisation's. There is no unqualified delete anywhere in
  this script.

Shipped `user_tables`: `organization_users.user_id`,
`organization_join_requests.user_id`, `provider_requests.user_id`,
`organization_create_requests.requested_by`.
Shipped `org_tables`: `provider_requests.organization_id`,
`organization_join_requests.organization_id`,
`organization_users.organization_id`, `organizations.id`.


## Environment

```bash
export KC_ADMIN_SECRET=...   # keycloak.admin_client_secret
export COS_ADMIN_PASS=...    # cos_admin.password, creation only
export PG_PASS=...           # postgres.password, deletion only
```


## When it fails

| Message | Meaning |
|---|---|
| `user ... already exists ...; set user.reuse_existing` | The name is taken. Generate a fresh one, or adopt it. |
| `pending org request <name> is not visible to the COS admin` | Wrong COS admin, the name was truncated by `name_max_length`, or the backlog is deeper than `list_max_pages`. |
| `<user> has no organisation_id attribute after approval` | The approval did not write the attribute — check that it really was granted, and give the platform a moment with `create.settle_seconds`. |
| `Keycloak did not gain role(s) ... within Ns` | The approval landed but the roles have not propagated. Raise `create.role_timeout_seconds`. |
| `Self-delete did not work (...); using the Keycloak Admin API` | **Expected.** The platform refuses to self-delete an org admin. |
| `Could not delete organisation ... through the API` | Also expected — the foreign key from `organization_users`. Use `organisation_mode: "postgres"`. |
| `Skipping <table>.<column> — not on this deployment` | Deployment drift. Fine, unless you expected that table to hold rows. |
| `postgres.enabled is false — leaving the ... rows in place` | The master switch is off; the account is gone from Keycloak but its rows and organisation remain. |
| `the postgres cleanup needs psycopg2` | `pip install -r ../../requirements.txt`. |
