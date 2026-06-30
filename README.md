# Quarterly Target Contact Enrichment

Every quarter, target contacts from owned accounts, target accounts, ICPs, ABX-tiered accounts, 1st gen prospects, and customer/pipeline accounts are enriched using Clay's LinkedIn data. The pipeline verifies whether each contact is still at the right company in Salesforce, updates titles, reassigns contacts who moved, creates new accounts when needed, and flags contacts whose status is uncertain.

The full list of target contacts is defined by a Salesforce report — [link to Target Contact Enrich List report]. Clay handles the LinkedIn enrichment; everything else (SFDC reads, comparisons, updates, account creation) is automated via GitHub Actions.

---

## How to Run Each Quarter

There are **4 manual steps**. Everything else is automated.

### Step 1: Trigger the push

Go to **[GitHub Actions](https://github.com/JoyCZ26/target-contact-enrichment/actions)** > **Quarterly Contact Enrichment** > **Run workflow**:
- Set **Phase** = `push`
- Leave everything else as default
- Click **Run workflow**

This pulls all target contacts from SFDC, stamps them for this quarter, and generates CSVs to upload to Clay. Download the CSV artifacts from the workflow run.

### Step 2: Upload to Clay and wait

1. Open the Clay workbook (duplicate a fresh one annually — each table has a 50K lifetime row limit)
2. Upload `batch_0_table1.csv` to Table 1 and `batch_1_table2.csv` to Table 2
3. Make sure **Auto-run is ON** in each table's settings
4. Wait for Clay to finish enriching — typically 2-3 days for ~65K contacts
5. Once done, come back and run **stamp-sent**: set **Phase** = `stamp-sent` in GitHub Actions. This marks all enrichment records as "Sent" so the system knows Clay has them.

### Step 3: Preview results

Once Clay has written results back to SFDC (check `Enrichment_Status__c = 'Enriched'` on the enrichment records), run a preview:
- Set **Phase** = `process`
- Check **Preview** = `true`
- Click **Run workflow**

This runs the full comparison logic and prints every contact's result **without changing anything in SFDC**. Review the output in the workflow log — look at the scenario breakdown, spot-check individual contacts.

### Step 4: Process for real

If the preview looks good:
- Set **Phase** = `process`
- Leave **Preview** unchecked
- Click **Run workflow**

This applies all updates to SFDC: updates contacts, creates new accounts, and marks enrichment records as processed. A metrics report is generated automatically at the end and committed to `reports/metrics.html`.

To view metrics standalone anytime: run **Phase** = `metrics`.

---

## What the Automation Does

### Phase A: Push (`--phase push`)
1. Clears `Quarterly_Enrich__c` from last quarter
2. Fetches target contacts from SFDC (mirrors the Target Contact Enrich List report)
3. Stamps `Quarterly_Enrich__c = true` on this quarter's contacts
4. Creates/resets/deletes `Contact_Enrichment__c` staging records as needed
5. Splits contacts into two batches (Clay's 50K table limit) and generates CSVs

### Phase B: Process (`--phase process`)
1. Reads enrichment results from `Contact_Enrichment__c` where `Processing_Status = Unprocessed`
2. Runs the cleaning logic (see below) against every enriched contact
3. Creates new Accounts in SFDC for contacts at companies not yet in Salesforce
4. Bulk-updates all Contacts (accuracy, moved status, titles, account reassignments)
5. Marks enrichment records as Processed
6. Generates accuracy metrics

### Metrics (`--phase metrics`)
Queries SFDC and generates accuracy metrics across 5 account segments: All Owned (excl. Customers), Target Accounts, ICPs (FY27), Tiered Accounts, and Customer Accounts. Commits an HTML report to `reports/metrics.html`.

---

## Cleaning Logic

For each enriched contact, the pipeline uses the LinkedIn data Clay found to determine one of four scenarios:

### Scenario 1 — Still at the same company

The LinkedIn company matches the contact's current SFDC Account. The contact stays where they are.

**Updates:** `Accurate = true`, `Person Has Moved = No` (or `Yes` if LinkedIn shows they've left)

### Scenario 2 — Moved to an existing SFDC Account

The LinkedIn company doesn't match the current account, but it matches a different account already in SFDC.

**Updates:** Contact is reassigned to the matched account. `Accurate = true`, title updated if changed.

### Scenario 3 — Moved to a company not in SFDC

The LinkedIn company doesn't match any account in SFDC, and it's a real company (not freelancer, retired, etc.).

- **If the company has a website domain:** a new Account is created in SFDC and the contact is reassigned to it.
- **If no domain:** the contact is flagged as moved but no account is created (can't create an account without a website).

**Updates:** `Accurate = true`, `Person Has Moved = Yes`, `Left Company = true` (for no-domain), title updated if changed.

### Scenario 4 — No data or invalid

Either Clay couldn't find the contact on LinkedIn, or the contact is at a non-viable company (freelancer, self-employed, retired, career break, etc.), or the company domain is dead (redirects to a parking page).

**Updates:** `Accurate = true`, `Person Has Moved = Yes` (invalid company) or `Uncertain` (no data). If the contact had a LinkedIn URL in SFDC but Clay returned nothing, the enrichment record is flagged for URL Review.

### Matching Signals

Company matching uses four exact-match signals, tried in order (first match wins):

1. **Domain match** — LinkedIn company domain vs SFDC Account website (e.g. `cloudzero.com = cloudzero.com`)
2. **Normalized name match** — Company names with legal suffixes stripped (Inc, LLC, Ltd, Corp, etc.) compared exactly
3. **LinkedIn company URL slug match** — LinkedIn company URL slug from enrichment compared against `Company_LinkedIn_URL__c` and `KN_LinkedIn_URL__c` on the Account
4. **Redirect-resolved domain match** — Follows HTTP redirects to catch cases like `cerence.ai → cerence.com`. Only runs if signals 1-3 all fail (avoids unnecessary HTTP requests). Domains that redirect to generic sinks (google.com, godaddy.com, etc.) are treated as dead.

### Always Backfilled

Regardless of scenario, the pipeline backfills these Contact fields if SFDC is missing them and Clay found them:
- LinkedIn URL
- LinkedIn Location
- Education

---

## Annual Maintenance

Clay tables have a **50K lifetime webhook row limit**. With ~32K contacts per table per quarter, you need to duplicate the Clay workbook roughly once a year:

1. Duplicate the Clay template workbook to get fresh tables with fresh webhook URLs
2. Update webhook URLs in **GitHub repo > Settings > Variables > Actions** (`CLAY_WEBHOOK_BATCH_0` and `CLAY_WEBHOOK_BATCH_1`)

---

## Fiscal Quarter Reference

CloudZero's fiscal year starts February 1. FY = calendar year + 1.

| Quarter | Months | Example |
|---|---|---|
| Q1 | Feb - Apr | FY2027-Q1 = Feb 2026 - Apr 2026 |
| Q2 | May - Jul | FY2027-Q2 = May 2026 - Jul 2026 |
| Q3 | Aug - Oct | FY2027-Q3 = Aug 2026 - Oct 2026 |
| Q4 | Nov - Jan | FY2027-Q4 = Nov 2026 - Jan 2027 |

The script auto-detects the current quarter.
