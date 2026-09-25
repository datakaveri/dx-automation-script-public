# User creation and deletion scripts

Three roles, each with a `creation/` and a `deletion/` script, each script with
its own config:

```
user_creation/
├── consumer/
│   ├── creation/   consumer_creation.py   + consumer_creation_config.json
│   └── deletion/   consumer_deletion.py   + consumer_deletion_config.json
├── provider/
│   ├── creation/   provider_creation.py   + provider_creation_config.json
│   └── deletion/   provider_deletion.py   + provider_deletion_config.json
└── org_admin/
    ├── creation/   org_admin_creation.py  + org_admin_creation_config.json
    └── deletion/   org_admin_deletion.py  + org_admin_deletion_config.json
```

Every script is standalone: it imports nothing from this repository and reads
one JSON config. Copy the `.json.example` beside it, fill in the values, run it.

```bash
python -m pip install -r requirements.txt

cd consumer/creation
cp consumer_creation_config.json.example consumer_creation_config.json
python consumer_creation.py                      # or: … config.json

cd ../deletion
cp consumer_deletion_config.json.example consumer_deletion_config.json
python consumer_deletion.py
```

Every script takes `--dry-run`, which prints what it would do and makes no
calls. Start there.

## How creation hands over to deletion

A creation script writes a small JSON record — username, Keycloak id,
organisation id, roles — to the path in `output.file`, which defaults to its own
folder, since that script is what produced it. The deletion script reads it
across through `target.input_file` (`../creation/<role>_created.json`) and
removes it once the account is gone.

So the normal pairing needs nothing typed twice. To delete an account this
handoff does not describe, pass `--username`, or set `target.username` or
`target.user_id` in the deletion config — an id on its own is enough, since the
username is then looked up from Keycloak.

Naming an account that the handoff file does not describe makes the script
**ignore that file entirely** rather than merging it: the id and password in it
belong to a different account, and using the id would delete the wrong one. For
the same reason the file is left in place afterwards instead of being cleaned
up, because the account it names is still there.

The record carries the account password by default (`output.include_password`),
because ControlPlane's self-delete endpoint signs in as the account. These files
are gitignored; turn the flag off and set `target.password` in the deletion
config if you would rather it were not written down.

## What each role actually is

**Consumer** — a plain account. Created in Keycloak, then introduced to
ControlPlane so the platform-side record exists. Deletion prefers
`DELETE /iudx/v2/auth/user/delete`, which cascades into Keycloak and the
database, and falls back to the Keycloak Admin API.

**Provider** — the platform never hands out the `provider` role directly, so
`provider.mode` picks how the account earns it:

| mode | what happens | needs |
|---|---|---|
| `join_and_request` (default) | join a configured organisation → org admin approves → user requests the provider role → org admin approves | an organisation id and its admin's credentials |
| `join_and_grant` | join a configured organisation → org admin approves → org admin grants the role outright | the same |
| `org_create` | the user asks for an organisation of their own → COS admin approves | a COS admin's credentials |
| `keycloak_role` | assign the realm role through the Keycloak Admin API | nothing beyond Keycloak |

`org_create` grants `org_admin` alongside `provider`, so the account is an
organisation admin too — deletion then behaves like the org_admin case below.
`keycloak_role` creates no organisation membership and no database rows, so the
account is not a working provider; it is there for token and role checks.

**Org admin** — made, not assigned: the user asks for an organisation and the
COS admin approves, and that approval creates the organisation and writes both
`org_admin` and `provider` into Keycloak.

## The compute role

`compute` is not a fourth kind of account — it is an extra role any of the three
can carry, so it lives as a `compute` block in each creation config rather than
as its own script. Turn it on and the creation script appends two calls to
whichever journey it was already running:

```
  user  POST /iudx/v2/auth/compute/requests        {"additionalInfo": {...}}
  cos   GET  /iudx/v2/auth/user/compute/requests   → the pending row's id
        (or GET /iudx/v2/auth/compute/requests, per compute.lookup)
  cos   PUT  /iudx/v2/auth/compute/requests/{id}   {"status": "granted"}
```

The approver is the **COS admin**, for all three roles — never the org admin —
so `cos_admin` must be filled in even in the consumer config, which needs it for
nothing else.

```json
"compute": {
  "enabled": false,
  "additional_info": {},
  "kyc": {"set_verified": true, "attribute": "kyc_verified",
          "value": "true", "refresh_token": true},
  "approve": true, "approve_status": "granted",
  "lookup": "self", "list_page_size": 100, "list_max_pages": 20,
  "settle_seconds": 2, "expect_role": "compute"
}
```

- **`enabled`** is the switch. Off by default, so existing configs behave exactly
  as before.
- **`additional_info`** is sent as `{"additionalInfo": {...}}` and omitted when
  empty — the body is optional.
- **`kyc`** sets the `kyc_verified` attribute on the Keycloak account before
  asking. The compute request is behind the platform's KYC gate; that gate is
  off wherever `kycRequired` is false (dev is one, which is why join, org-create
  and provider requests all succeed on unverified accounts), but a deployment
  with it on refuses the request. The attributes the account already has are
  read and merged, because Keycloak replaces the whole map on write, and the
  user token is re-minted afterwards since the attribute is a token claim.
- **`approve: false`** files the request and stops, leaving it pending for a
  human — the account then does *not* have the compute role.
- **`lookup`** is `self` (read the account's own requests) or `cos_admin` (page
  the COS admin's pending list).
- **`expect_role`** is appended to `create.expect_roles` when the grant goes
  through, so the role is asserted like any other. An empty `expect_roles` still
  means "just log what is there".

**One request per account, ever.** `compute_role.user_id` is `UNIQUE`, and the
API re-activates a rejected row to `pending` rather than inserting a second one.
A half-failed run burns the account it created, exactly like the join request.

**Teardown.** The granted role goes with the Keycloak account. The
`compute_role` row is keyed on `user_id` and is listed in the org_admin deletion
script's `postgres.user_tables`, so a Postgres-enabled teardown clears it. The
consumer and provider deletion scripts have no database leg, so there the row
rides on ControlPlane's own user delete — which, given that endpoint's habit of
overstating what it removed, is worth confirming with a `SELECT` the first time.

## Why org_admin teardown is different

ControlPlane refuses to self-delete an org admin, so the account goes through
the Keycloak Admin API — `delete.mode` defaults to `keycloak` there for that
reason.

The organisation cannot go through the API either. `DELETE /organisations/{id}`
is a bare delete with no cascade and hits a foreign key while any
`organization_users` row references the organisation, including the admin's own
row, which the platform will not remove. Clearing it means going to the
database, which is what `delete.organisation_mode: "postgres"` and the
`postgres` block do.

That block is off until `postgres.enabled` is true. It deletes only rows keyed
on this account's id and this organisation's id, it reads `information_schema`
first and skips tables and columns the deployment does not have, and
`--pg-dry-run` prints the statements instead of running them. **Point it at a
test deployment.**

## Configuration

Every URL, credential, endpoint path, payload field, timeout and step toggle is
a config key; the `.json.example` files list all of them, including the defaults,
so anything can be pasted over. A few worth knowing:

- **`${VAR}` and `${VAR:-fallback}`** are expanded from the environment anywhere
  in a config, so credentials need not sit in the file.
- **`endpoints`** holds every API path the script calls. Change one here rather
  than in the code when a deployment differs.
- **Naming.** `user.prefix` names the accounts a run creates, and
  `user.username_template` decides their shape — placeholders `{prefix}`,
  `{timestamp}`, `{random}`, `{domain}`, with `timestamp_format` (strftime) and
  `random_hex_bytes` alongside it:

  | config | produces |
  |---|---|
  | `prefix: "e2e-dev-consumer"` (default template) | `e2e-dev-consumer-20260908080559-9b6f@example.invalid` |
  | `prefix: "qa-smoke"`, `username_template: "{prefix}.{random}@{domain}"`, `random_hex_bytes: 4` | `qa-smoke.75f95a34@example.invalid` |
  | `prefix: "qa"`, `username_template: "{prefix}_{timestamp}@{domain}"`, `timestamp_format: "%d%b%Y-%H%M"` | `qa_08Sep2026-0806@example.invalid` |

  Set `user.username` to a literal string to name one account outright; that
  wins over the template, and `--username` wins over both. Pick a prefix a
  teardown can find later — the dev configs here use the main harness's own
  `e2e-dev` prefix so its sweep reaps anything an interrupted run leaves.

  Username and email end up the same string. Realms with
  `registrationEmailAsUsername` set overwrite the username with the email on
  create, and a distinct username would be silently discarded.
- **`user.reuse_existing`** adopts an account that is already there instead of
  failing.
- **`create.expect_roles`** is asserted with a poll, since approvals write to
  Keycloak asynchronously. Leave it empty to only log what the account has.
- **`org_admin` / `cos_admin` blocks** take either a `token` or a
  `username` + `password`.
- **Email verification.** Creating a user through the Keycloak Admin API sends
  nothing, which is why `user.email_verified` defaults to `true` — the account
  arrives already verified and can sign in at once. To exercise the real path
  instead, set `user.email_verified: false` and pick a mail route:

  | `create.verify_email` | what Keycloak does |
  |---|---|
  | `none` (default) | nothing is mailed |
  | `send` | `PUT .../send-verify-email` — the plain "confirm your address" mail |
  | `actions` | `PUT .../execute-actions-email` with `create.email_actions`, so the link can also carry `UPDATE_PASSWORD`, `CONFIGURE_TOTP`, … |

  `user.required_actions: ["VERIFY_EMAIL"]` marks the account as owing the
  action without mailing anything; a browser login then prompts for it.
  `email_link_client_id`, `email_link_redirect_uri` and
  `email_link_lifespan_seconds` shape the link.

  An account that owes a required action **cannot use the password grant**, so
  set `create.fetch_token` and `create.touch_control_plane` to `false` when
  testing this — otherwise the run fails right after creating the user. And the
  mail goes to whatever address the account carries: point `user.email` or
  `user.email_domain` at an inbox you actually own.
- **Output.** Every request and every response is printed as the run goes, and
  a creation script ends with an `=== account created ===` block carrying the
  username, password, Keycloak id, roles and organisation id; a deletion script
  opens with the matching `=== account to delete ===`. The `logging` block
  controls it:

  ```json
  "logging": {
    "level": "INFO",
    "print_requests": true,
    "print_responses": true,
    "response_preview_chars": 2000,
    "mask_secrets": false
  }
  ```

  `mask_secrets` is **off**, so bodies print verbatim — the account password,
  the Keycloak admin client secret and access tokens included. That is what
  makes the output worth reading during a manual test, and it is also why the
  output should not be pasted into a ticket or chat, or redirected into a file
  that outlives the run. Set it to `true` and passwords, secrets, tokens and
  JWTs are replaced with `***` everywhere, the summary blocks included.

## The configs in this tree

The `*_config.json` files beside each script are filled in from `main/config.json`
— the dev deployment (`v2.dev.iudx.io`, realm `iudx-v2`) — and are gitignored,
as every `*_config.json` in this repository is. The `.json.example` beside each
one is the committed template.

Two passwords are not in any config here, so they are read from the environment:

```bash
export ORG_ADMIN_PASS=…   # the org admin's password
export COS_ADMIN_PASS=…   # the COS admin's password
```

- **consumer** needs neither.
- **provider** needs `ORG_ADMIN_PASS` in the join modes; `--mode org_create`
  needs `COS_ADMIN_PASS` instead and no pre-existing organisation.
- **org_admin** needs `COS_ADMIN_PASS`.

Accounts are named `e2e-dev-<role>-<timestamp>-<random>@example.invalid`, which
is the prefix the main harness sweep already recognises, so an orphan from an
interrupted run does not become permanent.

`org_admin/deletion` ships with `postgres.enabled: false`. Turning it on lets the
script delete rows from the dev database — scoped to the account and the
organisation this pair created, but still the shared dev database. Run it with
`--pg-dry-run` first and read the statements it prints.
