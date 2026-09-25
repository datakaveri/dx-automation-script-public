# A full challenge round in one sitting — step by step

This is the hands-on walkthrough for testing the **whole** challenge
lifecycle on dev today: create → consumers submit → evaluation → results
announced → everything removed. It is written for someone who has not seen
the scripts before. The reference for every config key is
[`script/challenge/README.md`](../script/challenge/README.md); the general
runbook is [`challenge-scripts.md`](challenge-scripts.md).

## Why the database is involved

The community layer (`dx-community-layer`) stores the submission window as
plain **dates** and compares them by calendar day in Asia/Kolkata:

| step | who does it | when the server allows it |
|---|---|---|
| consumer submits | API | today is within `submission_starts_at … submission_ends_at` |
| admin scores a submission | API | any time the challenge is not CANCELLED |
| PUBLISHED → EVALUATION | server cron | the day **after** `submission_ends_at` |
| announce result → COMPLETED | API | the day **after** `submission_ends_at`, all submissions scored |
| delete a challenge | API | only while DRAFT or SCHEDULED |

So with a challenge created today, submissions and scoring work today, but
the move to EVALUATION and the announcement are always tomorrow's, and once
published the challenge can never be deleted through the API. Two scripts
therefore talk SQL to the challenge database, scoped to one challenge id:
`db_backdate` shifts the dates into the past, `db_purge` removes the rows
at the end.

## 0. One-time setup

```bash
cd ~/dx-automation-script/script/challenge
python3 -m pip install -r requirements.txt        # requests + psycopg2-binary
```

Each script folder has a `*_config.json` (gitignored) next to a
`*_config.json.example`. On this machine they are already filled in for dev.
On a fresh checkout, copy each example and fill in:

| block | in which configs | values (dev) |
|---|---|---|
| `community.base_url` | creation, submission, evaluation | `https://v2.dev.iudx.io/community` |
| `keycloak.url / realm / user_client_id` | creation, submission, evaluation, submission_cleanup | `https://v2.dev.iudx.io/auth`, `iudx-v2`, `frontend-client` |
| `keycloak.admin_client_id / admin_client_secret` | submission, submission_cleanup | the `admin-client` used by `user_creation/consumer` |
| `cos_admin.username / password` | creation, evaluation | the COS admin account |
| `postgres.host / database / user / password` | db_backdate, db_purge | the challenge database (`v2_challenge`, schema `tgdx_dev`) |

Every script takes `--dry-run` (prints what it would do, changes nothing)
and `--help`.

## 1. Create a published challenge

```bash
python3 creation/challenge_creation.py --publish
```

Creates `e2e-dev-challenge-<timestamp>-<random>` with `today` for every
date and publishes it at once. The shipped `sample_rules_and_guidelines.md`
is uploaded as the rules file, and three catalogue datasets from the dev
config are attached.

Expect: `status PUBLISHED` in the summary, and the challenge under
**Published** on `https://dev.mahaagx.iudx.io/challenges`. The id and title
are written to `creation/challenge_created.json`; every later step reads
that file, so nothing is typed twice.

## 2. Submit N solutions

```bash
python3 submission/challenge_submission.py --count 5
```

The server allows one submission per user, so five submissions need five
consumer accounts. The script uses any listed in `consumers.accounts` first
and creates the rest in Keycloak (`e2e-dev-submitter-…@cypress.com`,
password `Consumer@Pass1`). For each account it joins the challenge,
generates a `solution-N.zip` (a CSV of random predictions + README) and a
one-page `writeup-N.pdf`, uploads both through the attachment API, and
submits.

Expect: five `=== consumer #N ===` blocks each ending in `outcome created`,
and `submission/submissions_created.json` listing the accounts, their
submission ids and which accounts were created. In the admin panel the
challenge now shows 5 submissions.

To use real files instead of generated ones, set
`submission.solution_zip_file` and `submission.document_file`.

## 3. Move the dates into the past (SQL)

```bash
python3 db_backdate/challenge_db_backdate.py --dry-run
python3 db_backdate/challenge_db_backdate.py
```

Dry-run first — it prints the exact `UPDATE` statements. The live run then,
in one transaction for this one competition id:

| table | column | becomes |
|---|---|---|
| `competition_timelines` | `submission_starts_at` | 3 days ago (`yesterday-2d`) |
| | `submission_ends_at` | 2 days ago (`yesterday-1d`) |
| | `evaluation_ends_at` | yesterday |
| `competitions` | `published_at` | 3 days ago |
| | `scheduled_publish_at` | NULL (otherwise the cron would republish it) |
| | `status` | `EVALUATION` |
| `competition_submissions` | `created_at`, `updated_at` | 2 days ago |

Expect: `Updated … Committed`, an `=== after ===` block with
`status EVALUATION`, and the challenge under **Evaluation** in the UI, no
longer saying the evaluation is still open.

The dev deployment's cron is not running, which is why the config sets
`status.mode: "direct"` (the script writes the status itself). On a
deployment whose cron runs every minute, `"cron"` commits the dates and
waits for the server to make the move — the more faithful test.

## 4. Score every submission and announce the result

```bash
python3 evaluation/challenge_evaluation.py --announce
```

As the COS admin: lists the challenge's submissions, gives each unscored
one a random score between 50 and 100 with a comment (`evaluate.score.mode`
can be `fixed`, `descending` or `list` instead), reads them back to verify,
then — because `submission_ends_at` is now in the past and everything is
scored — calls announce-result.

Expect: one `=== submission #N ===` block per submission, `Verified 5
score(s)`, `Result announced — challenge is now COMPLETED`, and
`evaluation/submissions_evaluated.json`. In the UI the challenge moves to
**Completed** and the leaderboard shows the scores.

Without `--announce` the scores are stored and the challenge stays in
EVALUATION; run the script again with the flag whenever you want to
announce. Already-scored submissions are skipped on a re-run.

## 5. Remove the consumer accounts

```bash
python3 submission_cleanup/challenge_submission_cleanup.py
```

Deletes from Keycloak exactly the accounts step 2 created (flagged
`created_user` in the handoff); accounts you listed yourself are never
touched. Confirms each id answers 404 and removes
`submissions_created.json`.

## 6. Remove the challenge (SQL)

```bash
python3 db_purge/challenge_db_purge.py --dry-run
python3 db_purge/challenge_db_purge.py
```

Dry-run prints a row count for every table it would touch. The live run
deletes, in one transaction: every table with a `competition_id` column
(submissions, participants, timeline, prize pool, evaluation criteria,
datasets, bookmarks) `WHERE competition_id = <id>`, then the
`competitions` row, then the community layer's own `users` rows for the
created accounts. It verifies every count is zero before committing and
then removes the three handoff files.

Two guards: it refuses a challenge whose title does not start with `e2e-`
(`purge.require_title_prefix`), and it never deletes `users` rows for
accounts that were not created by step 2.

Expect: `Committed`, a `deleted` line with the per-table counts, and the
challenge gone from both the admin panel and the public list.

Not removed: the uploaded files in S3 under
`private/<user>/<competition>/…` — the scripts have no bucket credentials.

## The whole thing, copy-paste

```bash
cd ~/dx-automation-script/script/challenge
python3 creation/challenge_creation.py --publish
python3 submission/challenge_submission.py --count 5
python3 db_backdate/challenge_db_backdate.py --dry-run
python3 db_backdate/challenge_db_backdate.py
python3 evaluation/challenge_evaluation.py --announce
python3 submission_cleanup/challenge_submission_cleanup.py
python3 db_purge/challenge_db_purge.py --dry-run
python3 db_purge/challenge_db_purge.py
```

About five minutes end to end on dev.

## Variations

| want | do |
|---|---|
| a challenge that stays visible under **Published** for a demo | stop after step 1 (or 2) |
| scores without announcing | step 4 without `--announce` |
| only the dates changed, status untouched | `db_backdate … --status-mode none` |
| a different date spread | edit `times.*` in the backdate config — any of `today`, `yesterday`, `2026-10-01`, `today-3d`, `submission_end+7d` |
| act on some other challenge | add `--competition-id <uuid>` (or `--title "<exact title>"`) to any step; the handoff file is then ignored |
| fixed scores for a leaderboard check | `"score": {"mode": "list", "list": [95, 90, 85, 80, 75]}` |
| keep the consumer accounts | skip step 5; `db_purge` still removes their `users` rows unless `purge.users` is `"none"` |

## If something goes wrong

| message | meaning |
|---|---|
| `Submission window has not started` / `has ended` | the challenge's window does not include today — step 2 must run before step 3 |
| `Submission already exists` (409) | that account already submitted; use a fresh `--count` run or set `submission.on_existing: "skip"` |
| `the challenge is COMPLETED; status.require_current allows only …` | you are re-running step 3 on a finished challenge — use `--status-mode none` for dates only |
| `status is still PUBLISHED after … — is the cron running` | `status.mode` is `cron` but this deployment has no cron; use `direct` |
| `Not announcing: submission_ends_at is … and today is …` | step 3 has not been run — the window still ends today |
| `refusing: title '…' does not start with purge.require_title_prefix` | the id points at a challenge the scripts did not create; nothing was deleted |
| `rows survived the delete, rolling back` | a table could not be cleared; nothing was committed |
| 403 on an admin call | the token has no `cos_admin` role |

Every run prints each request and response, and the SQL scripts print each
statement, so the log is the audit trail. `logging.mask_secrets` is off in
the dev configs — turn it on before pasting a log anywhere.
