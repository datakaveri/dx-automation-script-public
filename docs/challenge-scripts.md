# Challenge scripts — how to use them

A runbook for `script/challenge/`: nine standalone scripts that create a
challenge in `dx-community-layer`, set its submission window and evaluation
deadline, delete it — play a round on it: N consumers submit solutions, the
admin scores them, the consumers are cleaned up — and, in SQL, move the
dates into the past so the round can be finished the same day, and purge a
published challenge afterwards. Everything below was verified against dev
(`v2.dev.iudx.io/community`, UI `dev.mahaagx.iudx.io`) on 2026-09-16/17 and
against the server source at `dev` @ `2a384ac`.

| | |
|---|---|
| Scripts | `script/challenge/{creation,submission_time,evaluation_time,deletion,submission,evaluation,submission_cleanup,db_backdate,db_purge}/` |
| Reference | [`script/challenge/README.md`](../script/challenge/README.md) — every config key |
| Walkthrough | [`challenge-round-walkthrough.md`](challenge-round-walkthrough.md) — the full round, step by step, for someone new |
| Server | `github.com/datakaveri/dx-community-layer`, `src/routes/challenge/admin.py` |

## 1. Setup, once

```bash
cd script/challenge
python3 -m pip install -r requirements.txt
for d in creation submission_time evaluation_time deletion submission evaluation submission_cleanup db_backdate db_purge; do
  cp $d/challenge_${d}_config.json.example $d/challenge_${d}_config.json
done
```

Fill in the same three blocks in each config (they are identical):

```json
"community": { "base_url": "https://v2.dev.iudx.io/community" },
"keycloak":  { "url": "https://v2.dev.iudx.io/auth", "realm": "iudx-v2", "user_client_id": "frontend-client" },
"cos_admin": { "username": "cos-admin@example.com", "password": "…" }
```

The account **must have the `cos_admin` realm role** — every admin route
answers 403 otherwise. A ready-made JWT can go in `cos_admin.token` instead.
`${VAR}` in any value reads the environment. The `*_config.json` files are
gitignored.

`submission` and `submission_cleanup` need one more thing when they have to
create consumer accounts: the Keycloak admin client (`keycloak.admin_client_id`
+ `admin_client_secret`, the same as `user_creation/consumer` uses).

`db_backdate` and `db_purge` need a `postgres` block instead — the challenge
database the community layer runs on (`CHALLENGE_DATABASE_URL` /
`CHALLENGE_DB_SCHEMA` in its deployment; schema `tgdx_dev` on dev):

```json
"postgres": { "host": "…", "port": 5432, "database": "…", "schema": "tgdx_dev",
              "user": "…", "password": "…", "sslmode": "require" }
```

Every script accepts `--dry-run` (prints the request, calls nothing) and
`--help`.

## 2. Two ways to run it

### A. Publish in one go — the challenge is in the UI immediately

`creation/challenge_creation_config.json`:

```json
"challenge": { "draft": false, ... },
"times": {
  "creation":         "today",
  "submission_start": "today",
  "submission_end":   "today+14d",
  "evaluation_end":   "today+21d"
}
```

```bash
python3 creation/challenge_creation.py --dry-run   # look at the body first
python3 creation/challenge_creation.py             # → status PUBLISHED
```

That is the whole flow. The other scripts do not apply afterwards: **the
server freezes a published challenge** — `PUT` and `DELETE` on it are
refused (400) — so it can only be changed or removed in the database.

### B. Build it up as a draft, publish at the end

`"draft": true` in the creation config, then:

```bash
python3 creation/challenge_creation.py                          # DRAFT — not visible to consumers
python3 submission_time/challenge_submission_time.py            # sets submission_start / submission_end
python3 evaluation_time/challenge_evaluation_time.py            # sets evaluation_end
python3 evaluation_time/challenge_evaluation_time.py --publish  # → PUBLISHED
python3 deletion/challenge_deletion.py                          # only while still DRAFT / SCHEDULED
```

Each script reads the id from `creation/challenge_created.json`, which the
creation script writes and the deletion script removes, so nothing is typed
twice. To act on some other challenge: `--competition-id <uuid>` or
`--title "<exact title>"`.

Use B when the dates are decided later than the content, or to test the
update routes; use A for a demo.

## 3. Times

Every config has one `times` block; that is the only place times come from.

| key | meaning |
|---|---|
| `creation` | when the challenge goes live. Only used when publishing: `today`/now → published at once; a future time → SCHEDULED, and the server's cron (every minute) publishes it then. Ignored for a draft. |
| `submission_start` / `submission_end` | the submission window (plain dates) |
| `evaluation_end` | the evaluation deadline (plain date) |

Values are expressions: `today`, `tomorrow`, `2026-10-01`, `01/11/2026`,
`today+14d`, `today+2w`, `today 06:00`, `submission_start+14d`,
`submission_end+7d` (in the update scripts, "relative to what the server
holds"). `null` = leave unset / unchanged. Command line overrides:
`--creation-time`, `--submission-start`, `--submission-end`,
`--evaluation-end`.

### What the server checks — this decides what "today for everything" does

| | create (`POST`, `draft: false`) | update (`PUT`, `draft: false`) |
|---|---|---|
| date order | **not checked** | `submission_start` ≥ publish date, `submission_end` > `submission_start`, `evaluation_end` > `submission_end` — strictly, on whole dates |
| required | 13 text/date fields, `rules_and_guidelines` (an uploaded file), `data_models` not null (`[]` ok) | the same, plus `data_models` **or** `ai_models` non-empty |

So `today / today / today` with `draft: false` **works in flow A** (the
script warns, the server accepts) and gives a PUBLISHED challenge at once.
The same dates can **never be published through flow B** — use at least
`today`, `today+1d`, `today+2d` there; the scripts check locally and stop
before calling.

After publishing, the cron moves a challenge to EVALUATION the day after
`submission_end`, so an all-`today` one shows under "evaluation" from
tomorrow. Consumers see PUBLISHED, EVALUATION and COMPLETED; DRAFT and
SCHEDULED exist only in the admin panel.

### The "start date shows yesterday" quirk

The public **detail page** shows `published_at` as the challenge's "start",
and parses the server's value — UTC with the offset dropped, e.g.
`2026-09-16T18:55:57` — as local time. A challenge published between
**00:00 and 05:30 IST** therefore shows the previous day as its start
(submission dates are unaffected). That is a UI bug (`new Date(published_at)`
without `Z`; the admin panel appends `Z` and is right). Until it is fixed:
publish after 05:30 IST, or set `"creation": "today 06:00"` to schedule it.
The creation script warns when a publish falls in that window. Every
timestamp the scripts print is shown both as the server sent it and in IST
(`published_at_local`).

## 4. Content

In `creation/challenge_creation_config.json`, the `challenge` block is the
request body, key for key. The ones worth changing per challenge:

| key | notes |
|---|---|
| `title` / `title_prefix` | blank title → `<prefix>-<timestamp>-<random>`; titles are unique on the server |
| `subtitle`, `overview`, `description`, `constraints`, `evaluation_criteria_definition`, `submission_file_definition`, `dataset_description` | free text; all required to publish |
| `prize_type`, `total_pool_amount`, `currency`, `prize_pool_description` | `CASH` needs the amount and a 3-letter currency; `NO_CASH` ignores them. Dev config: CASH, 10000, INR. The detail page splits the pool 50/30/20 itself. |
| `data_models`, `ai_models` | `[{"id": "<uuid>", "name": "…"}]` — catalogue items, see below |
| `image_url`, `additional_assets`, `other_resources` | optional |

**Files.** `rules_and_guidelines` is required to publish and must already be
in the challenge S3 bucket. The `uploads` block does that: name a local file
in `uploads.rules_and_guidelines_file` (default: the shipped
`sample_rules_and_guidelines.md`) and the script uploads it through
`POST /challenge/attachment` and puts the returned key in the body. Same for
`image_file` and `additional_asset_files`.

**Data models.** The admin UI's dataset picker searches the catalogue:

```bash
curl -s -X POST "https://v2.dev.iudx.io/controlplane/iudx/v2/cat/search?page=1&size=20" \
  -H "Content-Type: application/json" \
  -d '{"searchCriteria":[{"searchType":"term","field":"type","values":["adex:DataBank"]}]}'
#                                                          ...or ["adex:AiModel"] for ai_models
```

Take `id` and `name` from `result[]`. The dev config uses *Maharashtra
Weather Forecast at Block Level*, *Weather Data for Rahuri Station* and
*ResNet-50 Plant Disease*.

## 5. A round on a published challenge: submit N solutions, score them

The challenge must be PUBLISHED with a submission window that includes
today — flow A with `today` everywhere does exactly that. Then:

```bash
python3 submission/challenge_submission.py --count 5        # 5 consumers, 5 solutions
python3 evaluation/challenge_evaluation.py                  # scores all 5
python3 submission_cleanup/challenge_submission_cleanup.py  # deletes the 5 consumers
```

**Submission.** One submission per account, so `--count 5` means five
accounts. The script takes the ones in `consumers.accounts` first (username +
password, or a token) and creates the rest in Keycloak
(`consumers.auto_create` — password `Consumer@Pass1`, usernames
`e2e-dev-submitter-<timestamp>-<n>-<random>@<domain>`). For each one it
joins, uploads a `.zip` and a `.pdf` — generated on the fly unless
`submission.solution_zip_file` / `document_file` name real files — and
submits. Everything lands in `submission/submissions_created.json`: accounts,
passwords, submission ids, and which accounts were created. Re-running against
the same accounts hits the server's 409 "already submitted"; set
`submission.on_existing: "skip"` to record it and carry on.

**Evaluation.** Signs in as the COS admin, lists the challenge's submissions,
and scores each unscored one (`evaluate.score.mode`: `random` 50–100,
`fixed`, `descending`, or `list`) with a comment. The server lets this happen
while the challenge is still PUBLISHED — no need to wait for EVALUATION. A
score has to be above 0; the server treats 0 as "unscored". Results go to
`evaluation/submissions_evaluated.json`.

`--announce` moves the challenge to COMPLETED — **but only from the day after
`submission_end`**, and only once every non-disqualified submission is
scored. With an all-`today` challenge the script reports "submission_ends_at
is 2026-09-17 and today is 2026-09-17" and holds; run it again tomorrow.

**Cleanup.** Deletes the Keycloak accounts flagged `created_user` in the
handoff (never the ones you listed, unless `delete.configured_accounts` is
true) and removes the file. Submissions themselves have no delete route;
they stay with the challenge.

## 6. The whole thing in one sitting: backdate in SQL

Everything in §5 works today except the two steps the server gates on the
calendar date: PUBLISHED → EVALUATION (cron, day after `submission_end`)
and `announce-result` → COMPLETED (same rule). To test those today, edit
the dates in the database:

```bash
python3 creation/challenge_creation.py --publish              # today / today / today
python3 submission/challenge_submission.py --count 5
python3 db_backdate/challenge_db_backdate.py --dry-run        # shows the UPDATEs
python3 db_backdate/challenge_db_backdate.py                  # window → yesterday, waits for the cron → EVALUATION
python3 evaluation/challenge_evaluation.py --announce         # scores 5, announces → COMPLETED
python3 submission_cleanup/challenge_submission_cleanup.py   # Keycloak accounts
python3 db_purge/challenge_db_purge.py --dry-run              # counts every row it would delete
python3 db_purge/challenge_db_purge.py                        # challenge + children + created users
```

`db_backdate` sets `submission_starts_at` / `submission_ends_at` /
`published_at` and every submission's `created_at` to yesterday (config
`times.*`, any expression), NULLs `scheduled_publish_at` (otherwise the cron
republishes it every minute), and then waits for the cron to move it to
EVALUATION. If the deployment's cron is not running, `--status EVALUATION`
sets it directly. It refuses anything not currently PUBLISHED.

`db_purge` deletes `WHERE competition_id = <id>` in every table that has
that column, then the `competitions` row, then the community layer's `users`
rows for the accounts the submission script created (never accounts you
listed). It refuses a title that does not start with `e2e-`
(`purge.require_title_prefix`), counts before, verifies zero after, and
commits only then. The S3 files under `private/<user>/<competition>/` stay.

## 7. Deleting

```bash
python3 deletion/challenge_deletion.py                         # the one in challenge_created.json
python3 deletion/challenge_deletion.py --competition-id <uuid>
python3 deletion/challenge_deletion.py --title "e2e-dev-challenge-…"
```

Works on DRAFT and SCHEDULED (`delete.allowed_statuses`); the server itself
refuses only PUBLISHED, and the caller must be the admin who created it. The
script reads the challenge first, shows it, deletes, and confirms a 404.
Already gone → exits 0.

## 8. Things that went wrong, and what they meant

| symptom | cause |
|---|---|
| `2 timeline problem(s)` with all-`today` | `draft: false` in an **update** script — the server enforces order there. Use A, or spread the dates. |
| `is PUBLISHED; the server only accepts updates while it is DRAFT or SCHEDULED` | update/delete on a published challenge — nothing to do via the API |
| `data_models and ai_models are both empty` | publishing via update needs one of them |
| create returns 500 "Chanllenge creation failed" | `rules_and_guidelines` key not in S3 — let `uploads` handle it |
| challenge not in the UI after create | status SCHEDULED (`creation` in the future) — wait for the cron, or use `today` |
| `published_at` looks 5½ h old | UTC without an offset; see §3 |
| detail page start = yesterday | published before 05:30 IST; see §3 |
| 403 on every call | token has no `cos_admin` role |
| `Submission window has not started` / `has ended` | today (IST date) is outside `submission_start..submission_end` |
| `Competition is not published` | submitting to a DRAFT / SCHEDULED / EVALUATION challenge |
| `Submission already exists` (409) | that account already submitted — use another, or `on_existing: "skip"` |
| `Invalid file extension for attachment` | the two files must be a `.zip` plus a `.pdf`/`.doc`/`.docx` |
| `Not announcing: submission_ends_at is … and today is …` | announce needs the day after the window; the evaluation itself succeeded |
| `'keycloak.admin_client_id' is needed to create accounts` | `--count` exceeds `consumers.accounts` and no admin client is configured |
| `status is still PUBLISHED after 150s — is the cron running` | db_backdate in `cron` mode on a deployment without the cron; use `--status EVALUATION` |
| `refusing: title '…' does not start with purge.require_title_prefix` | db_purge's guard — the id or title names a challenge the scripts did not make |
| `rows survived the delete, rolling back` | a table the purge cannot clear (permissions, or a trigger); nothing was committed |
