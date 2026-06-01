import argparse
import sys
from collections import defaultdict

from .config import CLAY_WEBHOOK_BATCH_0, CLAY_WEBHOOK_BATCH_1, DRY_RUN
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
    fetch_contacts_by_ids,
    fetch_all_accounts,
    create_accounts,
    bulk_update_contacts,
    mark_enrichments_processed,
)
from .matching import build_account_maps, extract_domain
from .enrichment import process_enrichment


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

def phase_push(dry_run=False):
    """Phase A: Read report, manage enrichment records, push to Clay."""
    sf = connect_salesforce()

    # ── Step 1: Reset and re-stamp Quarterly_Enrich__c ──────────────────
    clear_quarterly_enrich(sf, dry_run=dry_run)
    contacts = fetch_target_contacts(sf)
    contact_ids = [c["Id"] for c in contacts]
    stamp_quarterly_enrich(sf, contact_ids, dry_run=dry_run)

    # ── Step 2: Compare with existing enrichment records ────────────────
    existing = fetch_existing_enrichments(sf)
    target_set = set(contact_ids)
    existing_set = set(existing.keys())

    new_ids = target_set - existing_set
    returning_ids = target_set & existing_set
    dropped_ids = existing_set - target_set

    print(f"\nEnrichment record reconciliation:")
    print(f"  New contacts (need enrichment record):     {len(new_ids)}")
    print(f"  Returning contacts (already have record):  {len(returning_ids)}")
    print(f"  Dropped contacts (record to delete):       {len(dropped_ids)}")

    # ── Step 3: Create new / delete stale enrichment records ────────────
    if new_ids:
        create_enrichment_records(sf, list(new_ids), dry_run=dry_run)

    if dropped_ids:
        enrichment_ids_to_delete = [existing[cid]["Id"] for cid in dropped_ids]
        delete_enrichment_records(sf, enrichment_ids_to_delete, dry_run=dry_run)

    # ── Step 4: Get enrichment Record IDs for all target contacts ───────
    enrichment_map = fetch_enrichment_ids_for_contacts(sf, contact_ids)

    # ── Step 5: Split by batch and push to Clay ─────────────────────────
    batch_0 = []
    batch_1 = []
    missing_enrichment = 0

    for contact in contacts:
        cid = contact["Id"]
        enrichment_id = enrichment_map.get(cid)
        if not enrichment_id:
            missing_enrichment += 1
            continue
        payload = build_clay_payload(contact, enrichment_id)
        if contact.get("Enrichment_Batch__c") == 0:
            batch_0.append(payload)
        else:
            batch_1.append(payload)

    if missing_enrichment:
        print(
            f"\n  WARNING: {missing_enrichment} contacts missing enrichment Record ID — skipped",
            file=sys.stderr,
        )

    print(f"\nClay push:")
    print(f"  Batch 0: {len(batch_0)} contacts → Webhook A")
    print(f"  Batch 1: {len(batch_1)} contacts → Webhook B")

    push_to_clay(batch_0, CLAY_WEBHOOK_BATCH_0, dry_run=dry_run)
    push_to_clay(batch_1, CLAY_WEBHOOK_BATCH_1, dry_run=dry_run)

    print("\n✓ Phase A complete — contacts pushed to Clay")


# ── Phase B ─────────────────────────────────────────────────────────────────

def phase_process(dry_run=False):
    """Phase B: Read enrichment results, compare, update contacts."""
    sf = connect_salesforce()

    # ── Step 1: Fetch unprocessed enrichments ───────────────────────────
    enrichments = fetch_unprocessed_enrichments(sf)
    if not enrichments:
        print("No unprocessed enrichments found. Nothing to do.")
        return

    # ── Step 2: Fetch referenced contacts ───────────────────────────────
    contact_ids = list(set(
        e["Contact__c"] for e in enrichments if e.get("Contact__c")
    ))
    contacts = fetch_contacts_by_ids(sf, contact_ids)

    # ── Step 2b: Clear Accurate__c for all contacts being processed ────
    clear_updates = [{"Id": cid, "Accurate__c": False} for cid in contact_ids]
    print(f"Clearing Accurate__c for {len(clear_updates)} contacts...")
    bulk_update_contacts(sf, clear_updates, dry_run=dry_run)

    # ── Step 3: Build account lookup maps ───────────────────────────────
    accounts = fetch_all_accounts(sf)
    domain_map, name_map = build_account_maps(accounts)

    # ── Step 4: Process each enrichment → resolve scenario ──────────────
    contact_updates = {}       # {contact_id: {field: value}}
    new_accounts_needed = {}   # {domain: {Name, Website}} — deduplicated
    scenario_to_contacts = defaultdict(list)  # for account assignment after creation
    scenario_counts = defaultdict(int)
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

        if needs_url_review:
            url_review_ids.append(enrichment["Id"])

        if updates:
            updates["Id"] = contact_id
            contact_updates[contact_id] = updates

        # Scenario 3: track new accounts to create (dedup by domain)
        if scenario == 3 and new_account:
            domain = extract_domain(new_account.get("Website") or "")
            key = domain or new_account["Name"].lower()
            if key not in new_accounts_needed:
                new_accounts_needed[key] = new_account
            # Track which contacts need this new account
            scenario_to_contacts[key].append(contact_id)

        processed_ids.append(enrichment["Id"])

    print(f"\nScenario breakdown:")
    print(f"  S1 — Still at same company:     {scenario_counts[1]}")
    print(f"  S2 — Moved to existing account: {scenario_counts[2]}")
    print(f"  S3 — Moved to new company:      {scenario_counts[3]}")
    print(f"  S4 — No data / uncertain:       {scenario_counts[4]}")
    print(f"  Errors:                          {len(error_ids)}")

    # ── Step 5: Create new Accounts (Scenario 3) ───────────────────────
    if new_accounts_needed:
        print(f"\nCreating {len(new_accounts_needed)} new accounts for Scenario 3...")
        new_account_list = list(new_accounts_needed.items())

        if not dry_run:
            account_records = [acct for _, acct in new_account_list]
            created_ids = create_accounts(sf, account_records, dry_run=dry_run)

            # Assign new AccountId to the contacts that need it
            for i, (key, _) in enumerate(new_account_list):
                new_account_id = created_ids[i]
                for contact_id in scenario_to_contacts.get(key, []):
                    if contact_id in contact_updates:
                        contact_updates[contact_id]["AccountId"] = new_account_id
        else:
            print(f"  [DRY RUN] Would create {len(new_accounts_needed)} accounts")

    # ── Step 6: Bulk update Contacts ────────────────────────────────────
    if contact_updates:
        updates_list = list(contact_updates.values())
        bulk_update_contacts(sf, updates_list, dry_run=dry_run)

    # ── Step 7: Mark enrichments as Processed ───────────────────────────
    mark_enrichments_processed(sf, processed_ids, status="Processed", dry_run=dry_run)
    if error_ids:
        mark_enrichments_processed(sf, error_ids, status="Error", dry_run=dry_run)
    if url_review_ids:
        mark_enrichments_processed(sf, url_review_ids, status="URL Review", dry_run=dry_run)

    # ── Summary ─────────────────────────────────────────────────────────
    print(f"\n✓ Phase B complete")
    print(f"  Contacts updated:      {len(contact_updates)}")
    print(f"  Accounts created:      {len(new_accounts_needed)}")
    print(f"  Enrichments processed: {len(processed_ids)}")
    print(f"  Enrichments errored:   {len(error_ids)}")
    print(f"  URL review needed:     {len(url_review_ids)}")


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Quarterly Contact Enrichment")
    parser.add_argument(
        "--phase",
        choices=["push", "process"],
        required=True,
        help="push = Phase A (send to Clay), process = Phase B (process results)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Log what would happen without making changes",
    )
    args = parser.parse_args()

    dry_run = args.dry_run or DRY_RUN

    if dry_run:
        print("=" * 60)
        print("  DRY RUN MODE — no changes will be made")
        print("=" * 60)

    if args.phase == "push":
        phase_push(dry_run=dry_run)
    elif args.phase == "process":
        phase_process(dry_run=dry_run)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
