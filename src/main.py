import argparse
import sys
import time
from collections import defaultdict

from .config import CLAY_WEBHOOK_BATCH_0, CLAY_WEBHOOK_BATCH_1, DRY_RUN, get_fiscal_quarter
from .sfdc import (
    connect_salesforce,
    clear_quarterly_enrich,
    fetch_target_contacts,
    stamp_quarterly_enrich,
    fetch_existing_enrichments,
    create_enrichment_records,
    delete_enrichment_records,
    fetch_enrichment_ids_for_contacts,
    push_to_clay,
    fetch_unprocessed_enrichments,
    count_remaining_unprocessed,
    query_all,
    fetch_contacts_by_ids,
    fetch_all_accounts,
    create_accounts,
    bulk_update_contacts,
    mark_enrichments_processed,
)
from .matching import build_account_maps, extract_domain
from .enrichment import process_enrichment
from .metrics import run_metrics


def build_clay_payload(contact, enrichment_record_id):
    """Build the row payload for a Clay webhook POST."""
    account = contact.get("Account") or {}
    return {
        "ContactId": contact["Id"],
        "EnrichmentRecordId": enrichment_record_id,
        "FirstName": contact.get("FirstName") or "",
        "LastName": contact.get("LastName") or "",
        "Email": contact.get("Email") or "",
        "Title": contact.get("Title") or "",
        "LinkedInURL": contact.get("LinkedIn_URL__c") or "",
        "AccountName": account.get("Name") or "",
        "AccountId": account.get("Id") or "",
        "AccountWebsite": account.get("Website") or "",
    }


# ── Phase A ─────────────────────────────────────────────────────────────────

def phase_push(dry_run=False, test_limit=None):
    """Phase A: Read report, manage enrichment records, push to Clay."""
    sf = connect_salesforce()
    quarter = get_fiscal_quarter()
    print(f"\nFiscal quarter: {quarter}\n")

    if test_limit:
        print(f"*** TEST MODE — limited to {test_limit} contacts ***\n")

    # ── Step 1: Reset and re-stamp Quarterly_Enrich__c ──────────────────
    if not test_limit:
        clear_quarterly_enrich(sf, dry_run=dry_run)
    contacts = fetch_target_contacts(sf)
    if test_limit:
        contacts = contacts[:test_limit]
    contact_ids = [c["Id"] for c in contacts]
    if not test_limit:
        stamp_quarterly_enrich(sf, contact_ids, dry_run=dry_run)

    # ── Step 2: Compare with existing enrichment records ────────────────
    existing = fetch_existing_enrichments(sf)
    target_set = set(contact_ids)
    existing_set = set(existing.keys())

    new_ids = target_set - existing_set
    returning_ids = target_set & existing_set
    dropped_ids = existing_set - target_set

    # Split returning into already processed this quarter vs needs reset
    already_done_ids = {
        cid for cid in returning_ids
        if existing[cid].get("Processing_Status__c") in ("Processed", "Sent")
        and existing[cid].get("Enrichment_Quarter__c") == quarter
    }
    needs_reset_ids = returning_ids - already_done_ids

    print(f"\nEnrichment record reconciliation:")
    print(f"  New contacts (need enrichment record):     {len(new_ids)}")
    print(f"  Returning — already done this quarter:     {len(already_done_ids)}")
    print(f"  Returning — needs reset:                   {len(needs_reset_ids)}")
    print(f"  Dropped contacts (record to delete):       {len(dropped_ids)}")

    # ── Step 3: Create new / delete stale enrichment records ────────────
    if new_ids:
        create_enrichment_records(sf, list(new_ids), quarter=quarter, dry_run=dry_run)

    if dropped_ids:
        enrichment_ids_to_delete = [existing[cid]["Id"] for cid in dropped_ids]
        delete_enrichment_records(sf, enrichment_ids_to_delete, dry_run=dry_run)

    # ── Step 3b: Reset returning enrichment records that need re-processing
    if needs_reset_ids:
        reset_updates = [
            {
                "Id": existing[cid]["Id"],
                "Processing_Status__c": "Unprocessed",
                "Enrichment_Quarter__c": quarter,
                "CE_Company__c": None,
                "CE_Title__c": None,
                "CE_Company_Domain__c": None,
                "CE_End_Date__c": None,
                "LinkedIn_Profile_URL__c": None,
            }
            for cid in needs_reset_ids
        ]
        print(f"Resetting {len(reset_updates)} returning enrichment records...")
        from .sfdc import _bulk_update
        if not dry_run:
            _bulk_update(sf, "Contact_Enrichment__c", reset_updates)
        else:
            print(f"  [DRY RUN] Would reset {len(reset_updates)} enrichment records")

    # ── Step 4: Get enrichment Record IDs for all target contacts ───────
    enrichment_map = fetch_enrichment_ids_for_contacts(sf, contact_ids)

    # ── Step 5: Split by batch and push to Clay ─────────────────────────
    # Skip contacts already processed this quarter
    batch_0 = []
    batch_1 = []
    missing_enrichment = 0
    skipped_done = 0

    for contact in contacts:
        cid = contact["Id"]
        if cid in already_done_ids:
            skipped_done += 1
            continue
        enrichment_id = enrichment_map.get(cid)
        if not enrichment_id:
            missing_enrichment += 1
            continue
        payload = build_clay_payload(contact, enrichment_id)
        if contact.get("Enrichment_Batch__c") == 0:
            batch_0.append(payload)
        else:
            batch_1.append(payload)

    if skipped_done:
        print(f"\n  Skipped {skipped_done} contacts already processed this quarter")

    if missing_enrichment:
        print(
            f"\n  WARNING: {missing_enrichment} contacts missing enrichment Record ID — skipped",
            file=sys.stderr,
        )

    print(f"\nClay push:")
    print(f"  Batch 0: {len(batch_0)} contacts → Webhook A")
    print(f"  Batch 1: {len(batch_1)} contacts → Webhook B")

    push_to_clay(batch_0, CLAY_WEBHOOK_BATCH_0, sf=sf, dry_run=dry_run)
    push_to_clay(batch_1, CLAY_WEBHOOK_BATCH_1, sf=sf, dry_run=dry_run)

    print("\n✓ Phase A complete — contacts pushed to Clay")


# ── Phase B ─────────────────────────────────────────────────────────────────

POLL_INTERVAL = 600  # seconds between checks (10 minutes)


SCENARIO_LABELS = {
    1: "S1 — Still at same company",
    2: "S2 — Moved to existing account",
    3: "S3 — Moved to new company",
    4: "S4 — No data / uncertain",
}


def _process_batch(sf, quarter, dry_run=False, preview=False):
    """Process one batch of enrichment records for a specific quarter.

    If preview=True, runs all logic and prints results but does NOT write
    any updates to SFDC. Returns (processed_count, remaining_count).
    """

    # ── Fetch ready enrichments ────────────────────────────────────────
    enrichments = fetch_unprocessed_enrichments(sf, quarter)
    if not enrichments:
        remaining = count_remaining_unprocessed(sf, quarter)
        return 0, remaining

    # ── Fetch referenced contacts ──────────────────────────────────────
    contact_ids = list(set(
        e["Contact__c"] for e in enrichments if e.get("Contact__c")
    ))
    contacts = fetch_contacts_by_ids(sf, contact_ids)

    # ── Build account lookup maps ──────────────────────────────────────
    accounts = fetch_all_accounts(sf)
    domain_map, name_map = build_account_maps(accounts)

    # ── Process each enrichment → resolve scenario ─────────────────────
    contact_updates = {}
    new_accounts_needed = {}
    scenario_to_contacts = defaultdict(list)
    scenario_counts = defaultdict(int)
    scenario_details = defaultdict(list)  # {scenario: [{contact details}]}
    processed_ids = []
    error_ids = []
    url_review_ids = []

    for enrichment in enrichments:
        contact_id = enrichment.get("Contact__c")
        contact = contacts.get(contact_id)

        if not contact:
            print(f"  WARNING: Contact {contact_id} not found — skipping", file=sys.stderr)
            error_ids.append(enrichment["Id"])
            continue

        try:
            scenario, updates, new_account, needs_url_review = process_enrichment(
                enrichment, contact, domain_map, name_map
            )
        except Exception as e:
            print(f"  ERROR processing {contact_id}: {e}", file=sys.stderr)
            error_ids.append(enrichment["Id"])
            continue

        scenario_counts[scenario] += 1

        # Track per-contact details for review
        contact_name = f"{contact.get('FirstName') or ''} {contact.get('LastName') or ''}".strip()
        sfdc_account = (contact.get("Account") or {}).get("Name") or "(no account)"
        li_company = enrichment.get("CE_Company__c") or "(no data)"
        li_domain = enrichment.get("CE_Company_Domain__c") or ""
        detail = {
            "contact_id": contact_id,
            "contact_name": contact_name,
            "sfdc_account": sfdc_account,
            "li_company": li_company,
            "li_domain": li_domain,
            "updates": updates,
        }
        if new_account:
            detail["new_account"] = new_account
        scenario_details[scenario].append(detail)

        if needs_url_review:
            url_review_ids.append(enrichment["Id"])

        if updates:
            updates["Id"] = contact_id
            contact_updates[contact_id] = updates

        if scenario == 3 and new_account:
            domain = extract_domain(new_account.get("Website") or "")
            key = domain or new_account["Name"].lower()
            if key not in new_accounts_needed:
                new_accounts_needed[key] = new_account
            scenario_to_contacts[key].append(contact_id)

        processed_ids.append(enrichment["Id"])

    # ── Print scenario breakdown ──────────────────────────────────────
    print(f"\n  Scenario breakdown:")
    print(f"    S1 — Still at same company:     {scenario_counts[1]}")
    print(f"    S2 — Moved to existing account: {scenario_counts[2]}")
    print(f"    S3 — Moved to new company:      {scenario_counts[3]}")
    print(f"    S4 — No data / uncertain:       {scenario_counts[4]}")
    print(f"    Errors:                          {len(error_ids)}")

    # ── Print per-contact details ─────────────────────────────────────
    print(f"\n  Per-contact results:")
    for scenario in sorted(scenario_details.keys()):
        label = SCENARIO_LABELS.get(scenario, f"S{scenario}")
        print(f"\n    {label}:")
        for d in scenario_details[scenario]:
            line = f"      {d['contact_name']} ({d['sfdc_account']})"
            if scenario in (2, 3):
                line += f" → {d['li_company']}"
                if d.get('li_domain'):
                    line += f" ({d['li_domain']})"
            if scenario == 3 and d.get('new_account'):
                line += " [NEW ACCOUNT]"
            if d.get('updates', {}).get('Person_Has_Moved__c') == 'Yes':
                line += " [HAS END DATE]"
            print(line)

    # ── Preview mode: stop here ───────────────────────────────────────
    if preview:
        print(f"\n  *** PREVIEW MODE — no changes written to SFDC ***")
        print(f"  Run without --preview to apply these updates.")
        remaining = count_remaining_unprocessed(sf, quarter)
        return len(processed_ids), remaining

    # ── Clear Accurate__c for contacts being processed ─────────────────
    clear_updates = [{"Id": cid, "Accurate__c": False} for cid in contact_ids]
    print(f"\n  Clearing Accurate__c for {len(clear_updates)} contacts...")
    bulk_update_contacts(sf, clear_updates, dry_run=dry_run)

    # ── Create new Accounts (Scenario 3) ───────────────────────────────
    if new_accounts_needed:
        print(f"  Creating {len(new_accounts_needed)} new accounts...")
        new_account_list = list(new_accounts_needed.items())

        if not dry_run:
            account_records = [acct for _, acct in new_account_list]
            created_ids = create_accounts(sf, account_records, dry_run=dry_run)

            for i, (key, _) in enumerate(new_account_list):
                new_account_id = created_ids[i]
                for contact_id in scenario_to_contacts.get(key, []):
                    if contact_id in contact_updates:
                        contact_updates[contact_id]["AccountId"] = new_account_id
        else:
            print(f"  [DRY RUN] Would create {len(new_accounts_needed)} accounts")

    # ── Bulk update Contacts ───────────────────────────────────────────
    if contact_updates:
        updates_list = list(contact_updates.values())
        bulk_update_contacts(sf, updates_list, dry_run=dry_run)

    # ── Mark enrichments ───────────────────────────────────────────────
    mark_enrichments_processed(sf, processed_ids, status="Processed", dry_run=dry_run)
    if error_ids:
        mark_enrichments_processed(sf, error_ids, status="Error", dry_run=dry_run)
    if url_review_ids:
        mark_enrichments_processed(sf, url_review_ids, status="URL Review", dry_run=dry_run)

    print(f"  Batch done: {len(processed_ids)} processed, {len(error_ids)} errors, {len(url_review_ids)} URL review")

    remaining = count_remaining_unprocessed(sf, quarter)
    return len(processed_ids), remaining


MAX_STALE_CHECKS = 6  # 6 consecutive empty checks × 10 min = 1 hour


def phase_process(dry_run=False, preview=False):
    """Phase B: Poll and process enrichment results until Clay is done.

    If preview=True, runs logic once and prints results without writing.
    Otherwise polls every 10 minutes until Clay is done.

    Only processes records for the current fiscal quarter.
    """
    sf = connect_salesforce()
    quarter = get_fiscal_quarter()
    print(f"\nFiscal quarter: {quarter}\n")

    if preview:
        print("=" * 60)
        print("  PREVIEW MODE — analyzing only, no changes will be made")
        print("=" * 60)
        _process_batch(sf, quarter, dry_run=True, preview=True)
        return

    total_processed = 0
    stale_count = 0
    iteration = 0

    while True:
        iteration += 1
        print(f"\n{'='*60}")
        print(f"  Processing iteration {iteration} ({quarter})")
        print(f"{'='*60}")

        processed, remaining = _process_batch(sf, quarter, dry_run=dry_run)
        total_processed += processed

        print(f"\n  Total processed so far: {total_processed}")
        print(f"  Remaining (no data):    {remaining}")

        # All done — nothing left
        if remaining == 0:
            print(f"\n✓ All {quarter} enrichment records processed")
            break

        # New records were processed — reset stale counter
        if processed > 0:
            stale_count = 0
        else:
            stale_count += 1
            print(f"  No new records this check ({stale_count}/{MAX_STALE_CHECKS})")

        # Clay appears done — no new records for 1 hour
        if stale_count >= MAX_STALE_CHECKS:
            print(f"\n  No new records for {MAX_STALE_CHECKS * POLL_INTERVAL // 60} minutes — Clay is done")
            print(f"  Marking {remaining} remaining records as Uncertain...")

            # Fetch remaining blank records for this quarter
            remaining_records = query_all(
                sf,
                f"SELECT Id, Contact__c FROM Contact_Enrichment__c "
                f"WHERE Processing_Status__c = 'Unprocessed' "
                f"AND Enrichment_Quarter__c = '{quarter}' "
                f"AND CE_Company__c = null AND CE_Title__c = null "
                f"AND LinkedIn_Profile_URL__c = null"
            )

            # Update contacts
            contact_updates = [
                {"Id": r["Contact__c"], "Person_Has_Moved__c": "Uncertain", "Accurate__c": True}
                for r in remaining_records if r.get("Contact__c")
            ]
            if contact_updates:
                bulk_update_contacts(sf, contact_updates, dry_run=dry_run)

            # Mark enrichment records as Processed
            remaining_ids = [r["Id"] for r in remaining_records]
            mark_enrichments_processed(sf, remaining_ids, status="Processed", dry_run=dry_run)

            total_processed += len(remaining_ids)
            break

        print(f"  Waiting {POLL_INTERVAL // 60} minutes before next check...")
        time.sleep(POLL_INTERVAL)

    # ── Final summary + metrics ────────────────────────────────────────
    print(f"\n✓ Phase B complete ({quarter}) — {total_processed} total enrichments processed")
    run_metrics()


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Quarterly Contact Enrichment")
    parser.add_argument(
        "--phase",
        choices=["push", "process", "metrics"],
        required=True,
        help="push = Phase A, process = Phase B, metrics = accuracy report only",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Log what would happen without making changes",
    )
    parser.add_argument(
        "--test",
        type=int,
        default=None,
        help="Test mode: limit to N contacts (skips clear/stamp)",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        default=False,
        help="Preview mode: run Phase B logic and show results without writing",
    )
    args = parser.parse_args()

    dry_run = args.dry_run or DRY_RUN

    if dry_run:
        print("=" * 60)
        print("  DRY RUN MODE — no changes will be made")
        print("=" * 60)

    if args.phase == "push":
        phase_push(dry_run=dry_run, test_limit=args.test)
    elif args.phase == "process":
        phase_process(dry_run=dry_run, preview=args.preview)
    elif args.phase == "metrics":
        run_metrics()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
