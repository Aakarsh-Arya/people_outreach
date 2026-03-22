# Manual Workflow Terminal Instructions

## Environment

Open PowerShell in the repo root:

```powershell
cd C:\Users\91836\Documents\email_automation
.\.venv\Scripts\Activate.ps1
```

## Core Commands

Export pending rows for one cohort into `manual_workflow/research_queue.md`:

```powershell
python manual_workflow/manual_workflow.py export --tab cohort_2018 --batch-size 5
```

Export all pending rows across all cohorts in `cohorts/manifest.json`:

```powershell
python manual_workflow/manual_workflow.py export --all-pending --batch-size 5
```

Filter research export by domain or exact sheet columns:

```powershell
python manual_workflow/manual_workflow.py export --tab cohort_2018 --domain finance --batch-size 5
python manual_workflow/manual_workflow.py export --tab cohort_2018 --filter Confidence_Level=low,unconfirmed --filter Graduation_Year=2018 --batch-size 5
python manual_workflow/manual_workflow.py export --tab cohort_2018 --names "Aditya Sharma" --filter PRIMARY_DOMAIN=analytics --batch-size 5
```

Ingest pasted `<PROFILE>` and `<EMAIL>` blocks from `manual_workflow/ingest_queue.md`:

```powershell
python manual_workflow/manual_workflow.py ingest
```

`ingest` now batches all sheet writes first. If the batch write fails, queue files are left untouched so the same paste can be retried safely.

Force re-process all ingest blocks, ignoring previous `INGEST_RUN_COMPLETE` markers:

```powershell
python manual_workflow/manual_workflow.py ingest --force
```

Re-batch the remaining research queue:

```powershell
python manual_workflow/manual_workflow.py rebatch --batch-size 5
```

Export email generation batches for `RESEARCH_DONE` rows:

```powershell
python manual_workflow/manual_workflow.py export-email --tab cohort_2013 --batch-size 5
```

`export-email` now preserves the full `Enrichment_Notes` payload in `manual_workflow/email_queue.md`, including hook fields embedded in the pasted profile block.

Export email batches across all cohorts:

```powershell
python manual_workflow/manual_workflow.py export-email --all-pending --batch-size 5
```

Filter email export by domain or exact sheet columns:

```powershell
python manual_workflow/manual_workflow.py export-email --tab cohort_2013 --domain consulting --batch-size 5
python manual_workflow/manual_workflow.py export-email --tab cohort_2013 --filter Confidence_Level=high,very_high --filter Email_Source=people_api --batch-size 5
```

Show top-level workflow status:

```powershell
python manual_workflow/manual_workflow.py status
```

## Detailed Status Queries

Show actionable rows for a cohort tab:

```powershell
python manual_workflow/manual_workflow.py status --detail cohort_2018
```

Filter to one status bucket:

```powershell
python manual_workflow/manual_workflow.py status --detail cohort_2018 --only PENDING
python manual_workflow/manual_workflow.py status --detail cohort_2018 --only RESEARCH_DONE
python manual_workflow/manual_workflow.py status --detail cohort_2018 --only FAILED_PARSE
python manual_workflow/manual_workflow.py status --detail cohort_2018 --only SKIP_GUESSED_EMAIL
python manual_workflow/manual_workflow.py status --detail cohort_2018 --only EMAIL_DONE
python manual_workflow/manual_workflow.py status --detail cohort_2018 --only SENT
```

Export only pending names for research copy/paste:

```powershell
python manual_workflow/manual_workflow.py status --detail cohort_2018 --export-names
```

Check what is still pending in `ingest_queue.md`:

```powershell
python manual_workflow/manual_workflow.py status --ingest-queue
```

## Review Queue Commands

List review items grouped by reason:

```powershell
python manual_workflow/manual_workflow.py review --list
```

Move one review item back into `ingest_queue.md` for retry:

```powershell
python manual_workflow/manual_workflow.py review --retry "Aditya Sharma"
```

Clear one review item:

```powershell
python manual_workflow/manual_workflow.py review --clear "Aditya Sharma"
```

Clear the entire review queue:

```powershell
python manual_workflow/manual_workflow.py review --clear-all
```

## Reconcile Commands

Reconcile all queue files against the sheet:

```powershell
python manual_workflow/manual_workflow.py reconcile
```

Reconcile a specific queue:

```powershell
python manual_workflow/manual_workflow.py reconcile --queue research
python manual_workflow/manual_workflow.py reconcile --queue ingest
python manual_workflow/manual_workflow.py reconcile --queue email
```

## Normal Manual Loop

1. Run `export` for the target cohort.
2. Copy one batch from `manual_workflow/research_queue.md`.
3. Paste `manual_workflow/system_prompt_research.md` into the external LLM.
4. Paste the batch block.
5. Paste the returned `<PROFILE>` or `<EMAIL>` blocks into `manual_workflow/ingest_queue.md`.
6. Run `python manual_workflow/manual_workflow.py ingest`.
7. If anything is flagged, inspect `manual_workflow/review_queue.md` and use the `review` commands.
8. Use `status --detail cohort_YYYY` to inspect remaining actionable rows.

After a successful ingest, `manual_workflow/email_queue.md` is auto-refreshed for the affected tabs.

## Normal Email Generation Loop

1. Run `export-email` for the target cohort.
2. Copy one batch from `manual_workflow/email_queue.md`.
3. Paste `manual_workflow/system_prompt_email.md` into the external LLM.
4. Paste the batch block.
5. Paste the returned `<EMAIL>` blocks into `manual_workflow/ingest_queue.md`.
6. Run `python manual_workflow/manual_workflow.py ingest`.
7. `email_queue.md` is auto-refreshed after successful ingest. Run `export-email` again only if you want a full rebuild across more tabs.

## What The Hardening Now Does Automatically

- Duplicate RECONCILE_IDs inside one ingest paste are pre-deduplicated before sheet writes.
- Dirty `Confidence_Level` values already in the sheet are normalized on the read side before comparison.
- Same-confidence profile pastes overwrite the sheet because the paste is treated as newer research.
- If a profile upgrade lands on an `EMAIL_DONE` row, `Subject` and `Body` are cleared and the status is reset before downstream regeneration.
- Rows with `Email_Source=guessed` or blank are forced to `SKIP_GUESSED_EMAIL` and must not receive email generation.
- Rows with `Email_Source=ambiguous` are treated the same as guessed rows in the manual path.
- Successful ingest rewrites `manual_workflow/ingest_queue.md` back to the template with a `_Last ingested:` timestamp header.
- Queue reconciliation runs before `export`, `export-email`, and `ingest`, and can also be run directly with `reconcile`.
- Ingest now uses one batched sheet-write phase via `sheets_helper.batch_write_rows(...)` before any queue-file cleanup is applied.
- Successful ingest auto-refreshes `manual_workflow/email_queue.md` for tabs that received writes.

## Useful Verification Commands

```powershell
python -m py_compile manual_workflow/manual_workflow.py
Select-String -Path manual_workflow/manual_workflow.py -Pattern "_normalize_confidence"
Select-String -Path manual_workflow/manual_workflow.py -Pattern "_reconcile_queue_file"
Select-String -Path manual_workflow/manual_workflow.py -Pattern "DEDUP"
Select-String -Path manual_workflow/manual_workflow.py -Pattern "SKIP_GUESSED_EMAIL"
Select-String -Path manual_workflow/manual_workflow.py -Pattern "STATUS_RESET"
Select-String -Path manual_workflow/manual_workflow.py -Pattern "INGEST_RUN_COMPLETE"
Select-String -Path manual_workflow/manual_workflow.py -Pattern "def cmd_review"
Select-String -Path manual_workflow/manual_workflow.py -Pattern "def cmd_export_email"
Select-String -Path manual_workflow/manual_workflow.py -Pattern "--detail"
Select-String -Path manual_workflow/manual_workflow.py -Pattern "TODO LIST"
Select-String -Path manual_workflow/manual_workflow.py -Pattern "INGEST_CLEANUP"
Select-String -Path manual_workflow/manual_workflow.py -Pattern "BATCH_WRITE"
Select-String -Path manual_workflow/manual_workflow.py -Pattern "EMAIL_QUEUE_REFRESH"
Select-String -Path manual_workflow/manual_workflow.py -Pattern "ambiguous"
Select-String -Path sheets_helper.py -Pattern "def batch_write_rows"
Select-String -Path .gitignore -Pattern "manual_workflow/logs"
Select-String -Path .gitignore -Pattern "email_queue"
python -m py_compile manual_workflow/cleanup_confidence.py
python -m py_compile cohort_runner.py
```

## One-Time Confidence Cleanup

Dry run first:

```powershell
python manual_workflow/cleanup_confidence.py --all
python manual_workflow/cleanup_confidence.py --all --dry-run
```

The cleanup script defaults to dry-run unless `--write` is added. To apply the normalized values:

```powershell
python manual_workflow/cleanup_confidence.py --all --write
```

## Do Not Do

- Do not edit `manual_workflow/system_prompt_research.md` or `manual_workflow/system_prompt_email.md` through the agent.
- Do not run live automated Phase 2 sheet batches from the agent session.
- Do not touch rows already marked `SENT`.

## Query System

Filter rows from any cohort tab by any column combination and output in different formats.

### Basic usage
python manual_workflow/manual_workflow.py query --tab cohort_2015 --status SKIP_GUESSED_EMAIL

### All filter flags (combine freely — AND logic)
--tab TAB                        Single cohort tab
--all-tabs                       All cohort tabs from manifest
--status STATUS1,STATUS2         Filter by STATUS column
--email-source guessed,ambiguous Filter by Email_Source column
--confidence low,unconfirmed     Filter by Confidence_Level column
--gender M/F/Unknown             Filter by GENDER column
--batch-year 2015                Filter by Graduation_Year column
--domain "ANALYTICS & DATA"      Filter by PRIMARY_DOMAIN (contains, case-insensitive)
--name "Shiva,Aditya"            Fuzzy name filter (comma-separated)

### Output modes
--output table                   Default: name/status/email_source/confidence/email
--output fields NAME,Email,LinkedIn_URL   Cherry-pick specific columns
--output research --batch-size 3 Format as BATCH blocks ready for Kimi/Qwen
--output reconcile               Print ready-to-run reconcile-email commands

### Common queries
# All guessed/ambiguous rows with research done (ready to manually verify)
python manual_workflow/manual_workflow.py query --tab cohort_2015 --email-source guessed,ambiguous --status RESEARCH_DONE

# All low/unconfirmed confidence rows (candidates for re-research)
python manual_workflow/manual_workflow.py query --tab cohort_2015 --confidence low,unconfirmed --output research --batch-size 3

# Get LinkedIn URLs for specific people
python manual_workflow/manual_workflow.py query --tab cohort_2015 --name "Shiva,Aditya" --output fields NAME,LinkedIn_URL,Email

# Generate reconcile commands for manually verified people
python manual_workflow/manual_workflow.py query --tab cohort_2015 --email-source guessed,ambiguous --status RESEARCH_DONE --output reconcile

---

## Manual Email Verification (reconcile-email)

When you have manually verified a guessed/ambiguous email via Gmail or other means,
run this to atomically update Email_Source → manual_verified and STATUS → PENDING.

python manual_workflow/manual_workflow.py reconcile-email --tab cohort_2015 --name "Shivasubramaniam S"

# Multiple names at once
python manual_workflow/manual_workflow.py reconcile-email --tab cohort_2015 --name "Shivasubramaniam S,Aditya S Rajadhyax,Chandan Kumar"

All overrides are logged to manual_workflow/manual_overrides.md with timestamp.

Optional status override:

By default, `reconcile-email` sets `STATUS` to `PENDING`.
Use `--status RESEARCH_DONE` when research is already complete and the row should skip straight to email generation.
Valid values are `PENDING`, `RESEARCH_DONE`, and `SKIP_GUESSED_EMAIL`.

python manual_workflow/manual_workflow.py reconcile-email --tab cohort_2015 --name "Someone" --status PENDING
python manual_workflow/manual_workflow.py reconcile-email --tab cohort_2015 --name "Shivasubramaniam S,Aditya S Rajadhyax,Chandan Kumar,Hari Prasad Sakthivel,Gaurav Singh Gehlot" --status RESEARCH_DONE

---

## Reset Helpers

Use these only for dry-run review first. `SENT` rows are never modified.

Reset generated emails back to `RESEARCH_DONE` without clearing `Email` or `Email_Source`:

```powershell
python manual_workflow/manual_workflow.py reset-email --tab cohort_2015 --domain consulting
python manual_workflow/manual_workflow.py reset-email --tab cohort_2015 --name "Aditya Sharma"
python manual_workflow/manual_workflow.py reset-email --tab cohort_2015 --filter Confidence_Level=low --write
```

Reset research output back to `PENDING` without clearing `Email` or `Email_Source`:

```powershell
python manual_workflow/manual_workflow.py reset-research --tab cohort_2015 --domain analytics
python manual_workflow/manual_workflow.py reset-research --tab cohort_2015 --name "Aditya Sharma"
python manual_workflow/manual_workflow.py reset-research --tab cohort_2015 --filter Confidence_Level=low,unconfirmed --write
```
