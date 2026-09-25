# Challenge scripts

Start with **[docs/challenge-round-walkthrough.md](../../docs/challenge-round-walkthrough.md)**
— the full round step by step — or **[docs/challenge-scripts.md](../../docs/challenge-scripts.md)**,
the runbook: setup, the two ways to run, times, content, and what each error means.
This file is the reference for every config key.

Nine standalone scripts, one per job, each with its own config. Four manage
the challenge through the admin API; three play out a round on it —
consumers submit, the admin scores; two go straight to the database, for
what the API cannot do — move the dates into the past so the round can be
finished today, and remove a published challenge afterwards:

```
challenge/
├── creation/            challenge_creation.py            + challenge_creation_config.json
├── submission_time/     challenge_submission_time.py     + challenge_submission_time_config.json
├── evaluation_time/     challenge_evaluation_time.py     + challenge_evaluation_time_config.json
├── deletion/            challenge_deletion.py            + challenge_deletion_config.json
├── submission/          challenge_submission.py          + challenge_submission_config.json
├── evaluation/          challenge_evaluation.py          + challenge_evaluation_config.json
├── submission_cleanup/  challenge_submission_cleanup.py  + challenge_submission_cleanup_config.json
├── db_backdate/         challenge_db_backdate.py         + challenge_db_backdate_config.json   (SQL)
└── db_purge/            challenge_db_purge.py            + challenge_db_purge_config.json      (SQL)
```

Every script is standalone: it imports nothing from this repository and reads
one JSON config. Copy the `.json.example` beside it, fill in the values, run it.

```bash
python -m pip install -r requirements.txt

cd creation
cp challenge_creation_config.json.example challenge_creation_config.json
python challenge_creation.py                          # or: … some_config.json

cd ../submission_time
cp challenge_submission_time_config.json.example challenge_submission_time_config.json
python challenge_submission_time.py

cd ../evaluation_time
cp challenge_evaluation_time_config.json.example challenge_evaluation_time_config.json
python challenge_evaluation_time.py

cd ../deletion
cp challenge_deletion_config.json.example challenge_deletion_config.json
python challenge_deletion.py

cd ../submission
cp challenge_submission_config.json.example challenge_submission_config.json
python challenge_submission.py --count 5                # 5 consumers, 5 solutions

cd ../evaluation
cp challenge_evaluation_config.json.example challenge_evaluation_config.json
python challenge_evaluation.py                          # scores every submission

cd ../submission_cleanup
cp challenge_submission_cleanup_config.json.example challenge_submission_cleanup_config.json
python challenge_submission_cleanup.py                  # removes the consumers it created
```

Every script takes `--dry-run`, which prints what it would do and makes no
calls. Start there.

## What each script does

| script | call | needs |
|---|---|---|
| **creation** | `POST /challenge/admin/challenge` | a `cos_admin` token |
| **submission_time** | `PUT /challenge/admin/challenge/{id}` with `submission_starts_at`, `submission_ends_at` | the same, and a DRAFT or SCHEDULED challenge |
| **evaluation_time** | `PUT /challenge/admin/challenge/{id}` with `evaluation_ends_at` | the same |
| **deletion** | `DELETE /challenge/admin/challenges/{id}` | the same admin that created it, and a challenge that is not PUBLISHED |
| **submission** | per consumer: `POST /challenge/users/challenges/{id}/join`, `POST /challenge/attachment` ×2, `POST /challenge/{id}/submission` | consumer accounts (or Keycloak admin credentials to create them), and a PUBLISHED challenge whose window includes today |
| **evaluation** | `PUT /challenge/admin/submission/{id}/evaluate` per submission; `--announce` adds `POST /challenge/admin/challenges/announce-result` | a `cos_admin` token |
| **submission_cleanup** | `DELETE /admin/realms/{realm}/users/{id}` in Keycloak | Keycloak admin credentials |
| **db_backdate** | `UPDATE competition_timelines / competitions / competition_submissions … WHERE competition_id = <id>` | a Postgres login to the challenge database |
| **db_purge** | `DELETE … WHERE competition_id = <id>` on every child table, then `competitions`, then the created `users` | the same |

The first four are admin routes: the token must carry the `cos_admin` realm role, or
the server answers 403. `cos_admin` in each config takes a `username` +
`password` (exchanged for a token through `keycloak.user_client_id`) or a
ready-made `token`. On dev a `frontend-client` password-grant token for the
COS admin is accepted — confirmed 2026-09-16.

### Why the creation script makes a draft

The server lets a challenge be updated or deleted **only while it is DRAFT or
SCHEDULED**. A PUBLISHED challenge is frozen: `PUT` answers 400 "not in draft
or scheduled status", `DELETE` answers 400 "Published challenges cannot be
deleted", and nothing else removes it. So `challenge.draft` defaults to `true`,
the two time scripts default to `update.draft: true` (edit without publishing),
and the deletion script refuses a PUBLISHED one before the server gets to.

Publishing is a deliberate step: `--publish` on any of the three, or
`draft: false` in its config. A `times.creation` in the future makes it
SCHEDULED instead, and the server's cron (every minute) publishes it when that
time comes.

### What the server checks when publishing — create vs update

The two routes are not the same, and the scripts mirror each exactly
(`admin_requests.py`, `admin_services.py`):

| | `POST` create with `draft: false` | `PUT` update with `draft: false` |
|---|---|---|
| required fields | subtitle, overview, description, constraints, evaluation_criteria_definition, submission_file_definition, prize_pool_description, the three dates, rules_and_guidelines, dataset_description, `data_models` (must not be null; `[]` passes) | the same, read from what is stored, plus **`data_models` or `ai_models` non-empty** |
| CASH | currency + total_pool_amount | the same |
| date order | **not checked** | start ≥ publish date, end > start, evaluation end > end — strictly, on whole dates |

So `today` for all three times with `draft: false` **is accepted on create**
and gives a PUBLISHED challenge visible to consumers at once; the creation
script only warns. The cron moves it to EVALUATION the day after
`submission_ends_at`, i.e. from tomorrow, where it stays visible under the
evaluation tab. The same dates can never be *published through the update
scripts*, which is why those fail locally before calling.

Consumers see only PUBLISHED, EVALUATION and COMPLETED
(`retrieve_competitions_handler`); DRAFT and SCHEDULED exist only in the admin
routes.

### Files: rules_and_guidelines and the rest

`rules_and_guidelines` is required to publish and has to be a file already in
the challenge bucket: the server `head_object`s the key and copies it under
the new challenge, and a missing key fails the whole create with a 500. The
creation script's `uploads` block does that upload for you:

```json
"uploads": {
  "enabled": true,
  "rules_and_guidelines_file": "sample_rules_and_guidelines.md",
  "image_file": "",
  "additional_asset_files": [{"file": "dataset-notes.pdf", "description": "…"}],
  "content_type": "application/octet-stream",
  "timeout_seconds": 120
}
```

Each file goes through `POST /challenge/attachment` (a presigned S3 PUT under
`temp/<admin>/<batch>/<name>`) and the key it returns replaces the matching
field in the body. Paths are relative to the config file. A shipped
`sample_rules_and_guidelines.md` is the default so a publish works out of the
box; give it a real document for a real challenge. To pass keys you uploaded
yourself instead, set `uploads.enabled: false` and put them in `challenge`.

### Why deletion is here

A draft the creation script made stays on the server until something removes
it. The deletion script is that something — it reads the handoff file, shows
what it found, checks the status against `delete.allowed_statuses`, deletes,
and then confirms the id answers 404. It also serves to clear a draft by
`--title` or `--competition-id` that no handoff describes.

## How the scripts hand over to each other

The creation script writes `challenge_created.json` beside itself — id, title,
status, the timeline as sent. The others read it through `target.input_file`
(`../creation/challenge_created.json`): the time scripts write the dates now
on the server back into it, and the deletion script removes it once the
challenge is gone. The submission script writes `submissions_created.json`
(accounts, passwords, submission ids, which accounts it created); the
evaluation script reads that too (`target.submissions_file`) and writes
`submissions_evaluated.json`; the cleanup script consumes
`submissions_created.json`.

So the normal sequences need nothing typed twice:

```bash
# a draft, edited, deleted
python creation/challenge_creation.py
python submission_time/challenge_submission_time.py
python evaluation_time/challenge_evaluation_time.py
python deletion/challenge_deletion.py

# a published challenge with a round played on it
python creation/challenge_creation.py --publish
python submission/challenge_submission.py --count 5
python evaluation/challenge_evaluation.py
python submission_cleanup/challenge_submission_cleanup.py

# the same, finished and removed the same day (SQL for the two steps the API gates by date)
python creation/challenge_creation.py --publish
python submission/challenge_submission.py --count 5
python db_backdate/challenge_db_backdate.py            # dates → yesterday, status → EVALUATION
python evaluation/challenge_evaluation.py --announce   # scores, then → COMPLETED
python submission_cleanup/challenge_submission_cleanup.py
python db_purge/challenge_db_purge.py
```

To work on a challenge that file does not describe, pass `--competition-id`,
or `--title` (looked up by exact title in the admin lists), or set
`target.competition_id` / `target.title`. Naming a different challenge makes
the script **ignore the handoff file** rather than merge with it, and leave it
in place afterwards, because the challenge it names is still there.

## Submissions and evaluation

The server's rules, from `submission_services.py`, `admin_services.py` and
`competition_services.py`:

- **One submission per user per challenge** (409 after that), so N solutions
  need N accounts. `consumers.accounts` lists the ones to use; when
  `consumers.count` / `--count` asks for more, `consumers.auto_create` makes
  the rest in Keycloak through the Admin API (`keycloak.admin_client_id` +
  `admin_client_secret`). Nothing else is needed: the community layer inserts
  the user from the token on its first request, and the consumer routes need
  no realm role. Accounts this script created are flagged `created_user` in
  the handoff and are what `submission_cleanup` deletes.
- **Exactly two attachments**: a `.zip` (the solution) and a `.pdf`/`.doc`/
  `.docx` (the write-up), uploaded through `POST /challenge/attachment` like
  the creation script's files. `submission.solution_zip_file` /
  `document_file` name yours; blank means the script generates them — a zip
  with `predictions.csv` + `README.txt`, and a one-page PDF — so `--count 5`
  works with nothing prepared.
- **Window check is by calendar date** (Asia/Kolkata): PUBLISHED, and
  `submission_starts_at ≤ today ≤ submission_ends_at`. Same-day `today`
  timelines therefore accept submissions all day.
- **Scoring needs no status**: the admin can score a submission while the
  challenge is still PUBLISHED; only CANCELLED is refused. The score must be
  above 0 — the server reads 0 as "not scored" (`if req_params.score:`), and
  the evaluation script refuses to send one. `evaluate.score.mode` is
  `random` (within `min`..`max`), `fixed`, `descending` (`max` down by
  `step`) or `list`. Already-scored submissions are skipped unless
  `evaluate.skip_scored` is false; `evaluate.which` narrows to the ids in the
  submission handoff (`file`) or to `evaluate.submission_ids` / repeated
  `--submission-id`.
- **Announcing is date-gated**: `announce-result` (challenge → COMPLETED)
  needs `submission_ends_at < today` and every non-disqualified submission
  scored. `--announce` checks both first and says exactly why it is holding
  back; with a same-day window that is always "from tomorrow". The cron's
  PUBLISHED → EVALUATION move has the same rule.
- **Submissions cannot be deleted** through the API. They go with the
  challenge (cascade) — the deletion script while DRAFT/SCHEDULED, the
  database once PUBLISHED. `submission_cleanup` therefore only removes the
  Keycloak accounts and the handoff file.

Verified on dev on 2026-09-17: 2 accounts created → joined → 2 generated
solutions submitted → both scored and read back → `--announce` correctly
held ("submission_ends_at is 2026-09-17 and today is 2026-09-17") → 2
accounts deleted.

## Finishing the round today: db_backdate and db_purge

The server compares the timeline by calendar date, so after a same-day
round the last two steps — the cron's move to EVALUATION and
`announce-result` → COMPLETED — are always tomorrow's. There is no API
that shortens that, and none that deletes a published challenge. The two
`db_*` scripts do it in SQL, each scoped to one competition id, in one
transaction, with `--dry-run` printing the exact statements first.

**db_backdate** rewrites the dates (`times.*`, the same expression syntax as
everywhere else, `null` leaves a column alone):

| column | default |
|---|---|
| `competition_timelines.submission_starts_at`, `submission_ends_at` | `yesterday` |
| `competition_timelines.evaluation_ends_at` | `today+7d` |
| `competitions.published_at` | `yesterday` |
| `competition_submissions.created_at`, `updated_at` (all on the challenge) | `yesterday` |

and clears `competitions.scheduled_publish_at` (`clear_scheduled_publish_at`):
the cron republishes anything with a past `scheduled_publish_at` that is not
PUBLISHED, which would undo EVALUATION every minute for a challenge that was
ever SCHEDULED. Then `status.mode`: `cron` (default) commits and waits up to
`wait.timeout_seconds` for the server's cron to set EVALUATION — the real
server path; `direct` sets `competitions.status` itself (`--status
EVALUATION`), for a deployment whose cron is off; `none` touches only the
dates. `status.require_current` (`["PUBLISHED"]`) refuses anything else.
The new dates and status are written back into `challenge_created.json`.

**db_purge** deletes, in this order: every table in the schema that has a
`competition_id` column (found in `information_schema`, so a table added
later or missing on one deployment is handled), `WHERE competition_id =
<id>`; the `competitions` row; then, for the accounts flagged
`created_user` in `submissions_created.json` only, every table with a
`user_id` column and the `users` row. It counts each table before, insists
on zero after, and only then commits and removes the three handoff files.
`purge.require_title_prefix` (`e2e-`) refuses a challenge whose title does
not start with it, so an id typo cannot take a real challenge along;
`purge.allowed_statuses` can narrow further. S3 objects and Keycloak
accounts are not its business — `submission_cleanup` does the accounts.

Both need a `postgres` block: `host`, `port`, `database`, `schema`
(`tgdx_dev` on dev — `CHALLENGE_DB_SCHEMA` in the server's env), `user`,
`password`, `sslmode`. Table and column names are config too.

Verified on 2026-09-17 against a local Postgres 16 loaded with the server's
`01_challenge_schema.sql`, seeded with a test challenge (3 submissions, 3
participants, a bookmark, a prize pool, two script-created users and one
listed user) beside a decoy "Real production challenge": backdate dry-run →
cron mode (timed out cleanly with no cron) → direct mode → EVALUATION;
purge refused the decoy by title, dry-run counted 12 tables, live run left
the decoy, the admin and the listed user untouched and everything else at
zero.

## Time configuration

Every config has a `times` block, and that is the one place the times come
from. Write `today` for all of them, or any date you like:

```json
"times": {
  "creation": "today",
  "submission_start": "today",
  "submission_end": "today",
  "evaluation_end": "today"
}
```

| key | what it is | used by |
|---|---|---|
| `creation` | when the challenge goes live — the server's `publish_schedule`. Only sent when publishing: now or the past publishes at once, the future makes it SCHEDULED. A draft has no publish time, so it is noted and not sent | all three (when publishing) |
| `submission_start` | `submission_starts_at` | creation, submission_time |
| `submission_end` | `submission_ends_at` | creation, submission_time |
| `evaluation_end` | `evaluation_ends_at` | creation, evaluation_time |

Each script writes only its own fields; the creation script has all four, the
two time scripts carry `creation` plus their own. `null` leaves a time out —
unset on create, unchanged on update. Any of them can be overridden on the
command line: `--creation-time`, `--submission-start`, `--submission-end`,
`--evaluation-end`.

Each value is an **expression**:

```
[base][offset...][ HH:MM]
```

| part | accepts |
|---|---|
| base | `today` / `now` (or blank), `tomorrow`, `yesterday`; an absolute date in any of `time_format.date_formats` — `2026-10-01`, `01/11/2026`, `2026-10-01 09:00`, `2026-10-01T09:00:00+05:30`; or the **name of another time** |
| offset | `+N` / `-N` followed by `min`, `h`, `d` or `w`; several may be chained: `today +2w -1d` |
| HH:MM | a clock time — kept for `creation`, dropped for the others, which the server stores as plain dates |

Naming another time makes one relative to it: `"submission_end": "submission_start+14d"`,
`"evaluation_end": "submission_end+7d"`. In the creation script that means a
time higher up in the block; in the two update scripts it means the value set
in this run, or failing that **the one already on the server** (either
spelling works: `submission_end` or `submission_ends_at`, and `published_at` /
`scheduled_publish_at` too). So:

```
"today" for everything                 a same-day draft
"2026-10-01", "2026-10-15", "2026-10-22"   fixed dates
"today", "today+14d", "today+21d"      the shipped defaults, always in the future
"today", "submission_start+14d", "submission_end+7d"   chained
```

The `time_format` block controls how they are read and written: `timezone`
(default `Asia/Kolkata`, which is what the server stamps in — `today` is read
there), `date_formats` for absolute input, `send_date_format` /
`send_datetime_format` for what goes on the wire.

### Why the server's timestamps look 5½ hours old

What the server does (`admin_services.py`, `01_challenge_schema.sql`,
`custom_responses.py`): it stamps `published_at`, `scheduled_publish_at` and
`updated_at` with `datetime.now(pytz.timezone("Asia/Kolkata"))`, the columns
are `timestamptz` so Postgres keeps them in UTC, and the response encoder
prints them with `strftime("%Y-%m-%dT%H:%M:%S")` — the UTC value with the
offset dropped. A challenge published at `00:21 IST` on the 17th therefore
comes back as `2026-09-16T18:51:20`. The timeline columns are plain `date`,
always the IST calendar date.

One consequence shows in the public UI. The challenge **detail page**
(`/challenges/{id}`) takes its "start" from `published_at` and parses it with
`new Date("2026-09-16T18:55:57")` — no `Z`, so the browser reads it as local
time and shows the 16th for a challenge published at 00:26 IST on the 17th.
The submission dates are plain `YYYY-MM-DD` and render correctly. The admin
panel appends `Z` before parsing and is right; the detail page is a UI bug.
Until it is fixed, publish after 05:30 IST — when UTC and IST share a date —
or set `times.creation` to e.g. `today 06:00`, which schedules the challenge
and lets the cron publish it then. The creation script warns when a publish
falls in the 00:00–05:30 IST window.

The scripts follow that: every server datetime is read as UTC and shown
again in `time_format.timezone` (`published_at_local` in the handoff, `UTC →
IST` in the summaries), an expression such as `published_at+1d` uses the
converted value, `publish_schedule` is sent with an explicit `+05:30` offset —
the server's publish check takes `.date()` of what it was sent — and titles
and `created_at` are stamped in the same zone.

### Validation

The `validate` block applies the server's publish rules locally before
anything is sent. In the two update scripts that is the three date rules plus
the required-field / datasets / CASH preflight from what the server holds;
`mode: auto` fails when publishing and warns for a draft, matching what the
server would do. In the creation script the server checks no date order at
all, so `auto` only warns there. `fail`, `warn` and `off` override either
way, and each date rule can be switched off on its own.

## Configuration

Every URL, credential, endpoint path, body field, timeout and step toggle is
a config key; the `.json.example` files list all of them with the defaults,
so anything can be pasted over.

- **`${VAR}` and `${VAR:-fallback}`** are expanded from the environment
  anywhere in a config. The examples read `COS_ADMIN_PASS` that way.
- **`endpoints`** holds every path the script calls; change one here when a
  deployment differs.
- **Data models.** `data_models` / `ai_models` are catalogue items,
  `{"id": "<uuid>", "name": "…"}`. The admin UI fills them from
  `POST {controlplane}/iudx/v2/cat/search` with `searchCriteria` `type` =
  `adex:DataBank` (datasets) or `adex:AiModel` (models); any item from there
  works. The dev config uses *Maharashtra Weather Forecast at Block Level*,
  *Weather Data for Rahuri Station* and *ResNet-50 Plant Disease*.
- **`challenge`** (creation) is the request body key for key, plus
  `extra_fields` for anything verbatim. `title` blank builds one from
  `title_prefix` + `title_template` (`{prefix}`, `{timestamp}`, `{random}`),
  because titles are unique on the server. `image_url`,
  `rules_and_guidelines` and `additional_assets` take S3 keys, or are filled
  in from `uploads`. `data_models` / `ai_models` are catalogue items,
  `{"id": "<uuid>", "name": "…"}`. `create.omit_null_fields` drops nulls from
  the body so the server's own defaults apply.
- **`update`** (both time scripts): `draft`, `require_status` (what the script insists on before calling; the server
  refuses anything else with a 400 anyway), `verify_after` (read back and
  check the dates landed), `expect_status`, `extra_fields`.
- **`delete`**: `allowed_statuses` — `["DRAFT", "SCHEDULED"]` by default. The
  server itself refuses only PUBLISHED, so EVALUATION, COMPLETED and
  CANCELLED can be added when that is really wanted; empty means whatever
  the server allows. `verify_after` / `expect_gone_status` confirm the id
  answers 404 afterwards.
- **`consumers`** (submission): `count`, `accounts` (`username` +
  `password`, or `token`), `auto_create` (the Keycloak user template —
  `username_template` takes `{prefix}`, `{timestamp}`, `{index}`, `{random}`,
  `{domain}`).
- **`submission`**: `title_template` / `description_template` (`{index}`,
  `{username}`, `{challenge}`, `{timestamp}`, `{random}`), the two files or
  the `generate` block, `on_existing` (`fail` / `skip` on a 409),
  `verify_after`, `pause_seconds`. `join.already_joined_ok` treats a join 409
  as fine.
- **`evaluate`** / **`announce`** (evaluation): see the section above.
- **`delete`** (submission_cleanup): `created_accounts` (default on),
  `configured_accounts` (default off — never touch accounts you listed),
  `missing_ok`, `verify_after`, `remove_input_file`.
- **`postgres`**, **`times`**, **`status`**, **`wait`** (db_backdate) and
  **`purge`**, **`tables.skip`** (db_purge): see the section above.
- **`lookup`** (`--title`): which admin lists to page and how far. The list
  endpoint's `query` is a prefix full-text search that does not cope with
  hyphenated titles, so `use_query` is off and the lists are paged whole.
- **Output.** Every request and response is printed as the run goes, and each
  script ends with a summary block. `logging.mask_secrets` is **off**, so the
  admin password and tokens print verbatim — turn it on before sharing a log.

## The configs in this tree

The `*_config.json` files beside each script point at dev
(`v2.dev.iudx.io/community`, realm `iudx-v2`) with the COS admin from
`user_creation/org_admin` and, for submission / cleanup, the `admin-client`
Keycloak client from `user_creation/consumer`. They are gitignored, as every
`*_config.json` in this repository is. So are `challenge_created.json`,
`submissions_created.json` and `submissions_evaluated.json`.

Verified end to end on dev on 2026-09-16: create draft → set window → set
deadline → delete by title, with nothing left behind — once with the shipped
defaults and once with `today` for every time. Drafts are named
`e2e-dev-challenge-<timestamp>-<random>`, so one an interrupted run leaves is
easy to spot in the admin draft list and clear with `--title`.
