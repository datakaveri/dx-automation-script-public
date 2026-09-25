# ControlPlane cleanup scripts

Every step the harness's teardown runs, as a script that can be run on its own.
Copy the `.json.example` beside a script, paste in the values, run it — the
same way the S3, NGSI-LD and OGC deletion scripts already work. A filled-in
`*_config.json` beside each script is gitignored, so the dev one can stay there.

```
CONTROLPLANE_Cleanup_Script/
├── keycloak_user_sweep/    keycloak_user_sweep.py    + keycloak_user_sweep_config.json     GAP
├── organisation_deletion/  organisation_deletion.py  + organisation_deletion_config.json   GAP
├── database_sweep/         database_sweep.py         + database_sweep_config.json          GAP
├── cleanup_verify/         cleanup_verify.py         + cleanup_verify_config.json          check
├── item_deletion/          item_deletion.py          + item_deletion_config.json           endpoint
├── artefact_deletion/      artefact_deletion.py      + artefact_deletion_config.json       endpoint
└── compute_credit_cleanup/ compute_credit_cleanup.py + compute_credit_cleanup_config.json  GAP
```

**GAP** marks what the platform has no working DELETE endpoint for — these are
the scripts a teardown cannot do without. **endpoint** marks a convenience
wrapper around a DELETE the platform does offer (the Postman collection calls
the same request); they are here so one config can clear a list of ids without
opening Postman, but nothing depends on them.

```bash
python -m pip install -r requirements.txt

cd database_sweep
cp database_sweep_config.json.example database_sweep_config.json
python database_sweep.py --dry-run        # every script has --dry-run; start there
python database_sweep.py
```

Exit codes are the same across all of them: `0` done (or already gone), `1`
something could not be removed, `2` the config was rejected. Credentials may be
written into the config or referenced as `${VAR}` / `${VAR:-fallback}`.

## Where the gaps are

What a teardown has to remove, and whether the platform offers an endpoint for
it. Everything in the right-hand column without an endpoint has a script.

| Artefact | Endpoint? | Script |
|---|---|---|
| Catalogue item | `DELETE /cat/item` (409 while a policy is active) | Postman folder 08 — or `item_deletion` |
| Policy | `PUT /acl/apd/v2/policy` deactivates, row stays | Postman folder 19 — row: `database_sweep` |
| Access request, credit/compute/asset request, subscription, app, feedback, delegation, resource server | DELETE exists, several soft-delete | Postman folders — or `artefact_deletion`; rows: `database_sweep` |
| Credit balance of one consumer | `PUT /admin/user/credit/deduct` zeroes it, rows stay | `compute_credit_cleanup` |
| Compute request, once granted | **none** — DELETE refuses anything but `pending`, and the admin PUT will not set `pending` | `compute_credit_cleanup` (deletes the `compute_role` row) |
| One consumer's credit rows (`credit_transactions`, `credit_requests`, `user_credits`, `kyc_transactions`) | **none** | `compute_credit_cleanup` — or `database_sweep` by prefix |
| Consumer account | `DELETE /auth/user/delete` (self) | `user_creation/consumer/deletion` |
| Org admin / provider account | **none** — self-delete refuses org admins | `keycloak_user_sweep` (`user_creation/*/deletion` for one) |
| Organisation, and every row keyed on it | **`DELETE /organisations/{id}` fails** on any approved org (FK, admin row unremovable) | `organisation_deletion` — by org id, or by the approved request's `req_id` |
| Leaderboards, credit rows, soft-deleted policy/request rows, audit logs | **none** | `database_sweep` |
| RabbitMQ exchange / queue / broker user | **none** (management API only) | `NGSILD_Automation_Script/ngsild_delete_v1.py`, `GATEWAY_Automation_Script/delete_rmq.py` |
| Elasticsearch data index | **none** | `ngsild_delete_v1.py`; orphans: `NGSILD_Automation_Script/Index_Sweep/es_index_sweep.py` |
| OGC collection rows, table, STAC child rows, `roles` row | **none** | `OGC_Automation_Script/Vector_Automation/Deletion/vector_deletion.py`; by prefix / with children / roles: `Extra_To_Clean_Collections/ogc_collection_sweep.py` |
| STAC items | STAC API | `OGC_Automation_Script/Raster_Automation/Deletion/stac_deletion.py` |
| Uploaded files | Files Connect API | `FILE_Automation_Script/deletion/file_deletion.py` |
| `.gpkg` / tiff folder in S3 | **none** | `OGC_Automation_Script/Extra_To_Clean_S3/s3_deleteion.py` |
| Proof it all went | — | `cleanup_verify` |

## The whole teardown, step by step

This is the order `complete_test.py` and `main/e2e.py` follow.

| # | What goes | Script |
|---|---|---|
| 1 | NGSI-LD index, exchange, broker user | `ngsild_delete_v1.py` |
| 2 | Gateway queue, broker user | `delete_rmq.py` |
| 3 | Uploaded files | `file_deletion.py` |
| 4 | STAC items | `stac_deletion.py` |
| 5 | OGC collection rows, table (+ STAC children, roles row) | `vector_deletion.py` / `ogc_collection_sweep.py` |
| 6 | `.gpkg` / tiff folder in S3 | `s3_deleteion.py` |
| 7 | Policies, then the catalogue item | Postman 19 + 08, or `item_deletion` |
| 7b | The collection's other DELETE folders | Postman, or `artefact_deletion` |
| 7c | A consumer's balance, compute request and role, credit rows | `compute_credit_cleanup` |
| 8 | Consumers (self-delete) | `consumer_deletion.py` |
| 9 | Organisation | `organisation_deletion` (or `org_admin_deletion.py` with its admin) |
| 10 | Every Keycloak account under the prefix | `keycloak_user_sweep` |
| 11 | Every database row under the prefix | `database_sweep` |
| 12 | Orphaned Elasticsearch indices | `es_index_sweep.py` |
| 13 | Check | `cleanup_verify` |

Steps 1–6 must run **before** 7: the exchange, the queue, the databank, the STAC
collection and the OGC collection are all named after the item, and their
servers refuse to talk about an item that no longer exists. Step 12 exists for
when that order was not kept — an index whose item is gone is reachable by id
and nothing else.

Steps 10 and 11 are the point of no return, and 11 depends on 10 not having
happened yet: the database sweep keys half its DELETEs on Keycloak user ids,
which it resolves from Keycloak when `keycloak.*` is filled in. Either run
`database_sweep` first, or run `keycloak_user_sweep` first and paste the ids it
prints into `database_sweep`'s `target.user_ids`.

## item_deletion

Deactivates every policy on an item through the ACL server, then deletes the
item. That order is the platform's: `DELETE /cat/item` answers 409 while an
active policy exists. A policy that is already inactive is fine.

Three ways to say which items, combinable:

- `target.item_ids` — pasted ids, deleted as `owner`
- `target.name_prefix` — everything `owner` has in `/cat/search/myassets` whose
  name starts with this
- `sweep.enabled` — sign in as every Keycloak account under
  `sweep.username_prefix` (they share `sweep.password`) and delete its prefixed
  items. This is the pass the harness runs after a collection, for items whose
  owner is not the run's own provider. Needs `keycloak.admin_client_*`. An
  account that cannot be signed in as is a problem only if it is a provider —
  nobody else can own an item.

`owner.token` skips the Keycloak sign-in when you already have one.

## artefact_deletion

The Postman collection's own teardown folders, as a script: one DELETE per id,
made as the role that created the thing. Paste ids under `target.<type>` —
`resource_server`, `credit_request`, `compute_request`, `delegation`,
`asset_request`, `subscription`, `app`, `user_feedback`, `provider_feedback`,
`policy`, `item` — and fill in the `actors` those types need (`cos_admin` for
resource servers, `consumer` for what a consumer raised, `provider` for the
rest). `types.<type>.actor` re-points a type at another actor when a deployment
differs. A 404 counts as done. The script's docstring has the full endpoint
table.

Several of these are soft deletes on the platform side; the rows go with
`database_sweep`.

## compute_credit_cleanup

Puts one consumer back where it was before its compute request was approved and
its credits were added — so the same account can request compute again next
run. It takes the **consumer** (user id, or username; the file
`consumer_creation.py` wrote also works), not a list of ids, and runs on its own:
no `database_sweep` afterwards, and no consumer password needed.

What one run does, in order:

| Step | How | Needs |
|---|---|---|
| Read the balance and deduct it to 0 | `PUT /admin/user/credit/deduct` | `actors.cos_admin` |
| Delete pending credit requests | `DELETE /user/credit/request/{id}` | the consumer's password (optional) |
| Delete the compute request, **any status** | `DELETE FROM compute_role WHERE id AND user_id` | `postgres` |
| Sweep the credit rows | `database_sweep`'s own SQL, for this one user id | `postgres` |
| Take the `compute` realm role off | Keycloak Admin API | `keycloak.admin_client_*`, `revoke_compute_role` |
| Unset `kyc_verified`, self-delete the account | Keycloak / `DELETE /user/delete` | only with `--clear-kyc` / `--delete-user` |
| Read everything back | balance, `compute_role` | — |

**Why the compute request goes through the database.** The platform only
deletes a `pending` compute request (`400 Only pending compute requests can be
deleted`), and nothing takes a grant back — the cos_admin
`PUT /auth/compute/requests/{id}` accepts only `granted` or `rejected`. What the
DELETE endpoint does (`ComputeRoleHandler.deletePendingComputeRequests` in
dx-controlplane) is one `DELETE FROM compute_role WHERE id = ?` plus an audit
event, so the script does that delete itself, for any status, keeping the
endpoint's ownership check as `AND user_id`. The audit event is not
reproduced. This matters beyond tidiness: `compute_role.user_id` is UNIQUE, so
while the row survives the account can never request compute again. The run
ends by saying whether a row is left. `clean.compute_via_database` switches
this off; with the `postgres` block empty the API is used instead, and a
granted request is reported as left behind.

**The credit rows.** Deducting never removes `user_credits`, and every add and
deduct writes a `credit_transactions` row (the deduct itself writes one). With
`clean.sweep_credit_rows` (on by default) the run deletes this user's rows from
`credit_transactions`, `credit_requests`, `user_credits` and `kyc_transactions`
after the deduct. The SQL is imported from `script/ControlPlane_Workflow/cleanup.py`,
the same statements `database_sweep` runs, but keyed on this one user and only
these four tables. The account's policies, access requests and memberships are
left alone. Like `database_sweep`, the script has to be run from inside this
checkout.

**Approval also grants the Keycloak `compute` role.** Deleting the row does not
take it off, so for a full revert set `clean.revoke_compute_role` (or pass
`--revoke-role`).

The balance deduct is the exact inverse of the add call, with the same
`{"user_id", "amount", "requested_at"}` body and the same cos_admin token.
`requested_at` is the platform's idempotency key: repeating a triple answers
409 Duplicate transaction request, so the timestamp defaults to now and is
retried on 409.

```bash
cp compute_credit_cleanup_config.json.example compute_credit_cleanup_config.json
# fill target.user_id (or username), actors.cos_admin, keycloak.admin_client_*, postgres
python compute_credit_cleanup.py --inventory-only     # read everything, change nothing
python compute_credit_cleanup.py --dry-run            # DB deletes run and roll back: real counts
python compute_credit_cleanup.py --revoke-role
python compute_credit_cleanup.py --username someone@example.invalid --user-id <uuid>
```

`target.password` is optional. A wrong or changed password is a warning, not a
stop. Without it only the pending credit-request API delete and `--delete-user`
are lost, and the sweep covers `credit_requests` anyway. `--user-id` saves a
Keycloak lookup, but when you pass `--username` for a new account, clear
`target.user_id` or pass the matching `--user-id` as well. Otherwise the config's
old id is used for the balance and the database steps.

Rerunning is safe: each step reports "nothing to" when the account is already
clean. If the same account is also driven by a Postman run at the same time,
this script will delete that run's compute grant, so run it when nothing else
is using the account.

## keycloak_user_sweep

Deletes Keycloak accounts through the Admin API, by exact `target.usernames`
and/or every account under `target.prefix`. The per-role scripts in
`user_creation/` delete one account each and prefer the platform's self-delete;
this one is for what is left when the flow is over — org admins, which the
platform refuses to self-delete, and the accounts of a run that crashed.

`target.protected_usernames` is checked before every delete. Name any borrowed
platform account here (the cos_admin, the OGC provider). `target.older_than_hours`
leaves accounts younger than that alone, for a prefix several people share. A
prefix shorter than three characters is refused.

It prints the ids it deleted: they are also the database's user ids.

## database_sweep

Runs the harness's `SWEEP_STATEMENTS` — the DELETEs the APIs never make: soft-
deleted policies and access requests, the organisation an org admin leaves
behind, leaderboard and credit rows — against the ControlPlane database, in
children-before-parents order.

The SQL is **imported** from `script/ControlPlane_Workflow/cleanup.py` rather
than copied, so a table added there is swept here too. That module also adapts
each statement to the live schema: a table this deployment lacks is skipped, a
column it lacks is dropped from its OR-group, and a statement that would lose
its whole anchor is skipped rather than widened. The only consequence for you is
that this script has to be run from inside this checkout.

Rows are found by `target.prefix` (`LIKE '<prefix>%'` on emails, org names,
asset names) **and** by id — user, org, item — for the tables that carry no
namespaced column. Ids come from the prefix queries, from Keycloak when
`keycloak.*` is set, and from whatever is pasted into `target.*_ids`.
`target.cos_admin_id` names a borrowed administrator whose rows for this run
should go while the account stays; `sweep.delete_all_cos_admin_data` widens that
to every row it owns.

`--dry-run` runs every DELETE inside one transaction, prints the row counts,
and rolls back — the counts are real. `--verify-only` just counts what is left.
`--dry-run` still needs a user that may DELETE; a read-only role will report
permission errors, not counts.

## organisation_deletion

`DELETE /organisations/{id}` cannot succeed for an approved organisation: no
cascade, and the admin's membership row can never be removed by the API. So
`delete.mode` is `auto` (try the API, fall back to the database), `api`, or
`postgres` — the one to use when the admin account is already gone.

Name the organisation by id (`target.org_ids` / `--org-id`) or by the
organisation-create request that was approved (`target.request_ids` /
`--request-id`, the `req_id` sent to `/organisations/requests/approve`). A
request id is resolved to its organisation through the shared name, and when
that misses — `PUT /organisations/{id}` renames the org, and the collection's
"update org details" test does so every run — through the admin membership
approval wrote (`organization_users.user_id = requested_by`, or
`official_email = manager_email`). Nothing else links the two. The request row
goes with it.

The database pass runs every org-keyed statement from the harness sweep with
the organisation id as the only anchor, so nothing else can match:
`organization_users`, `organization_join_requests`, `provider_requests`,
`organization_create_requests`, `access_rule_allowed_org`, `policy` and
`request` rows against its items, `shared_asset_visibility`,
`asset_visibility_snapshot`, the three leaderboards, `leaderboard_dirty_queue`,
and finally `organizations`. `delete.audit_rows` adds the activity/audit log
tables. Every table is re-counted afterwards and a surviving row fails the run.

Users are left to `keycloak_user_sweep` — but approval stamped the admin's
Keycloak account with an `organisation_id` attribute and the `org_admin` /
`provider` roles, and an account left pointing at a deleted org breaks its next
sign-in. With `keycloak.admin_client_*` set, every account carrying the
organisation's id has both removed (`delete.detach_keycloak_users`, on by
default; a warning when the admin client is not configured).

## cleanup_verify

Deletes nothing. Lists what is still there for a prefix and a set of ids:
Keycloak users, the database's namespaced rows (the harness's
`VERIFY_STATEMENTS`), a RabbitMQ exchange/queue per item id and a broker user
per provider id, an Elasticsearch index per item id, OGC collections by title.
Each check has its own `enabled`. Exit 1 with the list when anything survived.

## What has no script, on purpose

Nothing, now. The Postman collection's own DELETE requests run through newman
in the harness and are wrapped by `item_deletion`/`artefact_deletion` here; the
harness's `verify` is `cleanup_verify`.
