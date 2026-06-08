# Quarterly Contact Enrichment Pipeline

Automates the quarterly process of enriching ~63K target contacts using LinkedIn data via Clay, then updating Salesforce based on whether each contact is still at the right company.

## How It Works (Big Picture)

Every quarter, we need to verify that our target contacts are still at the companies we have them listed under in Salesforce. This pipeline:

1. **Pushes contacts to Clay** for LinkedIn enrichment
2. **Waits for Clay** to look up each contact on LinkedIn and write results back to Salesforce
3. **Previews the results** so you can review before anything changes
4. **Compares LinkedIn data against Salesforce** to determine if each contact is still at the right company
5. **Updates Salesforce** with the results (accurate, moved, uncertain, etc.)
6. **Reports metrics** on contact accuracy across account segments

---

## Quarterly Workflow (Step by Step)

This is the recommended order of operations each quarter:

1. **Dispatch `push`** — sends contacts to Clay
2. *(wait for Clay to enrich — hours to days)*
3. **Dispatch `process` with `preview` checked** — runs all comparison logic, prints per-contact scenario results, writes nothing. Review the output.
4. If results look good → **Dispatch `process`** (without preview) — applies all updates to SFDC
5. Results print automatically at the end with accuracy metrics

---

## Phases

### Phase A: Push (`--phase push`)

Sends this quarter's target contacts to Clay for enrichment.

**What it does, step by step:**

1. **Detects the fiscal quarter** automatically (e.g. `2027-Q2`). CloudZero's fiscal year starts in February.

2. **Clears the Quarterly Enrich flag** on all contacts. This resets from last quarter.

3. **Fetches this quarter's target contacts** using SOQL that mirrors the Target Contact Enrich List report. The criteria:
   - Contact is a CFO, CTO, VP of Infrastructure, or Target Contact
   - AND their account qualifies: Target Account, ICP, ICP (FY27), High/Medium fit, Tiered (ABX Tier 1-3), or has an AE assigned — and is NOT qualified out
   - OR their account is a Customer, Pipeline, or Churned Customer

4. **Stamps `Quarterly_Enrich__c = true`** on all contacts in this quarter's batch.

5. **Reconciles enrichment records:**
   - **New contacts** (first time in the report) get a new `Contact_Enrichment__c` record created
   - **Returning contacts** (were in last quarter's report too) keep their existing record but it gets reset: data fields cleared, `Processing_Status__c` set back to `Unprocessed`, `Enrichment_Quarter__c` stamped with current quarter
   - **Dropped contacts** (were in last quarter but not this one) get their enrichment record deleted

6. **Splits contacts into two batches** using the `Enrichment_Batch__c` formula field (deterministic 50/50 split based on the contact's Salesforce ID). This is because Clay tables have a 50K lifetime webhook row limit.

7. **POSTs each contact individually** to the Clay webhook (one per table). Each payload includes:
   - ContactId, EnrichmentRecordId, FirstName, LastName, Email, Title
   - LinkedInURL, AccountName, AccountId, AccountWebsite

**After Phase A, Clay takes over.** Clay's enrichment columns auto-run and look up each contact on LinkedIn. When done, Clay's "Update Record" column writes the results back to the `Contact_Enrichment__c` record in Salesforce.

---

### Phase B: Preview (`--phase process --preview`)

Runs all comparison logic and prints per-contact results **without writing anything to SFDC**. Use this to review before committing.

**Output shows:**
- Scenario breakdown (counts per scenario)
- Every contact listed under their scenario with:
  - Contact name and current SFDC account
  - LinkedIn company and domain (for S2/S3)
  - Whether a new account would be created (S3)
  - Whether the experience has an end date

**Always run preview first.** This is your chance to spot-check the matching logic before it touches real data.

---

### Phase B: Process (`--phase process`)

Reads enrichment results from Salesforce and updates contacts based on what LinkedIn says. Only runs on the current fiscal quarter's records.

**What it does, step by step:**

1. **Polls every 10 minutes** for enrichment records that Clay has written data to. A record is "ready" when it has a company name, title, or LinkedIn URL populated by Clay.

2. **For each batch of ready records**, runs the comparison logic:

   **Step 1: Does LinkedIn match the current SFDC account?**
   - Compares LinkedIn company domain against the SFDC account website domain (exact match)
   - Falls back to exact normalized name match (strips legal suffixes like Inc, LLC, Ltd — then exact compare)
   - **Match + no end date** = `Accurate = true`, `Person Has Moved = No`
   - **Match + end date** = `Accurate = true`, `Person Has Moved = Yes`

   **Step 2: Does LinkedIn match ANY SFDC account?**
   - Searches all accounts by exact domain, then exact normalized name
   - **Found** = Reassigns the contact's AccountId. `Accurate = true`, `Person Has Moved` based on end date.

   **Step 3: Company not in SFDC — is it a real company?**
   - Filters out non-companies: "Self-employed", "Freelance", "Retired", "Career Break", etc.
   - **Real company** = Creates a new Account in SFDC, reassigns contact. `Accurate = true`, `Person Has Moved` based on end date.
   - **Not a real company** = `Accurate = false`, `Person Has Moved = Yes`

   **No enrichment data at all:**
   - Clay couldn't find or enrich them. `Accurate = true`, `Person Has Moved = Uncertain`
   - If the contact had a LinkedIn URL in SFDC but Clay returned nothing, the enrichment record gets flagged as `URL Review` (the URL might be bad/outdated)

3. **Backfills missing fields** — LinkedIn URL, LinkedIn Location, and Education on the Contact if SFDC is missing them and Clay found them.

4. **Updates Title** on the Contact if LinkedIn shows a different title.

5. **Keeps polling** until one of two things happens:
   - All enrichment records for this quarter have been processed (remaining = 0)
   - No new records have appeared for **1 hour** (6 consecutive empty checks). Clay is assumed done. All remaining blank records get marked `Uncertain`.

6. **Prints accuracy metrics** when complete.

---

### Metrics (`--phase metrics`)

Prints a post-enrichment accuracy report. Runs automatically at the end of Phase B, or standalone.

Measures across 5 segments: All Owned Accounts (excl. Customers), Target Accounts, ICPs, Tiered Accounts, Customer Accounts.

For each: target contact count, % accuracy, account count, % accounts with target contacts, % accounts with 90%+ accuracy.

---

## How to Run

### From GitHub Actions (recommended)

Go to **Actions > Quarterly Contact Enrichment > Run workflow**:

| Input | Options | Description |
|---|---|---|
| Phase | `push` / `process` / `metrics` | Which phase to run |
| Dry run | true / false | Log only, no changes |
| Test limit | 0 (no limit) or N | Limit to N contacts for testing |
| Preview | true / false | Run Phase B logic without writing |

### From command line

```bash
# Phase A: push contacts to Clay
python -m src.main --phase push

# Phase B: preview results (no writes)
python -m src.main --phase process --preview

# Phase B: apply results
python -m src.main --phase process

# Metrics only
python -m src.main --phase metrics

# Testing
python -m src.main --phase push --test 5
python -m src.main --phase push --dry-run
```

---

## Guardrails and Safety

### Preview before applying
**Always run `--preview` first.** It shows every contact's scenario and planned updates without touching SFDC. Review the output, then run without preview to apply.

### Quarter isolation
Every enrichment record is stamped with the fiscal quarter (e.g. `2027-Q2`). Phase B only processes records matching the current quarter. Old quarters are invisible — no cross-quarter contamination.

### Company matching is exact only
Matching uses three exact signals — no fuzzy matching, no partial word matching:
1. **Exact domain match** (cloudzero.com = cloudzero.com)
2. **Exact normalized name match** (CloudZero, Inc. = CloudZero — legal suffixes stripped)
3. **Exact LinkedIn URL domain match**

This prevents false positives like "Irish American Partnership" matching "American Systems."

### Dry run mode
Use `--dry-run` for Phase A to log everything without making changes.

### Test mode
Use `--test 5` to limit Phase A to 5 contacts. Skips the clear/stamp steps so it doesn't disrupt the full population.

### Idempotent enrichment records
Enrichment records use `Contact_Id__c` as an external ID. Running Phase A twice won't create duplicates.

### Processing status
Each enrichment record tracks: `Unprocessed` > `Processed` / `Error` / `URL Review`. Phase B only touches `Unprocessed` records, so re-running is safe.

---

## Important Gotchas

### Clay table webhook limits
Each Clay table has a **50K lifetime webhook row limit**. With ~32K contacts per table per quarter, you'll need to **duplicate the Clay workbook and update webhook URLs** every 1-2 quarters.

To update: **GitHub repo > Settings > Variables > Actions** > update `CLAY_WEBHOOK_BATCH_0` and `CLAY_WEBHOOK_BATCH_1`.

### Clay auto-run must be ON
The Clay tables must have **Auto-run enabled** in table settings. Otherwise enrichment columns won't fire when rows come in.

### Clay Update Record mapping
Clay's "Update Record" column writes enrichment results back to `Contact_Enrichment__c` in SFDC. The field mappings are configured in the Clay table, not in this code. If a field isn't mapped in Clay, the script won't see it.

### Phase B timeout
Phase B gives up after **1 hour of no new records**. If Clay is still running (e.g. rate-limited), dispatch `process` again — it picks up where it left off since it only processes `Unprocessed` records.

### Don't run Phase A twice in the same quarter
Phase A resets enrichment records. Running it twice would clear data Clay already wrote. If you need to re-push, delete the enrichment records first.

### Contacts without LinkedIn URLs or titles
Contacts missing a LinkedIn URL won't be found by Clay's "Enrich Person". Contacts missing a Title won't match target contact criteria and won't be in the population. Handle pre-enrichment separately before running this pipeline.

### The Accurate field
`Accurate__c` gets cleared to `false` when Phase B processes a batch, then re-set based on comparison results. If Phase B crashes mid-batch, some contacts may show `Accurate = false` until the next run.

---

## GitHub Secrets and Variables

**Secrets** (Settings > Secrets > Actions):
| Name | Description |
|---|---|
| `SF_CLIENT_ID` | Connected App consumer key |
| `SF_CLIENT_SECRET` | Connected App consumer secret |
| `SF_LOGIN_URL` | `https://cloudzero.my.salesforce.com` |

**Variables** (Settings > Variables > Actions):
| Name | Description |
|---|---|
| `CLAY_WEBHOOK_BATCH_0` | Webhook URL for Clay table 1 |
| `CLAY_WEBHOOK_BATCH_1` | Webhook URL for Clay table 2 |

---

## Salesforce Objects and Fields

### Contact fields used
| Field | Purpose |
|---|---|
| `Quarterly_Enrich__c` | Checkbox — true for this quarter's target contacts |
| `Enrichment_Batch__c` | Formula (0 or 1) — splits contacts for Clay's two tables |
| `Accurate__c` | Result: is the contact at the right company? |
| `Person_Has_Moved__c` | Result: No / Yes / Uncertain |
| `LinkedIn_URL__c` | Backfilled from Clay if missing |
| `LinkedIn_Location__c` | Backfilled from Clay if missing |
| `Education__c` | Backfilled from Clay if missing |
| `Title` | Updated if LinkedIn shows a different title |
| `LinkedIn_Title__c` | Updated alongside Title |
| `AccountId` | Reassigned if contact moved (Scenarios 2 and 3) |

### Contact_Enrichment__c (staging object)
| Field | Purpose |
|---|---|
| `Contact__c` | Master-Detail to Contact |
| `Contact_Id__c` | External ID for upsert (= Contact ID) |
| `Enrichment_Quarter__c` | Which quarter this record belongs to (e.g. `2027-Q2`) |
| `Processing_Status__c` | Unprocessed / Processed / Error / URL Review |
| `Record_Created_Date__c` | Formula — mirrors CreatedDate for report filtering |
| `CE_Company__c` | LinkedIn company name (written by Clay) |
| `CE_Title__c` | LinkedIn title (written by Clay) |
| `CE_Company_Domain__c` | LinkedIn company domain (written by Clay) |
| `CE_End_Date__c` | End date of experience (written by Clay) |
| `LinkedIn_Profile_URL__c` | LinkedIn URL found by Clay |
| + 60 other fields | Written by Clay, available for future use |

---

## Monitoring a Run

### In GitHub Actions
Watch the workflow run log. It prints progress for every step, including per-contact scenario details.

### In Salesforce
Use the **"Contact Enrichment Processing Status"** report (in Public Reports). Filter by `Quarterly Enrich = true` to see this quarter's contacts and their processing status.

Or run this SOQL in Developer Console:
```sql
SELECT Processing_Status__c, COUNT(Id)
FROM Contact_Enrichment__c
WHERE Enrichment_Quarter__c = '2027-Q2'
GROUP BY Processing_Status__c
```

---

## Fiscal Quarter Reference

CloudZero's fiscal year starts February 1. FY = calendar year + 1.

| Quarter | Months | Example |
|---|---|---|
| Q1 | Feb - Apr | 2027-Q1 = Feb 2026 - Apr 2026 |
| Q2 | May - Jul | 2027-Q2 = May 2026 - Jul 2026 |
| Q3 | Aug - Oct | 2027-Q3 = Aug 2026 - Oct 2026 |
| Q4 | Nov - Jan | 2027-Q4 = Nov 2026 - Jan 2027 |

The script auto-detects the current quarter — you never need to specify it.
