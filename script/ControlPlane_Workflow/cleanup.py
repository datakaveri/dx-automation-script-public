#!/usr/bin/env python3
"""
Teardown, and the proof that teardown worked.

Runs whether the flow passed or failed — which is the whole reason the harness
is a script rather than a collection. Order matters: an item cannot be removed
after its owner, and the org admin cannot be removed by the API at all.

No step raises. One undeletable artefact must not strand every artefact behind
it, so failures are collected and reported at the end.
"""

import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from .client import field, rows_of, try_delete
from .config import (
    GATEWAY_TYPES,
    NGSILD_TYPES,
    file_server,
    gateway_server,
    is_file_server,
    is_ogc_raster,
    is_ogc_vector,
    ngsild_server,
    ogc_raster_server,
    ogc_vector_server,
)
from .flow import (
    declared_servers,
    item_declares_file,
    item_declares_gateway,
    item_declares_ngsild,
    item_declares_ogc_raster,
    item_declares_ogc_vector,
)
from .file_server import file_delete
from .ogc_raster import ogc_raster_delete
from .ogc_s3 import RASTER, VECTOR, deleted_count, ogc_s3_delete
from .gateway_delete import gateway_delete
from .ngsild_delete import delete_index, list_indices, ngsild_delete
from .ogc_vector import find_collections, ogc_vector_delete, remove_provider_role

# Anchors: the namespaced columns that identify a run's rows without needing any
# id captured at runtime, so orphans from a crashed run are reachable too.
#
# user_table is a weak anchor and must never be the only one. It is populated
# lazily by UserAccessHandler (UserAccessHandler.java:71), which is attached
# only to the ACL routes — UPDATE_ACCESS_REQUEST, CREATE_POLICY, DELETE_POLICY,
# VERIFY. A run that fails before phase 03 never reaches any of them, so its
# users have Keycloak accounts and organisation rows but no user_table row at
# all. Keycloak is the source of truth for user ids; see _resolve_user_ids.
ANCHOR_QUERIES = {
    "user_ids": "SELECT _id FROM {schema}.user_table WHERE email_id LIKE %(pattern)s",
    "org_ids": "SELECT id FROM {schema}.organizations WHERE name LIKE %(pattern)s",
    # policy has no email column — it keys on consumer_id/owner_id — so the
    # only prefix-matchable source of item ids is the request table.
    "item_ids": (
        "SELECT DISTINCT item_id FROM {schema}.request "
        "WHERE consumer_email_id LIKE %(pattern)s"
    ),
}

# Ordered children-before-parents. policy.owner_id and provider_requests.user_id
# are real foreign keys, so user_table and organizations must go last or the
# delete fails outright.
#
# Policies and access requests are hard-deleted here on purpose: the APIs only
# soft-delete them (PUT /policy flips ACTIVE to DELETE and keeps the row), so
# leaving it to the API would strand rows in the database forever.
SWEEP_STATEMENTS = [
    # Leaderboards are derived tables the platform maintains in the background,
    # so a run leaves rows here even though it never writes them directly. They
    # carry namespaced *_name columns, which matters: the ids they reference are
    # gone once organizations and user_table are swept, so name matching is the
    # only way to reach rows left by an earlier run.
    (
        "asset_leaderboard",
        "DELETE FROM {schema}.asset_leaderboard "
        "WHERE asset_name LIKE %(pattern)s OR organization_name LIKE %(pattern)s "
        "OR provider_id = ANY(%(user_ids)s::uuid[]) OR asset_id = ANY(%(item_ids)s::uuid[]) "
        "OR organization_id = ANY(%(org_ids)s::uuid[])",
    ),
    (
        "provider_leaderboard",
        "DELETE FROM {schema}.provider_leaderboard "
        "WHERE organization_name LIKE %(pattern)s "
        "OR provider_id = ANY(%(user_ids)s::uuid[]) "
        "OR organization_id = ANY(%(org_ids)s::uuid[])",
    ),
    (
        "organization_leaderboard",
        "DELETE FROM {schema}.organization_leaderboard "
        "WHERE organization_name LIKE %(pattern)s "
        "OR organization_id = ANY(%(org_ids)s::uuid[])",
    ),
    (
        "leaderboard_dirty_queue",
        "DELETE FROM {schema}.leaderboard_dirty_queue "
        "WHERE entity_id = ANY(%(item_ids)s::uuid[]) "
        "OR entity_id = ANY(%(user_ids)s::uuid[]) "
        "OR entity_id = ANY(%(org_ids)s::uuid[])",
    ),
    # Credit rows are provisioned for new users by the platform.
    (
        "credit_transactions",
        "DELETE FROM {schema}.credit_transactions "
        "WHERE user_id = ANY(%(user_ids)s::uuid[])",
    ),
    (
        "credit_requests",
        "DELETE FROM {schema}.credit_requests WHERE user_id = ANY(%(user_ids)s::uuid[])",
    ),
    (
        "user_credits",
        "DELETE FROM {schema}.user_credits WHERE user_id = ANY(%(user_ids)s::uuid[])",
    ),
    # Not written by the current flow, but cheap to cover and keyed on ids the
    # sweep already resolves — so a future phase cannot silently leak rows here.
    (
        "shared_asset_visibility",
        "DELETE FROM {schema}.shared_asset_visibility "
        "WHERE item_id = ANY(%(item_ids)s::uuid[]) "
        "OR user_id = ANY(%(user_ids)s::uuid[]) OR shared_by = ANY(%(user_ids)s::uuid[]) "
        "OR org_id = ANY(%(org_ids)s::uuid[])",
    ),
    (
        "asset_request",
        "DELETE FROM {schema}.asset_request "
        "WHERE user_id = ANY(%(user_ids)s::uuid[]) OR asset_id = ANY(%(item_ids)s::uuid[])",
    ),
    (
        "subscriptions",
        "DELETE FROM {schema}.subscriptions "
        "WHERE user_id = ANY(%(user_ids)s::uuid[])",
    ),
    (
        "bookmarks",
        "DELETE FROM {schema}.bookmarks "
        "WHERE user_id = ANY(%(user_ids)s::uuid[]) OR entity_id = ANY(%(item_ids)s::uuid[])",
    ),
    (
        "app_constraints",
        "DELETE FROM {schema}.app_constraints WHERE user_id = ANY(%(user_ids)s::uuid[])",
    ),
    (
        "app_credentials",
        "DELETE FROM {schema}.app_credentials WHERE user_id = ANY(%(user_ids)s::uuid[])",
    ),
    # Roles, KYC, feedback and delegations key on a user id but were never
    # listed, so the run's own users leaked rows here. Ordered before
    # access_rule because access_rule_allowed_user hangs off it.
    (
        "compute_role",
        "DELETE FROM {schema}.compute_role "
        "WHERE user_id = ANY(%(user_ids)s::uuid[]) "
        "OR user_name LIKE %(pattern)s",
    ),
    (
        "custom_user_role",
        "DELETE FROM {schema}.custom_user_role "
        "WHERE user_id = ANY(%(user_ids)s::uuid[])",
    ),
    (
        "kyc_transactions",
        "DELETE FROM {schema}.kyc_transactions WHERE user_id = ANY(%(user_ids)s::uuid[])",
    ),
    (
        "provider_feedback",
        "DELETE FROM {schema}.provider_feedback "
        "WHERE user_id = ANY(%(user_ids)s::uuid[]) "
        "OR asset_id = ANY(%(item_ids)s::uuid[])",
    ),
    # delegation_scope_constraints and delegation_update_requests cascade.
    (
        "delegation_grants",
        "DELETE FROM {schema}.delegation_grants "
        "WHERE delegator_id = ANY(%(user_ids)s::uuid[]) "
        "OR delegate_id = ANY(%(user_ids)s::uuid[])",
    ),
    # Owned infrastructure. The harness registers none, so this only ever
    # matches if a future phase starts doing so.
    (
        "resource_servers",
        "DELETE FROM {schema}.resource_servers WHERE owner_id = ANY(%(user_ids)s::uuid[])",
    ),
    (
        "acl_servers",
        "DELETE FROM {schema}.acl_servers WHERE owner_id = ANY(%(user_ids)s::uuid[])",
    ),
    # access_rule_allowed_{org,role,user} cascade from access_rule, but only
    # for rules the sweep itself deletes — a row naming one of our users on
    # somebody else's rule has to be matched directly. user_id is varchar here,
    # not uuid, so this one casts to text[].
    (
        "access_rule_allowed_user",
        "DELETE FROM {schema}.access_rule_allowed_user "
        "WHERE user_id = ANY(%(user_ids)s::text[])",
    ),
    (
        "access_rule_allowed_org",
        "DELETE FROM {schema}.access_rule_allowed_org "
        "WHERE org_id = ANY(%(org_ids)s::text[])",
    ),
    (
        "access_rule",
        "DELETE FROM {schema}.access_rule "
        "WHERE owner_id = ANY(%(user_ids)s::uuid[]) OR item_id = ANY(%(item_ids)s::uuid[])",
    ),
    (
        "policy",
        "DELETE FROM {schema}.policy "
        "WHERE owner_id = ANY(%(user_ids)s::uuid[]) "
        "OR consumer_id = ANY(%(user_ids)s::uuid[]) "
        "OR item_id = ANY(%(item_ids)s::uuid[]) "
        "OR item_organization_id = ANY(%(org_ids)s::uuid[])",
    ),
    (
        "request_messages",
        "DELETE FROM {schema}.request_messages WHERE sender_id = ANY(%(user_ids)s::uuid[])",
    ),
    (
        "request",
        "DELETE FROM {schema}.request "
        "WHERE consumer_email_id LIKE %(pattern)s "
        "OR provider_id = ANY(%(user_ids)s::uuid[]) OR consumer_id = ANY(%(user_ids)s::uuid[]) "
        "OR consumer_organization_id = ANY(%(org_ids)s::uuid[]) "
        "OR item_organization_id = ANY(%(org_ids)s::uuid[])",
    ),
    (
        "client_credentials",
        "DELETE FROM {schema}.client_credentials WHERE user_id = ANY(%(user_ids)s::uuid[])",
    ),
    (
        "user_interactions",
        "DELETE FROM {schema}.user_interactions "
        "WHERE user_id = ANY(%(user_ids)s::uuid[]) OR asset_id = ANY(%(item_ids)s::uuid[])",
    ),
    (
        "item_votes",
        "DELETE FROM {schema}.item_votes "
        "WHERE user_id = ANY(%(user_ids)s::uuid[]) OR entity_id = ANY(%(item_ids)s::uuid[])",
    ),
    (
        "asset_visibility_snapshot",
        "DELETE FROM {schema}.asset_visibility_snapshot "
        "WHERE provider_id = ANY(%(user_ids)s::uuid[]) OR asset_id = ANY(%(item_ids)s::uuid[]) "
        "OR organization_id = ANY(%(org_ids)s::uuid[])",
    ),
    (
        "provider_requests",
        "DELETE FROM {schema}.provider_requests "
        "WHERE organization_id = ANY(%(org_ids)s::uuid[]) OR user_id = ANY(%(user_ids)s::uuid[])",
    ),
    # The request row carries no organisation id — approval copies its name
    # into organizations and writes the requester as the org's admin, and
    # nothing links the two afterwards. An org known only by id reaches its
    # request through either: the name, until PUT /organisations/{id} renames
    # the org (the collection does, every run); or the admin membership, whose
    # user_id is the request's requested_by. Both lookups need their rows still
    # there, so this runs before organization_users and organizations. Nothing
    # references this table, so it can go first.
    (
        "organization_create_requests",
        "DELETE FROM {schema}.organization_create_requests "
        "WHERE name LIKE %(pattern)s OR requested_by = ANY(%(user_ids)s::uuid[]) "
        "OR name IN (SELECT name FROM {schema}.organizations "
        "WHERE id = ANY(%(org_ids)s::uuid[])) "
        "OR requested_by IN (SELECT user_id FROM {schema}.organization_users "
        "WHERE organization_id = ANY(%(org_ids)s::uuid[]) AND role = 'admin')",
    ),
    (
        "organization_join_requests",
        "DELETE FROM {schema}.organization_join_requests "
        "WHERE organization_id = ANY(%(org_ids)s::uuid[]) OR user_id = ANY(%(user_ids)s::uuid[])",
    ),
    (
        "organization_users",
        "DELETE FROM {schema}.organization_users "
        "WHERE organization_id = ANY(%(org_ids)s::uuid[]) OR user_id = ANY(%(user_ids)s::uuid[])",
    ),
    (
        "organizations",
        "DELETE FROM {schema}.organizations "
        "WHERE name LIKE %(pattern)s OR id = ANY(%(org_ids)s::uuid[])",
    ),
    # By id as well as by email: user_ids is resolved from Keycloak (minus the
    # protected accounts) before anything is deleted, and a caller that has
    # only an id — org_admin_deletion, database_sweep's target.user_ids — has
    # no email to match on.
    (
        "user_table",
        "DELETE FROM {schema}.user_table "
        "WHERE email_id LIKE %(pattern)s OR _id = ANY(%(user_ids)s::uuid[])",
    ),
]

# Append-only by design. Swept only when run.delete_audit_rows is on, which
# should be true solely on a stack you own.
#
# The run's own users are namespaced, so every row they own is this run's and
# the account id alone is scope enough.
#
# Every row also names the organisation it was made in or against, so an
# organisation swept by id takes its history with it — the only way to reach
# rows written by an account that is not in user_ids (a borrowed admin, a user
# already deleted) about this run's org.
AUDIT_SWEEP_STATEMENTS = [
    (
        "user_activity_audit_log",
        "DELETE FROM {schema}.user_activity_audit_log "
        "WHERE user_id = ANY(%(user_ids)s::uuid[]) "
        "OR org_id = ANY(%(org_ids)s::uuid[]) OR asset_org_id = ANY(%(org_ids)s::uuid[])",
    ),
    # The platform keeps a copy of the table above; same rows, same keys.
    (
        "user_activity_audit_log_backup",
        "DELETE FROM {schema}.user_activity_audit_log_backup "
        "WHERE user_id = ANY(%(user_ids)s::uuid[]) "
        "OR org_id = ANY(%(org_ids)s::uuid[]) OR asset_org_id = ANY(%(org_ids)s::uuid[])",
    ),
    # The live table the UI reads.
    (
        "user_activity_log",
        "DELETE FROM {schema}.user_activity_log "
        "WHERE user_id = ANY(%(user_ids)s::uuid[]) OR org_id = ANY(%(org_ids)s::uuid[])",
    ),
    # A second, separate audit table — not a view over the one above.
    (
        "activity_audit_log",
        "DELETE FROM {schema}.activity_audit_log "
        "WHERE user_id = ANY(%(user_ids)s::uuid[]) OR org_id = ANY(%(org_ids)s::uuid[])",
    ),
]

# A borrowed cos_admin is different: the account is never deleted and it is kept
# out of user_ids, so anything it owns needs statements of its own. How far they
# reach is run.delete_all_cos_admin_data.
#
# Both log tables, scoped to this run. Every row the harness causes through the
# admin names the item it published or the organisation it approved, so the
# anchors reach all of them; a row naming neither is the account's own history.
COS_ADMIN_LOG_SCOPED = [
    (
        "user_activity_audit_log (cos admin, this run)",
        "DELETE FROM {schema}.user_activity_audit_log "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[]) AND ("
        "asset_id = ANY(%(item_ids)s::uuid[]) "
        "OR org_id = ANY(%(org_ids)s::uuid[]) "
        "OR asset_provider_id = ANY(%(user_ids)s::uuid[]) "
        "OR asset_name LIKE %(pattern)s "
        "OR org_name LIKE %(pattern)s)",
    ),
    (
        "user_activity_log (cos admin, this run)",
        "DELETE FROM {schema}.user_activity_log "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[]) AND ("
        "asset_id = ANY(%(item_ids)s::uuid[]) "
        "OR org_id = ANY(%(org_ids)s::uuid[]) "
        "OR asset_name LIKE %(pattern)s "
        "OR org_name LIKE %(pattern)s)",
    ),
    (
        "activity_audit_log (cos admin, this run)",
        "DELETE FROM {schema}.activity_audit_log "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[]) AND ("
        "entity_id = ANY(%(item_ids)s::uuid[]) "
        "OR org_id = ANY(%(org_ids)s::uuid[]) "
        "OR provider_id = ANY(%(user_ids)s::uuid[]) "
        "OR entity_name LIKE %(pattern)s "
        "OR org_name LIKE %(pattern)s)",
    ),
]

# The same two tables, account-wide.
COS_ADMIN_LOG_ALL = [
    (
        "user_activity_audit_log (cos admin, all)",
        "DELETE FROM {schema}.user_activity_audit_log "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "user_activity_log (cos admin, all)",
        "DELETE FROM {schema}.user_activity_log "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "activity_audit_log (cos admin, all)",
        "DELETE FROM {schema}.activity_audit_log "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
]

# Everywhere else the account's id appears, restricted to this run: the same
# user-keyed columns the main sweep matches on, pointed at the cos_admin, AND-ed
# with the item, organisation or name anchor that makes the row this run's.
#
# Tables keyed on nothing but a user id are absent — credits, subscriptions,
# apps, clients, request_messages, leaderboard_dirty_queue. There is no anchor
# to scope them by, and the harness never touches them through the cos_admin
# anyway: it registers no client for that account, sends no request message and
# provisions no credits. They are swept only under delete_all_cos_admin_data.
#
# The main sweep would reach most of these rows on its own, since its item and
# org branches match whoever owns the row. Stating them here anyway keeps "what
# this run deletes for a borrowed cos_admin" readable in one place instead of
# inferred from twenty-odd OR clauses elsewhere.
COS_ADMIN_ROWS_SCOPED = [
    (
        "asset_leaderboard (cos admin, this run)",
        "DELETE FROM {schema}.asset_leaderboard "
        "WHERE provider_id = ANY(%(cos_admin_ids)s::uuid[]) AND ("
        "asset_name LIKE %(pattern)s OR organization_name LIKE %(pattern)s "
        "OR asset_id = ANY(%(item_ids)s::uuid[]) "
        "OR organization_id = ANY(%(org_ids)s::uuid[]))",
    ),
    (
        "provider_leaderboard (cos admin, this run)",
        "DELETE FROM {schema}.provider_leaderboard "
        "WHERE provider_id = ANY(%(cos_admin_ids)s::uuid[]) AND ("
        "organization_name LIKE %(pattern)s "
        "OR organization_id = ANY(%(org_ids)s::uuid[]))",
    ),
    (
        "shared_asset_visibility (cos admin, this run)",
        "DELETE FROM {schema}.shared_asset_visibility "
        "WHERE (user_id = ANY(%(cos_admin_ids)s::uuid[]) "
        "OR shared_by = ANY(%(cos_admin_ids)s::uuid[])) AND ("
        "item_id = ANY(%(item_ids)s::uuid[]) OR org_id = ANY(%(org_ids)s::uuid[]))",
    ),
    (
        "asset_request (cos admin, this run)",
        "DELETE FROM {schema}.asset_request "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[]) "
        "AND asset_id = ANY(%(item_ids)s::uuid[])",
    ),
    (
        "bookmarks (cos admin, this run)",
        "DELETE FROM {schema}.bookmarks "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[]) "
        "AND entity_id = ANY(%(item_ids)s::uuid[])",
    ),
    (
        "access_rule (cos admin, this run)",
        "DELETE FROM {schema}.access_rule "
        "WHERE owner_id = ANY(%(cos_admin_ids)s::uuid[]) "
        "AND item_id = ANY(%(item_ids)s::uuid[])",
    ),
    (
        "policy (cos admin, this run)",
        "DELETE FROM {schema}.policy "
        "WHERE (owner_id = ANY(%(cos_admin_ids)s::uuid[]) "
        "OR consumer_id = ANY(%(cos_admin_ids)s::uuid[])) "
        "AND item_id = ANY(%(item_ids)s::uuid[])",
    ),
    (
        "request (cos admin, this run)",
        "DELETE FROM {schema}.request "
        "WHERE (provider_id = ANY(%(cos_admin_ids)s::uuid[]) "
        "OR consumer_id = ANY(%(cos_admin_ids)s::uuid[])) "
        "AND consumer_email_id LIKE %(pattern)s",
    ),
    (
        "user_interactions (cos admin, this run)",
        "DELETE FROM {schema}.user_interactions "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[]) "
        "AND asset_id = ANY(%(item_ids)s::uuid[])",
    ),
    (
        "item_votes (cos admin, this run)",
        "DELETE FROM {schema}.item_votes "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[]) "
        "AND entity_id = ANY(%(item_ids)s::uuid[])",
    ),
    (
        "asset_visibility_snapshot (cos admin, this run)",
        "DELETE FROM {schema}.asset_visibility_snapshot "
        "WHERE provider_id = ANY(%(cos_admin_ids)s::uuid[]) "
        "AND asset_id = ANY(%(item_ids)s::uuid[])",
    ),
    (
        "provider_requests (cos admin, this run)",
        "DELETE FROM {schema}.provider_requests "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[]) "
        "AND organization_id = ANY(%(org_ids)s::uuid[])",
    ),
    (
        "organization_join_requests (cos admin, this run)",
        "DELETE FROM {schema}.organization_join_requests "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[]) "
        "AND organization_id = ANY(%(org_ids)s::uuid[])",
    ),
    (
        "organization_users (cos admin, this run)",
        "DELETE FROM {schema}.organization_users "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[]) "
        "AND organization_id = ANY(%(org_ids)s::uuid[])",
    ),
    (
        "organization_create_requests (cos admin, this run)",
        "DELETE FROM {schema}.organization_create_requests "
        "WHERE requested_by = ANY(%(cos_admin_ids)s::uuid[]) "
        "AND name LIKE %(pattern)s",
    ),
    # The admin approving a run user's compute role, or requesting a scope for
    # one: the row is the admin's, the subject is this run's.
    (
        "compute_role (cos admin, this run)",
        "DELETE FROM {schema}.compute_role "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "custom_user_role (cos admin, this run)",
        "DELETE FROM {schema}.custom_user_role "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "provider_feedback (cos admin, this run)",
        "DELETE FROM {schema}.provider_feedback "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[]) "
        "AND asset_id = ANY(%(item_ids)s::uuid[])",
    ),
    (
        "delegation_grants (cos admin, this run)",
        "DELETE FROM {schema}.delegation_grants "
        "WHERE (delegator_id = ANY(%(cos_admin_ids)s::uuid[]) "
        "OR delegate_id = ANY(%(cos_admin_ids)s::uuid[])) AND ("
        "delegator_id = ANY(%(user_ids)s::uuid[]) "
        "OR delegate_id = ANY(%(user_ids)s::uuid[]))",
    ),
]

# Everywhere else the account's id appears — the same user-keyed columns the
# main sweep matches on, pointed at the cos_admin instead. Runs only under
# run.delete_all_cos_admin_data, because unlike the log tables these rows are
# how the platform holds the account together: its credits, its registered
# apps, its organisation memberships. Deleting them leaves the Keycloak user and
# its user_table row intact but strips it back to a bare account, which is what
# a throwaway stack wants and what a shared one must never do.
#
# user_table and organizations are absent on purpose: they are keyed by name
# pattern, not by user id, and user_table *is* the user — the one thing this
# whole split exists to preserve.
#
# Ordered children-before-parents, matching SWEEP_STATEMENTS.
COS_ADMIN_ROWS_ALL = [
    (
        "asset_leaderboard (cos admin)",
        "DELETE FROM {schema}.asset_leaderboard "
        "WHERE provider_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "provider_leaderboard (cos admin)",
        "DELETE FROM {schema}.provider_leaderboard "
        "WHERE provider_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "leaderboard_dirty_queue (cos admin)",
        "DELETE FROM {schema}.leaderboard_dirty_queue "
        "WHERE entity_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "credit_transactions (cos admin)",
        "DELETE FROM {schema}.credit_transactions "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "credit_requests (cos admin)",
        "DELETE FROM {schema}.credit_requests "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "user_credits (cos admin)",
        "DELETE FROM {schema}.user_credits "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "shared_asset_visibility (cos admin)",
        "DELETE FROM {schema}.shared_asset_visibility "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[]) "
        "OR shared_by = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "asset_request (cos admin)",
        "DELETE FROM {schema}.asset_request "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "subscriptions (cos admin)",
        "DELETE FROM {schema}.subscriptions "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "bookmarks (cos admin)",
        "DELETE FROM {schema}.bookmarks "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "app_constraints (cos admin)",
        "DELETE FROM {schema}.app_constraints "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "app_credentials (cos admin)",
        "DELETE FROM {schema}.app_credentials "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "access_rule (cos admin)",
        "DELETE FROM {schema}.access_rule "
        "WHERE owner_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "policy (cos admin)",
        "DELETE FROM {schema}.policy "
        "WHERE owner_id = ANY(%(cos_admin_ids)s::uuid[]) "
        "OR consumer_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "request_messages (cos admin)",
        "DELETE FROM {schema}.request_messages "
        "WHERE sender_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "request (cos admin)",
        "DELETE FROM {schema}.request "
        "WHERE provider_id = ANY(%(cos_admin_ids)s::uuid[]) "
        "OR consumer_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "client_credentials (cos admin)",
        "DELETE FROM {schema}.client_credentials "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "user_interactions (cos admin)",
        "DELETE FROM {schema}.user_interactions "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "item_votes (cos admin)",
        "DELETE FROM {schema}.item_votes "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "asset_visibility_snapshot (cos admin)",
        "DELETE FROM {schema}.asset_visibility_snapshot "
        "WHERE provider_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "provider_requests (cos admin)",
        "DELETE FROM {schema}.provider_requests "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "organization_join_requests (cos admin)",
        "DELETE FROM {schema}.organization_join_requests "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "organization_users (cos admin)",
        "DELETE FROM {schema}.organization_users "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "organization_create_requests (cos admin)",
        "DELETE FROM {schema}.organization_create_requests "
        "WHERE requested_by = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "compute_role (cos admin)",
        "DELETE FROM {schema}.compute_role "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "custom_user_role (cos admin)",
        "DELETE FROM {schema}.custom_user_role "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "kyc_transactions (cos admin)",
        "DELETE FROM {schema}.kyc_transactions "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "provider_feedback (cos admin)",
        "DELETE FROM {schema}.provider_feedback "
        "WHERE user_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    # delegation_scope_constraints and delegation_update_requests cascade.
    (
        "delegation_grants (cos admin)",
        "DELETE FROM {schema}.delegation_grants "
        "WHERE delegator_id = ANY(%(cos_admin_ids)s::uuid[]) "
        "OR delegate_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "access_rule_allowed_user (cos admin)",
        "DELETE FROM {schema}.access_rule_allowed_user "
        "WHERE user_id = ANY(%(cos_admin_ids)s::text[])",
    ),
    # Infrastructure the account owns. Registrations the harness never creates
    # and cannot recreate — on a shared deployment this is the pair that turns
    # a sweep into an outage, which is why it sits behind this flag and this
    # flag only. Staging-or-throwaway stacks only.
    (
        "resource_servers (cos admin)",
        "DELETE FROM {schema}.resource_servers "
        "WHERE owner_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
    (
        "acl_servers (cos admin)",
        "DELETE FROM {schema}.acl_servers "
        "WHERE owner_id = ANY(%(cos_admin_ids)s::uuid[])",
    ),
]

# Checked after teardown. Anything non-zero fails the run.
VERIFY_STATEMENTS = [
    ("organizations", "SELECT count(*) FROM {schema}.organizations WHERE name LIKE %(pattern)s"),
    (
        "organization_create_requests",
        "SELECT count(*) FROM {schema}.organization_create_requests "
        "WHERE name LIKE %(pattern)s",
    ),
    ("user_table", "SELECT count(*) FROM {schema}.user_table WHERE email_id LIKE %(pattern)s"),
    (
        "request",
        "SELECT count(*) FROM {schema}.request WHERE consumer_email_id LIKE %(pattern)s",
    ),
    (
        "organization_users",
        "SELECT count(*) FROM {schema}.organization_users "
        "WHERE official_email LIKE %(pattern)s",
    ),
    # policy and access_rule carry no namespaced column, so they can only be
    # checked through the users that reference them. Once those users are gone
    # this reads zero regardless, which makes it a weaker check than the others
    # — the sweep itself is what guarantees these rows go, using ids resolved
    # from Keycloak before any deletion.
    (
        "provider_leaderboard",
        "SELECT count(*) FROM {schema}.provider_leaderboard "
        "WHERE organization_name LIKE %(pattern)s",
    ),
    (
        "organization_leaderboard",
        "SELECT count(*) FROM {schema}.organization_leaderboard "
        "WHERE organization_name LIKE %(pattern)s",
    ),
    (
        "asset_leaderboard",
        "SELECT count(*) FROM {schema}.asset_leaderboard WHERE asset_name LIKE %(pattern)s",
    ),
    (
        "policy",
        "SELECT count(*) FROM {schema}.policy WHERE owner_id IN "
        "(SELECT _id FROM {schema}.user_table WHERE email_id LIKE %(pattern)s) "
        "OR consumer_id IN "
        "(SELECT _id FROM {schema}.user_table WHERE email_id LIKE %(pattern)s)",
    ),
    # The compute/credit rows a granted compute role leaves behind. No endpoint
    # removes them — a granted credit request will not delete, and deducting a
    # balance to zero only writes one more transaction — so a run that went
    # through the compute journey and was verified without these read clean
    # while they were still there.
    #
    # compute_role is the one with a namespaced column of its own, and it is
    # also the one that matters most: compute_role.user_id is UNIQUE, so a
    # surviving row means that account can never request the compute role
    # again.
    (
        "compute_role",
        "SELECT count(*) FROM {schema}.compute_role WHERE user_name LIKE %(pattern)s",
    ),
    # The rest key on a user id alone, so they are checked through the users
    # that reference them — the same weaker check as policy above: once those
    # users are gone this reads zero regardless, and the sweep is what
    # guarantees the rows went.
    (
        "credit_requests",
        "SELECT count(*) FROM {schema}.credit_requests WHERE user_id IN "
        "(SELECT _id FROM {schema}.user_table WHERE email_id LIKE %(pattern)s)",
    ),
    (
        "credit_transactions",
        "SELECT count(*) FROM {schema}.credit_transactions WHERE user_id IN "
        "(SELECT _id FROM {schema}.user_table WHERE email_id LIKE %(pattern)s)",
    ),
    (
        "user_credits",
        "SELECT count(*) FROM {schema}.user_credits WHERE user_id IN "
        "(SELECT _id FROM {schema}.user_table WHERE email_id LIKE %(pattern)s)",
    ),
    (
        "kyc_transactions",
        "SELECT count(*) FROM {schema}.kyc_transactions WHERE user_id IN "
        "(SELECT _id FROM {schema}.user_table WHERE email_id LIKE %(pattern)s)",
    ),
]


def protected_accounts(ctx):
    """Accounts this harness must never delete, whatever a sweep turns up.

    Borrowed platform accounts — the OGC onboarding provider, an existing
    cos_admin — are real users with real history. Prefix matching already keeps
    them out of a sweep, but "it does not match the pattern" is a weak promise
    for an irreversible action, so they are named and checked explicitly.

    Returns (usernames, ids), both lowercased.
    """
    usernames, ids = set(), set()

    borrowed = ctx.config["ogc_vector"].get("provider_username")
    if borrowed:
        usernames.add(borrowed.lower())
    borrowed_id = getattr(ctx, "ogc_provider_sub", None)
    if borrowed_id:
        ids.add(str(borrowed_id).lower())

    existing_admin = ctx.config["cos_admin"].get("username")
    if existing_admin:
        usernames.add(existing_admin.lower())
    admin_id = getattr(ctx, "borrowed_cos_admin_id", None)
    if admin_id:
        # Excluded from user_ids, which is what the account delete and every
        # other user-keyed DELETE run on. The audit sweep is the one exception:
        # it keys on audit_user_ids, which adds this id back, because those rows
        # are the harness's own actions wearing an administrator's name.
        ids.add(str(admin_id).lower())

    return usernames, ids


def _is_protected(ctx, username=None, user_id=None):
    usernames, ids = protected_accounts(ctx)
    return (
        (username or "").lower() in usernames
        or str(user_id or "").lower() in ids
    )


def _resolve_user_ids(ctx, problems):
    """Every user id this teardown may delete rows for.

    Keycloak is the authority: a user exists there from the moment the harness
    creates it, whereas user_table is only written once an ACL route is hit. A
    run that fails in phase 01 or 02 has Keycloak accounts and database rows but
    no user_table row to find them by.

    This must run *before* any deletion. Both the Keycloak sweep and the DB
    sweep destroy the very records that make these ids discoverable, so
    resolving late would silently return an empty set — and an empty set makes
    every user-keyed DELETE a no-op rather than an error.

    Keycloak user ids are the database's user ids: UserAccessHandler.java:65
    stores `dxUser.sub()` as `user_table._id`.
    """
    ids = {str(u.user_id) for u in ctx.users.values() if u.user_id}
    try:
        for user in ctx.kc.find_users_by_prefix(ctx.config["run"]["prefix"]):
            if user.get("id"):
                ids.add(str(user["id"]))
    except Exception as err:  # noqa: BLE001 - fall back to what we already know
        problems.append(f"could not read user ids from Keycloak: {err}")

    # A borrowed account's rows are never swept, even if one of these lookups
    # somehow returned it: this set drives every user-keyed DELETE.
    _, protected_ids = protected_accounts(ctx)
    ids = {i for i in ids if i.lower() not in protected_ids}
    return sorted(ids)


def _cos_admin_ids(ctx):
    """The borrowed cos_admin, as the anchor list its audit statements key on.

    Empty when the harness created its own — that account is namespaced, sits in
    user_ids, and is swept by AUDIT_SWEEP_STATEMENTS like any other user. An
    empty list makes the cos_admin statements match nothing, so the created mode
    runs them as no-ops rather than special-casing them away.
    """
    borrowed = getattr(ctx, "borrowed_cos_admin_id", None)
    return [str(borrowed)] if borrowed else []


def teardown(ctx, stage="all"):
    """Remove everything this run created. Returns a list of problem strings.

    `stage` splits the work at the one point where order is not negotiable:

        data_plane  the broker, S3 and OGC objects — everything that is named
                    after the catalogue item and becomes unreachable once the
                    item is gone
        platform    the item itself, its policies, the consumers and the org —
                    everything that still needs an account to call an API as
        accounts    the Keycloak users and the database sweep — the point of no
                    return, after which nothing can be signed in as
        all         the three in that order — what `main/e2e.py` wants

    The split exists because a harness may have work of its own at each seam.
    The complete-test harness runs the collection's own `DELETE /cat/item`
    before `platform`, so that the published request is the one under test —
    and the data-plane objects have to be gone before that happens, or the file
    server, the STAC store and the OGC tables are all asked about a databank
    that no longer exists and every one of them fails.

    The second seam is there for the same kind of reason. Sweeping catalogue
    items means signing in as each namespaced account to ask what it owns, so
    it has to happen while those accounts still exist — which is to say before
    `accounts`, not after it.
    """
    problems = []
    config = ctx.config

    if not config["run"]["cleanup"]:
        print("    cleanup disabled — leaving artefacts in place")
        return problems

    if stage in ("all", "data_plane"):
        # Before the item: the exchange and queue are named with the item id,
        # and these objects are only reachable while something still names them.
        _teardown_ngsild(ctx, problems)
        _teardown_gateway(ctx, problems)
        _teardown_file(ctx, problems)
        _teardown_ogc_raster(ctx, problems)
        _teardown_ogc_vector(ctx, problems)
        _teardown_ogc_s3(ctx, problems)

    if stage in ("all", "platform"):
        # Ahead of every deletion, and stashed rather than passed: the consumer
        # self-delete below already cascades into Keycloak, so resolving this
        # any later loses the ids it removed. `accounts` reads what is stashed
        # here, which is what keeps the two stages equivalent to one call.
        ctx.swept_user_ids = _resolve_user_ids(ctx, problems)

        _teardown_policies(ctx, problems)
        _teardown_item(ctx, problems)
        # Before the self-delete: every endpoint below is read and called as the
        # account itself, and the balance is keyed on a user id the cascade
        # takes with it.
        _teardown_compute_credit(ctx, problems)
        _teardown_consumers(ctx, problems)
        _teardown_organisation(ctx, problems)

    if stage in ("all", "accounts"):
        # Only resolved here when `platform` was skipped — a caller running the
        # accounts stage alone, with nothing deleted yet for it to have missed.
        user_ids = getattr(ctx, "swept_user_ids", None)
        if user_ids is None:
            user_ids = _resolve_user_ids(ctx, problems)

        _teardown_keycloak_users(ctx, problems)

        if config["run"]["sweep_database"]:
            _sweep_database(ctx, problems, user_ids)

    return problems


def sweep_only(ctx):
    """Reap namespaced leftovers from earlier runs, without running the flow.

    Useful after a crash, or on a schedule to keep a shared deployment tidy.
    """
    problems = []
    # teardown() gets this from sign-in; --sweep-only never signs in, so without
    # it a borrowed cos_admin is neither protected by id nor reachable by the
    # audit sweep, and a crashed run's rows on that account would survive.
    _resolve_borrowed_cos_admin(ctx, problems)
    _resolve_borrowed_ogc_provider(ctx, problems)
    user_ids = _resolve_user_ids(ctx, problems)
    # Must precede _sweep_database, which deletes the request rows this reads.
    item_ids = _resolve_item_ids(ctx, problems)
    # Must precede the Keycloak sweep: catalogue items are found through their
    # owner's token, which stops existing the moment that user is deleted.
    handled = _sweep_catalogue_items(ctx, problems)
    # Same ordering, for a second reason: one of its anchors is the owner's
    # Keycloak id, which _teardown_keycloak_users is about to destroy.
    _sweep_ogc_collections(ctx, problems, user_ids, handled)
    # After the catalogue pass, which removes the indices it could reach through
    # an item. What is left here is what nothing else names.
    _sweep_elasticsearch_indices(ctx, problems, item_ids | handled)
    _teardown_keycloak_users(ctx, problems)
    if ctx.config["run"]["sweep_database"]:
        _sweep_database(ctx, problems, user_ids)
    return problems


def _resolve_borrowed_cos_admin(ctx, problems):
    """Fill in borrowed_cos_admin_id from the configured username, if unset.

    A no-op when no cos_admin is configured — the harness created its own, and
    it is swept like every other namespaced user.
    """
    if getattr(ctx, "borrowed_cos_admin_id", None):
        return
    username = ctx.config["cos_admin"].get("username")
    if not username:
        return
    try:
        found = ctx.kc.find_user(username)
    except Exception as err:  # noqa: BLE001 - the sweep still runs without it
        problems.append(f"could not resolve cos admin {username}: {err}")
        return
    if found:
        ctx.borrowed_cos_admin_id = found["id"]


def _resolve_item_ids(ctx, problems):
    """Every item id this run's prefix can still be tied to.

    `request` is the only table that keeps a durable, prefix-matchable record of
    an item id: `consumer_email_id` carries the namespaced email and `item_id`
    the item the access request was raised against. An item that has data has
    necessarily passed phase 03, so anything with an Elasticsearch index has a
    row here.

    This must be read before _sweep_database, which deletes those very rows.
    """
    ids = {str(lane.item_id) for lane in ctx.lanes if lane.item_id}
    if not ctx.config["postgres"]["enabled"]:
        return ids

    schema = ctx.config["postgres"]["schema"]
    pattern = f"{ctx.config['run']['prefix']}-%"
    try:
        connection = _connect(ctx.config)
    except Exception as err:  # noqa: BLE001 - the sweep degrades, it does not stop
        problems.append(f"could not read item ids from postgres: {err}")
        return ids

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT DISTINCT item_id FROM {schema}.request "
                "WHERE consumer_email_id LIKE %(pattern)s",
                {"pattern": pattern},
            )
            for (item_id,) in cursor.fetchall():
                if item_id:
                    ids.add(str(item_id))
    except Exception as err:  # noqa: BLE001
        problems.append(f"could not read item ids from {schema}.request: {err}")
    finally:
        connection.close()
    return ids


def _sweep_elasticsearch_indices(ctx, problems, item_ids=()):
    """Delete the NGSI-LD data indices of items this harness created.

    An index is named `<index_prefix><item id>` and carries nothing else — no
    prefix, no owner, no label. So unlike the catalogue, the OGC database or
    Keycloak, Elasticsearch offers the sweep no anchor of its own, and an index
    whose catalogue item is gone is invisible to every other pass: the NGSI-LD
    teardown is reached through the item, and once that is deleted nothing on
    the platform names the index.

    The anchor therefore has to come from outside: item ids resolved from
    `request` before the database sweep removes those rows. Matching an index
    only when its uuid is in that set is what makes this safe — an index this
    harness never created cannot be named by it, whatever else shares the
    deployment.
    """
    config = ctx.config["ngsild_delete"]
    if not config["enabled"]:
        return

    wanted = {str(item) for item in item_ids if item}
    if not wanted:
        print("    elasticsearch sweep: no item ids to match on, nothing to do")
        return

    try:
        indices = list_indices(ctx)
    except Exception as err:  # noqa: BLE001 - reported, never fatal
        problems.append(f"elasticsearch sweep skipped, could not list indices: {err}")
        return

    prefix = config["index_prefix"]
    orphans = [
        (name, docs)
        for name, docs in sorted(indices.items())
        if name.startswith(prefix) and name[len(prefix):] in wanted
    ]
    if not orphans:
        print(f"    elasticsearch sweep: no orphaned {prefix}* index")
        return

    print(f"    elasticsearch sweep: {len(orphans)} orphaned index/indices")
    for name, docs in orphans:
        if config.get("dry_run"):
            print(f"    DRY RUN — would delete {name} ({docs} doc(s))")
            continue
        try:
            delete_index(ctx, name)
        except Exception as err:  # noqa: BLE001
            problems.append(f"could not delete elasticsearch index {name}: {err}")
            continue
        print(f"    deleted elasticsearch index {name} ({docs} doc(s))")


def _resolve_borrowed_ogc_provider(ctx, problems):
    """Fill in ogc_provider_sub from the configured OGC provider, if unset.

    protected_accounts() checks that id, and a run fills it in as it goes.
    --sweep-only never runs the flow, so without this the OGC sweep would have
    nothing to recognise a borrowed provider's collections by.

    A no-op when none is configured — the harness onboards as its own throwaway
    provider, and everything that provider owns is the sweep's to remove.
    """
    if getattr(ctx, "ogc_provider_sub", None):
        return
    username = ctx.config["ogc_vector"].get("provider_username")
    if not username:
        return
    try:
        found = ctx.kc.find_user(username)
    except Exception as err:  # noqa: BLE001 - the sweep still runs without it
        problems.append(f"could not resolve ogc provider {username}: {err}")
        return
    if found:
        ctx.ogc_provider_sub = found["id"]


def _sweep_ogc_collections(ctx, problems, user_ids=(), handled=()):
    """Delete OGC collections left behind in the OGC server's own database.

    The catalogue pass can only reach a collection through the item that names
    it, read with that item owner's token. Anything that outlives its item — or
    whose owner cannot be signed in as, because a run used a different
    run.user_password — is invisible to it. The ControlPlane sweep is no help
    either: collections live in ogc_postgres, keyed by the item uuid, in tables
    that carry no namespaced column for the prefix to match.

    So this is the OGC database's own sweep, anchored on what find_collections
    documents. It runs only under --sweep-only, deliberately. A normal teardown
    already removes its own lanes' collections by id, and it is the only thing
    that knows a failed onboarding is being held for inspection
    (ogc_vector_delete.keep_on_failure) — a prefix-wide pass there would delete
    the very wreckage that flag preserves.
    """
    if not ctx.config["ogc_vector_delete"]["enabled"]:
        return

    prefix = ctx.config["run"]["prefix"]
    try:
        rows = find_collections(
            ctx,
            f"{prefix}-%",
            user_ids=user_ids,
            # The label/description pairs this harness configures, matched
            # exactly. These are what reach collections onboarded before the
            # title carried the namespace; see find_collections on why both
            # halves have to match.
            labels=(
                (
                    ctx.config["ogc_vector"].get("label"),
                    ctx.config["ogc_vector"].get("description"),
                ),
                (
                    ctx.config["ogc_raster"].get("title"),
                    ctx.config["ogc_raster"].get("description"),
                ),
            ),
        )
    except Exception as err:  # noqa: BLE001 - one unreachable database is not fatal
        problems.append(f"ogc collection sweep skipped, could not read: {err}")
        return

    done = {str(item) for item in handled}
    pending = [row for row in rows if str(row[0]) not in done]
    if not pending:
        print("    ogc collection sweep: nothing orphaned")
        return

    print(f"    ogc collection sweep: {len(pending)} orphaned collection(s)")
    for collection_id, title, owner_id, has_stac in pending:
        if owner_id and _is_protected(ctx, user_id=owner_id):
            print(f"    protected owner, ogc collection left alone: {collection_id}")
            continue
        kind = RASTER if has_stac else VECTOR
        print(f"    orphan ogc collection {collection_id} ({title}) — {kind}")
        if ctx.config["ogc_s3_cleanup"]["enabled"]:
            _run_ogc_s3_teardown(ctx, collection_id, kind, problems)
        # Removes the STAC children first, so a raster collection whose items
        # were never deleted through the API comes out too.
        _run_ogc_vector_teardown(ctx, collection_id, None, problems)


def _sweep_catalogue_items(ctx, problems):
    """Delete catalogue items left behind by earlier runs.

    Items live in Elasticsearch, so the database sweep cannot reach them — a
    run that ends without teardown leaves one on the deployment permanently.

    There is no admin-wide "find items by name" endpoint available to a
    provider, so items are discovered per owner via /cat/search/myassets, which
    needs that owner's token. Every user the harness creates shares
    run.user_password, so a token can be minted for each namespaced account
    found in Keycloak.

    Returns the item ids it dealt with, so the OGC sweep that follows does not
    go over the same collections a second time.
    """
    prefix = ctx.config["run"]["prefix"]
    password = ctx.config["run"]["user_password"]
    # A run whose password one of its own accounts had changed under it holds
    # the working one on the user rather than in the config — see
    # CompleteTestContext._recover_password. Preferring it keeps the sweep able
    # to sign in as that account; the configured password still covers every
    # account left behind by an earlier run.
    known = {
        u.username.lower(): u.password
        for u in ctx.users.values()
        if getattr(u, "username", None) and getattr(u, "password", None)
    }

    try:
        users = ctx.kc.find_users_by_prefix(prefix)
    except Exception as err:  # noqa: BLE001
        problems.append(f"could not list Keycloak users for catalogue sweep: {err}")
        return set()

    handled = set()
    for user in users:
        username = user.get("username", "")
        if _is_protected(ctx, username, user.get("id")):
            print(f"    protected account, assets left alone: {username}")
            continue
        try:
            token = ctx.kc.user_token(username, known.get(username.lower(), password))
        except Exception:  # noqa: BLE001
            # The password is not the one this harness set. Either an older run
            # used a different one, or a collection under test changed it —
            # PUT /auth/user/password is one of its own test cases.
            #
            # Only a provider can own a catalogue item, so for anyone else there
            # is provably nothing here to find and the failed sign-in is not
            # worth reporting as a problem. The Keycloak sweep removes the
            # account either way.
            if not _may_own_items(ctx, user):
                print(f"    cannot sign in as {username}; it owns no assets, so nothing to sweep")
                continue
            problems.append(
                f"could not sign in as {username} to find its assets — it is a "
                f"provider, so any item it owns is left behind"
            )
            continue

        try:
            payload = ctx.cp.get(
                "/iudx/v2/cat/search/myassets",
                f"list assets of {username}",
                token=token,
                params={"page": 1, "size": 100},
            )
        except Exception as err:  # noqa: BLE001
            problems.append(f"could not list assets for {username}: {err}")
            continue

        for row in rows_of(payload):
            item_id = field(row, "id", "itemId")
            name = str(field(row, "name", default=""))
            if not item_id or not name.startswith(prefix):
                continue
            # Broker objects first: once the item is gone nothing names them.
            # The owner's token goes with them: teardowns that call an API have
            # to do it as the account that owns the item, and in a sweep this
            # run's own provider does not exist to borrow one from.
            _sweep_broker_objects(
                ctx, row, item_id, user.get("id"), problems, owner_token=token
            )
            _delete_item_with_policies(ctx, token, item_id, name, problems)
            handled.add(str(item_id))

    return handled


def _may_own_items(ctx, user):
    """Whether an account could own a catalogue item at all.

    Items are created by providers, so an account without the provider role has
    none — which turns "could not sign in to list its assets" from a gap in the
    sweep into a step that had nothing to do.
    """
    try:
        return ctx.config["keycloak"]["roles"]["provider"] in ctx.kc.realm_roles(user["id"])
    except Exception:  # noqa: BLE001 - unknown means assume it might
        return True


def _delete_item_with_policies(ctx, token, item_id, name, problems):
    """Remove an item's policies, then the item.

    handleDeleteItem refuses with 409 while any active policy exists
    (ItemController.java:813), so the order is mandatory.
    """
    try:
        payload = ctx.acl.get(
            "/iudx/acl/apd/v2/policy/provider",
            f"policies on {name}",
            token=token,
        )
        for row in rows_of(payload):
            if str(field(row, "itemId")) != str(item_id):
                continue
            policy_id = field(row, "policyId", "id", "_id")
            if policy_id:
                try_delete(
                    lambda pid=policy_id: ctx.acl.put(
                        "/iudx/acl/apd/v2/policy",
                        f"delete policy {pid}",
                        token=token,
                        params={"id": pid},
                    ),
                    f"delete policy {policy_id} on {name}",
                    problems,
                )
    except Exception as err:  # noqa: BLE001
        problems.append(f"could not list policies on {name}: {err}")

    if try_delete(
        lambda: ctx.cp.delete(
            "/iudx/v2/cat/item", f"delete item {name}", token=token,
            params={"id": item_id},
        ),
        f"delete item {name}",
        problems,
    ):
        print(f"    deleted catalogue item {name}")


def _policy_ids_for_item(ctx, lane, problems):
    """Every policy on one lane's item, asked for at teardown time.

    lane.policy_ids is only filled at the very end of phase 03, so a run that
    died between granting the access request and reading the policy list would
    have an empty set here — and the item delete would then 409. Re-querying
    makes teardown depend on the platform's state rather than on how far the
    flow happened to get.
    """
    ids = list(lane.policy_ids)
    if not lane.item_id:
        return ids
    try:
        payload = ctx.acl.get(
            "/iudx/acl/apd/v2/policy/provider",
            f"list policies for teardown{lane.suffix}",
            token=lane.provider.token,
        )
        for row in rows_of(payload):
            if str(field(row, "itemId")) != str(lane.item_id):
                continue
            pid = field(row, "policyId", "id", "_id")
            if pid and pid not in ids:
                ids.append(pid)
    except Exception as err:  # noqa: BLE001 - fall back to what the flow recorded
        problems.append(f"could not list policies for teardown: {err}")
    return ids


def _teardown_policies(ctx, problems):
    """Delete every policy on the item, before the item itself.

    This ordering is mandatory, not cosmetic: handleDeleteItem checks
    hasActivePolicies and fails with 409 "Item cannot be deleted as it has
    active policies" (ItemController.java:813).

    The API only soft-deletes — the row stays with status DELETE for
    traceability — so the database sweep is what finally removes it.

    A policy that is already inactive is not a problem to report. The
    complete-test harness gives the collection's own deactivate request the
    first go, so by the time this runs the policy may already be down, and the
    API answers that with 400 "policy is not ACTIVE". The outcome wanted here is
    an inactive policy, and it is inactive.
    """
    for lane in ctx.lanes:
        for policy_id in _policy_ids_for_item(ctx, lane, problems):
            already_down = []
            try_delete(
                lambda pid=policy_id, owner=lane.provider, seen=already_down: (
                    _deactivate_policy(ctx, pid, owner, seen)
                ),
                f"delete policy {policy_id}",
                problems,
            )


def _deactivate_policy(ctx, policy_id, owner, seen):
    """Deactivate one policy, treating "already inactive" as done."""
    try:
        ctx.acl.put(
            "/iudx/acl/apd/v2/policy",
            f"delete policy {policy_id}",
            token=owner.token,
            params={"id": policy_id},
            expect=(200,),
        )
    except Exception as err:  # noqa: BLE001 - inspected, then re-raised
        if "not ACTIVE" in str(err):
            seen.append(policy_id)
            print(f"    policy {policy_id} was already deactivated")
            return
        raise


def _teardown_ngsild(ctx, problems):
    """Remove the item's Elasticsearch index, exchange and broker user.

    Runs first, while the catalogue item still exists: the exchange is named
    with the item id, and once the item is gone nothing on the platform names
    these objects — they become leftovers no sweep can find.

    Skipped unless the item is an NGSI-LD one. Only those get an exchange, so
    for anything else there is nothing here to delete and running the script
    would report three objects missing as a failure.
    """
    if not ctx.config["ngsild_delete"]["enabled"]:
        return
    for lane in ctx.lanes:
        _teardown_ngsild_lane(ctx, lane, problems)


def _teardown_ngsild_lane(ctx, lane, problems):
    """The NGSI-LD objects of one lane's item, deleted as that lane's provider.

    The broker user is the provider's, so each lane deletes its own — which is
    the whole reason the gateway item gets a provider of its own.
    """
    config = ctx.config["ngsild_delete"]
    if not lane.item_id:
        return

    declared, source = item_declares_ngsild(ctx, lane)
    if not declared:
        print(
            f"    ngsi-ld teardown: skipped, {lane.item_name} is not an "
            f"NGSI-LD item per {source}"
        )
        return

    # The provider's broker user, created by the catalogue and named with their
    # Keycloak id. Without it the script has nothing to delete in step 3 and
    # refuses to start, so an unresolved id is reported rather than guessed.
    broker_user = config["delete_user"] or lane.provider.user_id
    if not broker_user:
        problems.append(
            "ngsi-ld teardown skipped: no broker user to delete — the run never "
            f"resolved {lane.provider.key}'s Keycloak id (set "
            "ngsild_delete.delete_user to name one explicitly)"
        )
        return

    _run_ngsild_teardown(ctx, lane.item_id, broker_user, problems)


def _teardown_file(ctx, problems):
    """Delete the uploaded objects, before the databank item that holds them."""
    if not ctx.config["file_delete"]["enabled"]:
        return
    for lane in ctx.lanes:
        _teardown_file_lane(ctx, lane, problems)


def _teardown_file_lane(ctx, lane, problems):
    if not lane.item_id:
        return

    declared, source = item_declares_file(ctx, lane)
    if not declared:
        print(
            f"    file teardown: skipped, {lane.item_name} is not a file item "
            f"per {source}"
        )
        return

    keys = list(getattr(ctx, "file_keys", None) or [])
    if not keys:
        # Nothing was uploaded — a run that failed before phase 04, or an
        # upload that never succeeded.
        key = ctx.config["file_upload"].get("key")
        if not key:
            path = ctx.config["file_upload"].get("file_path")
            key = os.path.basename(path) if path else None
        keys = [key] if key else []
    if not keys:
        return

    token = _provider_token(ctx, problems, lane)
    if token:
        _run_file_teardown(ctx, lane.item_id, token, keys, problems)


def _run_file_teardown(ctx, item_id, token, keys, problems):
    """Delete one databank's uploaded objects."""
    print(f"    file teardown: {', '.join(keys)} from databank {item_id}")
    result = file_delete(ctx, item_id, token, keys)
    if not result.ok:
        problems.append(
            f"file teardown exited {result.code} ({result.meaning}) for "
            f"databank {item_id} — some objects may still exist"
        )


def _teardown_ogc_s3(ctx, problems):
    """Remove the item's files from the bucket, before the item names nothing.

    A vector item leaves `<item>.gpkg`; a raster one leaves an `<item>/` folder
    of GeoTIFFs. Which of the two is decided the same way the onboarding was.
    """
    if not ctx.config["ogc_s3_cleanup"]["enabled"]:
        return
    for lane in ctx.lanes:
        _teardown_ogc_s3_lane(ctx, lane, problems)


def _teardown_ogc_s3_lane(ctx, lane, problems):
    if not lane.item_id:
        return

    if item_declares_ogc_raster(ctx, lane)[0]:
        kind = RASTER
    elif item_declares_ogc_vector(ctx, lane)[0]:
        kind = VECTOR
    else:
        return

    _run_ogc_s3_teardown(ctx, lane.item_id, kind, problems)


def _run_ogc_s3_teardown(ctx, item_id, kind, problems):
    """Delete one item's objects from the bucket."""
    print(f"    s3 cleanup: {kind} objects for {item_id}")
    result = ogc_s3_delete(ctx, item_id, kind)
    if result.ok:
        removed = deleted_count(result.output)
        print(f"    s3 cleanup: {removed} object(s) removed")
    else:
        problems.append(
            f"s3 cleanup exited {result.code} ({result.meaning}) for {item_id} — "
            f"files may still be in the bucket"
        )


def _teardown_ogc_raster(ctx, problems):
    """Delete the STAC items, before the catalogue item that names them.

    Only the items: the collection's own rows are keyed by the same id and go
    with the ogc_vector_delete pass that follows.
    """
    if not ctx.config["ogc_raster_delete"]["enabled"]:
        return
    for lane in ctx.lanes:
        _teardown_ogc_raster_lane(ctx, lane, problems)


def _teardown_ogc_raster_lane(ctx, lane, problems):
    if not lane.item_id:
        return

    declared, source = item_declares_ogc_raster(ctx, lane)
    if not declared:
        print(
            f"    stac teardown: skipped, {lane.item_name} is not an OGC raster "
            f"item per {source}"
        )
        return

    token = _provider_token(ctx, problems, lane)
    if not token:
        return

    _run_ogc_raster_teardown(ctx, lane.item_id, token, problems)


def _provider_token(ctx, problems, lane=None):
    """A fresh token for one lane's provider, for teardown calls that need one."""
    requester = (lane or ctx.primary).provider
    try:
        return ctx.kc.user_token(requester.username, requester.password)
    except Exception:  # noqa: BLE001 - the stored one is usually still valid
        if requester.token:
            return requester.token
        problems.append(
            f"stac teardown skipped: could not mint a token for {requester.key}"
        )
        return None


def _run_ogc_raster_teardown(ctx, item_id, token, problems):
    """Delete one collection's STAC items."""
    print(f"    stac teardown: items of collection {item_id}")
    result = ogc_raster_delete(ctx, item_id, token)
    if not result.ok:
        problems.append(
            f"stac teardown exited {result.code} ({result.meaning}) for "
            f"collection {item_id} — some items may still exist"
        )


def _teardown_ogc_vector(ctx, problems):
    """Remove the OGC collection's rows and its table.

    Runs before the item, like the other two: the collection is keyed by the
    item id, and the deletion drops a table of that name.
    """
    if not ctx.config["ogc_vector_delete"]["enabled"]:
        return
    for lane in ctx.lanes:
        _teardown_ogc_vector_lane(ctx, lane, problems)


def _teardown_ogc_vector_lane(ctx, lane, problems):
    config = ctx.config["ogc_vector_delete"]
    if not lane.item_id:
        return

    declared, source = item_declares_ogc_vector(ctx, lane)
    if not declared:
        # A raster collection's rows live in the same tables, keyed by the same
        # id, so this pass cleans up after either kind.
        declared, source = item_declares_ogc_raster(ctx, lane)
    if not declared:
        print(
            f"    ogc vector teardown: skipped, {lane.item_name} is not an OGC "
            f"item per {source}"
        )
        return

    if getattr(ctx, "ogc_onboarding_failed", False) and config["keep_on_failure"]:
        ogc_db = ctx.config["ogc_postgres"]
        print(
            f"    ogc vector teardown: HELD — onboarding failed, so what it "
            f"managed to create is left for inspection\n"
            f"      psql {ogc_db['database']}: "
            f"select * from collections_details where id = '{lane.item_id}';\n"
            f"      psql {ogc_db['database']}: "
            f"select to_regclass('\"{lane.item_id}\"');   -- the ogr2ogr table\n"
            f"      remove it with: --set ogc_vector_delete.keep_on_failure=false"
        )
        # The collection's rows are held, but a roles row this run added is not
        # evidence of anything — ri_details rolled back with the transaction —
        # and leaving it behind litters a shared database.
        _remove_added_role(ctx, problems)
        return

    _run_ogc_vector_teardown(ctx, lane.item_id, None, problems)

    # After the collection's rows: ri_details references this row.
    _remove_added_role(ctx, problems)


def _remove_added_role(ctx, problems):
    """Remove the OGC roles row this run added, if it added one."""
    user_id = getattr(ctx, "ogc_role_user", None)
    if not user_id or _is_protected(ctx, user_id=user_id):
        return
    try:
        removed = remove_provider_role(ctx, user_id)
        print(f"    ogc vector teardown: removed {removed} roles row(s) for {user_id}")
    except Exception as err:  # noqa: BLE001 - reported, never fatal
        problems.append(f"could not remove the OGC roles row for {user_id}: {err}")


def _run_ogc_vector_teardown(ctx, item_id, _broker_user, problems):
    """Delete one collection. The broker-user argument is unused — this one is
    database-only — and is taken so the sweep can call all three alike."""
    print(f"    ogc vector teardown: collection {item_id}")
    result = ogc_vector_delete(ctx, item_id)
    if not result.ok:
        problems.append(
            f"ogc vector teardown exited {result.code} ({result.meaning}) for "
            f"collection {item_id} — its rows or table may still exist"
        )


def _teardown_gateway(ctx, problems):
    """Remove the item's queue and the provider's broker user.

    The gateway equivalent of _teardown_ngsild, and skipped the same way unless
    the item is a gateway one — a queue named with the item id exists only for
    those.
    """
    if not ctx.config["gateway_delete"]["enabled"]:
        return
    for lane in ctx.lanes:
        _teardown_gateway_lane(ctx, lane, problems)


def _teardown_gateway_lane(ctx, lane, problems):
    """One lane's queue and its own provider's broker user.

    When the run split the gateway into its own lane this user is nobody else's:
    the NGSI-LD teardown deleted the other provider's, and neither is left
    deleting a user the other already removed.
    """
    config = ctx.config["gateway_delete"]
    if not lane.item_id:
        return

    declared, source = item_declares_gateway(ctx, lane)
    if not declared:
        print(
            f"    gateway teardown: skipped, {lane.item_name} is not a gateway "
            f"item per {source}"
        )
        return

    queue = config["queue_name"] or lane.item_id
    broker_user = config["delete_user"] or lane.provider.user_id
    if not broker_user:
        problems.append(
            "gateway teardown skipped: no broker user to delete — the run never "
            f"resolved {lane.provider.key}'s Keycloak id (set "
            "gateway_delete.delete_user to name one explicitly)"
        )
        return

    _run_gateway_teardown(ctx, queue, broker_user, problems)


def _run_gateway_teardown(ctx, queue, broker_user, problems):
    """Delete one item's queue and broker user."""
    print(f"    gateway teardown: queue {queue}, broker user {broker_user}")
    result = gateway_delete(ctx, queue, broker_user)
    if not result.ok:
        problems.append(
            f"gateway teardown exited {result.code} ({result.meaning}) for "
            f"queue {queue} — the queue or broker user may still exist"
        )


def _run_ngsild_teardown(ctx, item_id, broker_user, problems):
    """Delete one item's index, exchange and broker user.

    Retried once, because the failure this actually sees is a management API
    call that hung until its timeout — thirty seconds against a broker that
    answers the same request in a fraction of a second when asked again. The
    script is safe to repeat: an object that is already gone is not a failure to
    it, so a second pass either finishes the job or confirms the first one did.
    """
    print(f"    ngsi-ld teardown: exchange {item_id}, broker user {broker_user}")
    result = ngsild_delete(ctx, item_id, broker_user)
    if not result.ok:
        print("    ngsi-ld teardown did not complete; trying once more")
        result = ngsild_delete(ctx, item_id, broker_user)
    if not result.ok:
        problems.append(
            f"ngsi-ld teardown exited {result.code} ({result.meaning}) for "
            f"exchange {item_id} — the index, exchange or broker user may "
            f"still exist"
        )


def _row_declares(ctx, row, matches, config_server):
    """Whether a swept item has a server of some kind.

    Same evidence rule as a live run: the entries the search returned, falling
    back to config when the projection does not carry resourceServer.
    """
    entries = declared_servers(row)
    if entries is None:
        return config_server(ctx.config) is not None
    return any(matches(entry) for entry in entries)


def _sweep_broker_objects(ctx, row, item_id, broker_user, problems, owner_token=None):
    """Tear down the broker objects of an orphaned item, before deleting it.

    Runs before the item is deleted, for the same reason as during a normal
    teardown: the exchange and queue are named with the item id. The owner is
    known here — the sweep found the item through that user's own token — so the
    broker user needs no separate lookup.

    owner_token is that same token. The teardowns that call an API need one, and
    falling back to _provider_token would ask Keycloak for this run's provider —
    an account --sweep-only never created, so every such teardown would be
    skipped with a problem while the orphan's own token sat unused.
    """
    kinds = []
    if ctx.config["ngsild_delete"]["enabled"] and _row_declares(
        ctx, row, lambda e: e["type"] in NGSILD_TYPES, ngsild_server
    ):
        kinds.append(("ngsi-ld", _run_ngsild_teardown))
    if ctx.config["gateway_delete"]["enabled"] and _row_declares(
        ctx, row, lambda e: e["type"] in GATEWAY_TYPES, gateway_server
    ):
        kinds.append(("gateway", _run_gateway_teardown))

    if ctx.config["file_delete"]["enabled"] and _row_declares(
        ctx, row, is_file_server, file_server
    ):
        token = owner_token or _provider_token(ctx, problems)
        key = ctx.config["file_upload"].get("key") or os.path.basename(
            ctx.config["file_upload"].get("file_path") or ""
        )
        if token and key:
            _run_file_teardown(ctx, item_id, token, [key], problems)

    if ctx.config["ogc_raster_delete"]["enabled"] and _row_declares(
        ctx, row, is_ogc_raster, ogc_raster_server
    ):
        token = owner_token or _provider_token(ctx, problems)
        if token:
            _run_ogc_raster_teardown(ctx, item_id, token, problems)

    if ctx.config["ogc_s3_cleanup"]["enabled"]:
        if _row_declares(ctx, row, is_ogc_raster, ogc_raster_server):
            _run_ogc_s3_teardown(ctx, item_id, RASTER, problems)
        elif _row_declares(ctx, row, is_ogc_vector, ogc_vector_server):
            _run_ogc_s3_teardown(ctx, item_id, VECTOR, problems)

    if ctx.config["ogc_vector_delete"]["enabled"] and (
        _row_declares(ctx, row, is_ogc_vector, ogc_vector_server)
        or _row_declares(ctx, row, is_ogc_raster, ogc_raster_server)
    ):
        # Database-only, so it needs no broker user and runs even when the
        # owner's Keycloak id could not be resolved.
        _run_ogc_vector_teardown(ctx, item_id, None, problems)

    if not kinds:
        return

    if not broker_user:
        names = " and ".join(kind for kind, _ in kinds)
        problems.append(
            f"{names} teardown skipped for {item_id}: its owner has no Keycloak id"
        )
        return

    for _, teardown_one in kinds:
        teardown_one(ctx, item_id, broker_user, problems)


def _teardown_item(ctx, problems):
    """Delete each lane's catalogue item.

    404 counts as success. A collection may have deleted the item already — the
    complete-test harness gives the collection's own DELETE the first go, since
    running the published request is worth more than repeating it by hand — and
    an item that is gone is the outcome this wanted either way. Treating it as a
    failure would report a problem for work that was done correctly.
    """
    for lane in ctx.lanes:
        if not lane.item_id:
            continue
        try_delete(
            lambda l=lane: ctx.cp.delete(
                "/iudx/v2/cat/item",
                f"delete catalogue item{l.suffix}",
                token=l.provider.token,
                params={"id": l.item_id},
                expect=(200, 404),
            ),
            f"delete item {lane.item_id}",
            problems,
        )


def _teardown_compute_credit(ctx, problems):
    """Unwind the credit and compute state the 05/06 folders leave on a consumer.

    The collection's own teardown folders delete a credit or compute request by
    the id the flow captured, and they are enough while that request is still
    pending. This covers the three things they cannot do:

      * the **balance**. `PUT /admin/user/credit/add` has an exact inverse in
        `/deduct` — same body, same cos_admin token — and nothing else on the
        platform will bring a balance back to zero.
      * a request the flow **approved**. `DELETE /user/credit/request/{id}`
        answers 400 "Only pending credit requests can be deleted", so the
        granted ones the 05/06 folders created are still there.
      * anything raised **outside** the ids the flow captured — a second credit
        request, or a run that died before `capture` ran.

    None of it is recorded as a teardown problem: a granted request refusing to
    delete is the platform behaving as documented, and the rows go with the
    database sweep either way. What the run does print is the `compute_role`
    row, because that one outlives the account in a way that matters —
    `compute_role.user_id` is UNIQUE, so a surviving row means that account can
    never request the compute role again.
    """
    if not ctx.config["run"].get("unwind_compute_credit", True):
        return

    for key in ("consumer", "nopolicy"):
        user = ctx.user(key)
        if not user.token:
            continue
        _zero_credit_balance(ctx, user, problems)
        _delete_own_requests(
            ctx, user, problems, "credit request", "/iudx/v2/auth/user/credit/request"
        )
        _delete_own_requests(
            ctx, user, problems, "compute request", "/iudx/v2/auth/user/compute/requests"
        )


def _credit_balance(ctx, user):
    """The account's own balance, or None when the endpoint is not there."""
    payload = ctx.cp.get(
        "/iudx/v2/auth/user/credit/balance", f"credit balance of {user.key}",
        token=user.token, expect=(200, 403, 404),
    )
    balance = payload.get("balance") if isinstance(payload, dict) else None
    return float(balance) if isinstance(balance, (int, float)) else None


def _zero_credit_balance(ctx, user, problems):
    """Deduct the whole balance as the cos_admin.

    `requested_at` is the platform's idempotency key — the same
    (user, amount, requested_at) twice answers 409 Duplicate transaction
    request — so a retry uses a later second rather than repeating the call.
    """
    try:
        balance = _credit_balance(ctx, user)
    except Exception as err:  # noqa: BLE001 - teardown never propagates
        problems.append(f"read credit balance of {user.username}: {err}")
        return
    if not balance or balance <= 0:
        return
    if not ctx.cos_admin_token or not user.user_id:
        print(f"    credit balance of {user.key} is {balance} — no cos_admin to deduct it")
        return

    for attempt in range(2):
        moment = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=attempt)
        body = {
            "user_id": user.user_id,
            "amount": balance,
            "requested_at": moment.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        try:
            result = ctx.cp.put(
                "/iudx/v2/auth/admin/user/credit/deduct", f"deduct {balance} from {user.key}",
                token=ctx.cos_admin_token, json_body=body, expect=(200, 409),
            )
        except Exception as err:  # noqa: BLE001 - teardown never propagates
            problems.append(f"deduct {balance} from {user.username}: {err}")
            return
        updated = result.get("updatedBalance") if isinstance(result, dict) else None
        if updated is not None:
            print(f"    deducted {balance} from {user.key} (balance now {updated})")
            return
        # 409: the platform already has this (user, amount, requested_at). The
        # next second is a different transaction, so the retry is the fix.
    print(f"    could not deduct {balance} from {user.key}: duplicate transaction")


def _delete_own_requests(ctx, user, problems, label, list_path):
    """Delete every credit/compute request the account owns that will delete.

    The listing is the account's own, so it needs no admin rights and it finds
    what the flow never captured. A 400 is the platform refusing to delete
    anything but a pending request — printed, not recorded as a problem.
    """
    try:
        payload = ctx.cp.get(
            list_path, f"own {label}s of {user.key}", token=user.token,
            params={"page": 1, "size": 50, "sort": "createdAt", "status": ""},
            expect=(200, 403, 404),
        )
    except Exception as err:  # noqa: BLE001 - teardown never propagates
        problems.append(f"list {label}s of {user.username}: {err}")
        return

    # The compute endpoint answers with one object — compute_role.user_id is
    # UNIQUE — where the credit endpoint answers with a list.
    rows = rows_of(payload)
    if not rows and isinstance(payload, dict) and payload.get("id"):
        rows = [payload]

    for row in rows:
        request_id = row.get("id")
        status = row.get("status", "?")
        if not request_id:
            continue
        try:
            response = ctx.cp.delete(
                f"{list_path}/{quote(str(request_id))}",
                f"delete {label} {request_id}", token=user.token,
                expect=(200, 204, 400, 403, 404),
            )
        except Exception as err:  # noqa: BLE001 - teardown never propagates
            problems.append(f"delete {label} {request_id}: {err}")
            continue
        # A refusal keeps its envelope, so it still carries a title; a success
        # is unwrapped to its result, or says "Success" when it has none.
        title = response.get("title") if isinstance(response, dict) else None
        if title and title != "Success":
            detail = response.get("detail") or title
            print(f"    {label} {request_id} ({status}) not deleted — {detail}; "
                  f"the row goes with the database sweep")
        else:
            print(f"    deleted {label} {request_id} ({status})")


def _teardown_consumers(ctx, problems):
    """Consumers can remove themselves, which cascades into Keycloak and the DB."""
    for key in ("consumer", "nopolicy"):
        user = ctx.user(key)
        if not user.token:
            continue
        try_delete(
            lambda u=user: ctx.cp.delete(
                "/iudx/v2/auth/user/delete", f"self-delete {u.key}", token=u.token
            ),
            f"self-delete {user.username}",
            problems,
        )


def _teardown_organisation(ctx, problems):
    """Remove each lane's organisation, where the platform allows it.

    DELETE /organisations/{id} cannot succeed for an approved org. It is a bare
    orgDAO.delete with no cascade, so it hits a foreign-key violation while any
    organization_users row references the org — and the org admin's row is
    unremovable: deleteOrganisationUserById rejects it with "Cannot delete admin
    user", and deleteDxUser rejects org admins (AdminHandler.java:417).

    So when the database sweep is running it owns org deletion, and calling the
    API first would only produce a guaranteed 500. Without the sweep the call is
    still attempted, because a failure there is worth reporting: it means the
    organisation is genuinely stuck on the deployment.
    """
    if ctx.config["run"]["sweep_database"]:
        return

    for lane in ctx.lanes:
        if not lane.org_id:
            continue
        try_delete(
            lambda l=lane: ctx.cp.delete(
                f"/iudx/v2/auth/organisations/{l.org_id}",
                f"delete organisation{l.suffix}",
                token=l.provider.token,
            ),
            f"delete organisation {lane.org_id} (needs run.sweep_database — the "
            "API cannot remove an org whose admin still exists)",
            problems,
        )


def _teardown_keycloak_users(ctx, problems):
    """Sweep every namespaced user, plus orphans from earlier crashed runs.

    The requester is an org admin by this point, and /auth/user/delete refuses
    org admins (AdminHandler.java:417), so the Admin API is the only way out.
    """
    prefix = ctx.config["run"]["prefix"]
    try:
        leftovers = ctx.kc.find_users_by_prefix(prefix)
    except Exception as err:  # noqa: BLE001
        problems.append(f"could not list Keycloak users for sweep: {err}")
        return

    cutoff_ms = _orphan_cutoff_ms(ctx)
    for user in leftovers:
        username = user.get("username", "")
        if _is_protected(ctx, username, user.get("id")):
            print(f"    protected account, never deleted: {username}")
            continue
        mine = username.startswith(ctx.namespace)
        created = user.get("createdTimestamp") or 0
        if not mine and created > cutoff_ms:
            continue  # another run's, and still recent — leave it alone
        try_delete(
            lambda uid=user["id"], name=username: ctx.kc.delete_user(uid, name),
            f"delete keycloak user {username}",
            problems,
        )


def _orphan_cutoff_ms(ctx):
    import time

    hours = ctx.config["run"]["sweep_older_than_hours"]
    return int((time.time() - hours * 3600) * 1000)


def _resolve_anchors(cursor, schema, pattern, known):
    """Collect the user, org and item ids this sweep is allowed to touch.

    Resolved up front because the deletes cascade through each other: once
    user_table rows are gone, nothing keyed on user_id is reachable any more.

    The ids this run captured at runtime are merged in on top of what the
    prefix query finds. That matters for tables with no namespaced column of
    their own — client_credentials is keyed only on user_id, so once the
    self-delete API has removed the user's user_table row, the prefix alone
    can no longer reach it.
    """
    anchors = {"pattern": pattern}
    for key, template in ANCHOR_QUERIES.items():
        try:
            cursor.execute(template.format(schema=schema), {"pattern": pattern})
            anchors[key] = [str(row[0]) for row in cursor.fetchall()]
        except Exception:  # noqa: BLE001 - a missing table must not stop the sweep
            cursor.connection.rollback()
            anchors[key] = []

    for key, extra in known.items():
        for value in extra:
            if value and str(value) not in anchors[key]:
                anchors[key].append(str(value))
    return anchors


# Deployments drift. Staging, dev and a fresh stack do not carry the same
# tables or even the same column names — user_activity_audit_log names the
# delegate `delegate_id` on one and `delegator_id` on another, and a table like
# leaderboard_dirty_queue exists on dev but not in v2.3. A sweep that assumes
# one shape fails statements on the other and reports the run as broken.
#
# So the sweep reads the live schema first and adapts each statement to it:
# a statement naming a missing table is skipped, and a term naming a missing
# column is dropped from its OR group. Both directions only ever delete less.
# If a whole group would empty out the statement is skipped entirely rather
# than run without it — dropping an anchor group is what would turn a scoped
# delete into an unscoped one, and that must never happen silently.

_COLUMN_REF = re.compile(r"(\w+)\s*(?:=|LIKE)\s*(?:ANY\s*\()?\s*%\(")


def _split_top_level(clause, separator):
    """Split on `separator` only where parentheses are balanced."""
    parts, depth, current, i = [], 0, [], 0
    width = len(separator)
    while i < len(clause):
        char = clause[i]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if depth == 0 and clause[i:i + width].upper() == separator.upper():
            parts.append("".join(current))
            current = []
            i += width
            continue
        current.append(char)
        i += 1
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _wraps_all(clause):
    """True when a leading '(' is the one closed by the trailing ')'."""
    if not (clause.startswith("(") and clause.endswith(")")):
        return False
    depth = 0
    for i, char in enumerate(clause):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return i == len(clause) - 1
    return False


def render_sql(cursor, sql, params):
    """The statement as the server will see it, values bound — for the log.

    psycopg2's mogrify does the substitution client-side without running
    anything; on any other driver the template is returned as written.
    """
    try:
        rendered = cursor.mogrify(sql, params)
    except Exception:  # noqa: BLE001 - logging must never break the sweep
        return sql
    return rendered.decode() if isinstance(rendered, bytes) else str(rendered)


def _schema_columns(cursor, schema):
    """{table: {column, ...}} for one schema, or None if it cannot be read."""
    try:
        cursor.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = %(schema)s",
            {"schema": schema},
        )
    except Exception:  # noqa: BLE001 - without this the sweep just runs unguarded
        cursor.connection.rollback()
        return None
    columns = {}
    for table, column in cursor.fetchall():
        columns.setdefault(table, set()).add(column)
    return columns


def _adapt_to_schema(sql, schema, columns):
    """Rewrite one DELETE for this deployment, or None to skip it.

    Returns (sql, dropped_columns). Only ever narrows what the statement
    matches, never widens it.
    """
    match = re.match(
        r"\s*DELETE FROM %s\.(\w+)\s+WHERE\s+(.*)" % re.escape(schema),
        sql,
        re.S | re.I,
    )
    if not match:
        return sql, []
    table, where = match.group(1), match.group(2)
    if table not in columns:
        return None, []
    present = columns[table]

    groups, dropped = [], []
    for group in _split_top_level(where, " AND "):
        wrapped = _wraps_all(group)
        inner = group[1:-1].strip() if wrapped else group
        kept = []
        for term in _split_top_level(inner, " OR "):
            missing = [c for c in _COLUMN_REF.findall(term) if c not in present]
            if missing:
                dropped.extend(missing)
            else:
                kept.append(term)
        if not kept:
            # An emptied group means either no subject or no anchor left.
            # Running the rest would widen the statement, so drop it whole.
            return None, dropped
        joined = " OR ".join(kept)
        groups.append("(%s)" % joined if len(kept) > 1 else joined)

    return "DELETE FROM %s.%s WHERE %s" % (schema, table, " AND ".join(groups)), dropped


def _sweep_database(ctx, problems, user_ids=()):
    """Hard-delete every row this run produced, plus orphans from crashed runs.

    Policies and access requests are included: the APIs only soft-delete them,
    so without this they would accumulate in the database indefinitely.

    user_ids comes from _resolve_user_ids, captured before anything was deleted.
    """
    pattern = f"{ctx.config['run']['prefix']}-%"
    schema = ctx.config["postgres"]["schema"]

    run = ctx.config["run"]
    statements = list(SWEEP_STATEMENTS)
    if run["delete_audit_rows"]:
        leading = list(AUDIT_SWEEP_STATEMENTS)
        if getattr(ctx, "borrowed_cos_admin_id", None):
            if run["delete_all_cos_admin_data"]:
                # Log tables first, then everywhere else the id appears. Both
                # run ahead of SWEEP_STATEMENTS, so nothing has been unhooked
                # from user_table by the time these match on it.
                leading += COS_ADMIN_LOG_ALL + COS_ADMIN_ROWS_ALL
                scope = "every row it owns, in every table but user_table"
            else:
                leading += COS_ADMIN_LOG_SCOPED + COS_ADMIN_ROWS_SCOPED
                scope = "rows from this run only"
            print(f"    borrowed cos admin: {scope}; the account itself is kept")
        statements = leading + statements

    try:
        connection = _connect(ctx.config)
    except Exception as err:  # noqa: BLE001
        problems.append(f"postgres sweep skipped, could not connect: {err}")
        return

    try:
        with connection.cursor() as cursor:
            anchors = _resolve_anchors(
                cursor,
                schema,
                pattern,
                {
                    "user_ids": list(user_ids),
                    "org_ids": [lane.org_id for lane in ctx.lanes],
                    "item_ids": [lane.item_id for lane in ctx.lanes],
                },
            )
            anchors["cos_admin_ids"] = _cos_admin_ids(ctx)
            print(
                f"    sweeping {len(anchors['user_ids'])} user(s), "
                f"{len(anchors['org_ids'])} org(s), {len(anchors['item_ids'])} item(s)"
            )
            columns = _schema_columns(cursor, schema)
            skipped = []
            for table, template in statements:
                sql = template.format(schema=schema)
                if columns is not None:
                    sql, dropped = _adapt_to_schema(sql, schema, columns)
                    if sql is None:
                        skipped.append(table)
                        continue
                    if dropped:
                        print(
                            f"    {table}: no {', '.join(sorted(set(dropped)))} "
                            f"column on this deployment, matched on the rest"
                        )
                try:
                    cursor.execute(sql, anchors)
                    if cursor.rowcount:
                        print(f"    swept {cursor.rowcount} row(s) from {table}")
                    connection.commit()
                except Exception as err:  # noqa: BLE001
                    connection.rollback()
                    problems.append(f"sweep {table}: {err}")
            if skipped:
                print(
                    f"    no such table on this deployment, skipped: "
                    f"{', '.join(skipped)}"
                )
    finally:
        connection.close()


def verify(ctx):
    """Re-query after teardown. Returns a list of survivor descriptions.

    Cleanup is asserted, not assumed — otherwise you discover months later that
    a deployment accumulated hundreds of orphan orgs.
    """
    survivors = []

    try:
        leftover_users = ctx.kc.find_users_by_prefix(ctx.namespace)
        for user in leftover_users:
            survivors.append(f"keycloak user {user.get('username')}")
    except Exception as err:  # noqa: BLE001
        survivors.append(f"could not verify Keycloak: {err}")

    _verify_broker(ctx, survivors)

    if not ctx.config["postgres"]["enabled"]:
        return survivors

    pattern = f"{ctx.namespace}%"
    schema = ctx.config["postgres"]["schema"]
    try:
        connection = _connect(ctx.config)
    except Exception as err:  # noqa: BLE001
        survivors.append(f"could not verify postgres: {err}")
        return survivors

    try:
        with connection.cursor() as cursor:
            columns = _schema_columns(cursor, schema)
            for table, template in VERIFY_STATEMENTS:
                if columns is not None and table not in columns:
                    # Nothing to assert on a table this deployment does not
                    # have. Not a survivor, and not a failure.
                    continue
                try:
                    cursor.execute(template.format(schema=schema), {"pattern": pattern})
                    count = cursor.fetchone()[0]
                    if count:
                        survivors.append(f"{count} row(s) in {table}")
                except Exception as err:  # noqa: BLE001
                    connection.rollback()
                    survivors.append(f"could not verify {table}: {err}")
    finally:
        connection.close()

    return survivors


def _verify_broker(ctx, survivors):
    """Assert the broker objects went too.

    Keycloak and Postgres were the whole of this check for a long time, and that
    made "nothing survived" a claim about half the platform: the exchange, the
    queue and the RabbitMQ user the catalogue creates live nowhere near either.
    A teardown step that timed out could therefore leave a broker user behind
    and the run would still report a clean bill of health — which is exactly
    what happened once, and is worse than not checking at all, because the
    report said the opposite of the truth.
    """
    import requests

    delete = ctx.config["ngsild_delete"]
    publish = ctx.config["ngsild_publish"]
    databroker = ctx.config["databroker"]

    def either(key):
        return delete.get(key) or publish.get(key) or databroker.get(key)

    mgmt = delete.get("mgmt_url") or ctx.config["gateway_delete"].get("mgmt_url")
    username, password = either("username"), either("password")
    if not (mgmt and username and password):
        return
    vhost = delete.get("vhost") or publish.get("vhost")

    def gone(path, what):
        try:
            response = requests.get(
                f"{mgmt.rstrip('/')}/{path}", auth=(username, password), timeout=20
            )
        except Exception as err:  # noqa: BLE001 - an unreachable broker is not a survivor
            survivors.append(f"could not verify {what}: {err}")
            return
        if response.status_code == 200:
            survivors.append(what)

    for lane in ctx.lanes:
        if lane.item_id and vhost:
            quoted = quote(vhost, safe="")
            gone(f"api/exchanges/{quoted}/{lane.item_id}", f"rabbitmq exchange {lane.item_id}")
            gone(f"api/queues/{quoted}/{lane.item_id}", f"rabbitmq queue {lane.item_id}")
        provider_id = getattr(lane.provider, "user_id", None)
        if provider_id:
            gone(f"api/users/{provider_id}", f"rabbitmq user {provider_id}")

def _connect(config):
    import psycopg2

    postgres = config["postgres"]
    return psycopg2.connect(
        host=postgres["host"],
        port=postgres["port"],
        dbname=postgres["database"],
        user=postgres["user"],
        password=postgres["password"],
        sslmode=postgres["sslmode"],
        connect_timeout=10,
    )