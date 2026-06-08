import sys
import requests
from simple_salesforce import Salesforce

from .config import (
    SF_CLIENT_ID, SF_CLIENT_SECRET, SF_LOGIN_URL,
    SOQL_BATCH, CLAY_POST_BATCH, TARGET_CONTACTS_SOQL,
)


# ── Auth ────────────────────────────────────────────────────────────────────

def connect_salesforce():
    """OAuth client_credentials flow — same pattern as Instantly sync."""
    resp = requests.post(
        f"{SF_LOGIN_URL}/services/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": SF_CLIENT_ID,
            "client_secret": SF_CLIENT_SECRET,
        },
    )
    if not resp.ok:
        print(f"SFDC auth error {resp.status_code}: {resp.text}", file=sys.stderr)
    resp.raise_for_status()
    token_data = resp.json()
    return Salesforce(
        instance_url=token_data["instance_url"],
        session_id=token_data["access_token"],
    )


# ── Query helpers ───────────────────────────────────────────────────────────

def query_all(sf, soql):
    """Run a SOQL query and page through all results via query_more."""
    result = sf.query(soql)
    records = result["records"]
    while not result["done"]:
        result = sf.query_more(result["nextRecordsUrl"], identifier_is_url=True)
        records.extend(result["records"])
    return records


def _strip_attributes(record):
    """Remove the 'attributes' key SF injects into every record/sub-object."""
    clean = {}
    for k, v in record.items():
        if k == "attributes":
            continue
        if isinstance(v, dict) and "attributes" in v:
            clean[k] = {sk: sv for sk, sv in v.items() if sk != "attributes"}
        else:
            clean[k] = v
    return clean


# ── Step 1a: Clear Quarterly_Enrich__c ──────────────────────────────────────

def clear_quarterly_enrich(sf, dry_run=False):
    """Set Quarterly_Enrich__c = false for every Contact that currently has it true."""
    print("Clearing Quarterly_Enrich__c for all contacts...")
    records = query_all(sf, "SELECT Id FROM Contact WHERE Quarterly_Enrich__c = true")
    count = len(records)
    print(f"  Found {count} contacts with Quarterly_Enrich__c = true")

    if count == 0 or dry_run:
        if dry_run:
            print(f"  [DRY RUN] Would clear Quarterly_Enrich__c on {count} contacts")
        return count

    updates = [{"Id": r["Id"], "Quarterly_Enrich__c": False} for r in records]
    _bulk_update(sf, "Contact", updates)
    print(f"  Cleared Quarterly_Enrich__c on {count} contacts")
    return count


# ── Step 1b: Fetch target contacts from report SOQL ────────────────────────

def fetch_target_contacts(sf):
    """Run the report-equivalent SOQL and return all matching contacts."""
    print("Fetching target contacts (report-equivalent SOQL)...")
    records = query_all(sf, TARGET_CONTACTS_SOQL)
    print(f"  Found {len(records)} target contacts")
    return [_strip_attributes(r) for r in records]


# ── Step 1c: Stamp Quarterly_Enrich__c = true ──────────────────────────────

def stamp_quarterly_enrich(sf, contact_ids, dry_run=False):
    """Set Quarterly_Enrich__c = true for the given Contact IDs."""
    print(f"Stamping Quarterly_Enrich__c = true on {len(contact_ids)} contacts...")
    if dry_run:
        print(f"  [DRY RUN] Would stamp {len(contact_ids)} contacts")
        return

    updates = [{"Id": cid, "Quarterly_Enrich__c": True} for cid in contact_ids]
    _bulk_update(sf, "Contact", updates)
    print(f"  Stamped {len(contact_ids)} contacts")


# ── Step 2: Fetch existing enrichment records ───────────────────────────────

def fetch_existing_enrichments(sf):
    """Return all Contact_Enrichment__c records as {contact_id: enrichment_record}."""
    print("Fetching existing Contact_Enrichment__c records...")
    records = query_all(
        sf,
        "SELECT Id, Contact__c, Contact_Id__c FROM Contact_Enrichment__c"
    )
    # Key by Contact ID for fast lookup
    by_contact = {}
    for r in records:
        contact_id = r.get("Contact__c") or r.get("Contact_Id__c")
        if contact_id:
            by_contact[contact_id] = {
                "Id": r["Id"],
                "Contact__c": r.get("Contact__c"),
                "Contact_Id__c": r.get("Contact_Id__c"),
            }
    print(f"  Found {len(by_contact)} existing enrichment records")
    return by_contact


# ── Step 3: Create / delete enrichment records ─────────────────────────────

def create_enrichment_records(sf, contact_ids, quarter=None, dry_run=False):
    """Create blank Contact_Enrichment__c records for new contacts.
    Uses upsert on Contact_Id__c to be idempotent."""
    print(f"Creating {len(contact_ids)} new enrichment records...")
    if dry_run:
        print(f"  [DRY RUN] Would create {len(contact_ids)} enrichment records")
        return

    records = [
        {"Contact__c": cid, "Contact_Id__c": cid,
         **({"Enrichment_Quarter__c": quarter} if quarter else {})}
        for cid in contact_ids
    ]
    if len(records) <= 200:
        # Use REST API for small batches — bulk API can choke on tiny sets
        for rec in records:
            ext_id = rec["Contact_Id__c"]
            body = {k: v for k, v in rec.items() if k != "Contact_Id__c"}
            sf.Contact_Enrichment__c.upsert(
                f"Contact_Id__c/{ext_id}", body
            )
    else:
        _bulk_upsert(sf, "Contact_Enrichment__c", "Contact_Id__c", records)
    print(f"  Created {len(contact_ids)} enrichment records")


def delete_enrichment_records(sf, enrichment_ids, dry_run=False):
    """Delete Contact_Enrichment__c records for contacts no longer in the report."""
    print(f"Deleting {len(enrichment_ids)} stale enrichment records...")
    if dry_run:
        print(f"  [DRY RUN] Would delete {len(enrichment_ids)} enrichment records")
        return

    _bulk_delete(sf, "Contact_Enrichment__c", enrichment_ids)
    print(f"  Deleted {len(enrichment_ids)} enrichment records")


# ── Step 4: Query back enrichment Record IDs ────────────────────────────────

def fetch_enrichment_ids_for_contacts(sf, contact_ids):
    """Return {contact_id: enrichment_record_id} for the given contacts."""
    print(f"Querying enrichment Record IDs for {len(contact_ids)} contacts...")
    result_map = {}
    for i in range(0, len(contact_ids), SOQL_BATCH):
        batch = contact_ids[i:i + SOQL_BATCH]
        in_clause = ", ".join(f"'{cid}'" for cid in batch)
        records = query_all(
            sf,
            f"SELECT Id, Contact__c FROM Contact_Enrichment__c "
            f"WHERE Contact__c IN ({in_clause})"
        )
        for r in records:
            result_map[r["Contact__c"]] = r["Id"]
    print(f"  Mapped {len(result_map)} enrichment Record IDs")
    return result_map


# ── Step 5: Push to Clay ───────────────────────────────────────────────────

def push_to_clay(contacts, webhook_url, dry_run=False):
    """POST contacts to a Clay webhook — one row per request."""
    total = len(contacts)
    print(f"  Pushing {total} contacts to Clay webhook...")

    if dry_run:
        print(f"  [DRY RUN] Would POST {total} rows to {webhook_url}")
        return

    errors = 0
    for i, contact in enumerate(contacts):
        resp = requests.post(webhook_url, json=contact)
        if not resp.ok:
            errors += 1
            print(
                f"  WARNING: Clay webhook returned {resp.status_code} "
                f"for row {i + 1}: {resp.text[:200]}",
                file=sys.stderr,
            )
        if (i + 1) % 100 == 0 or (i + 1) == total:
            print(f"  Sent {i + 1}/{total} rows")
    if errors:
        print(f"  {errors}/{total} rows failed", file=sys.stderr)


# ── Phase B: Fetch enrichment results ───────────────────────────────────────

ENRICHMENT_FIELDS = """
    Id, Contact__c, Contact_Id__c,
    CE_Company__c, CE_Title__c, CE_Company_Domain__c, CE_Is_Current__c,
    CE_Start_Date__c, CE_End_Date__c,
    LE_Company__c, LE_Title__c, LE_Company_Domain__c, LE_Is_Current__c,
    LinkedIn_Profile_URL__c, LinkedIn_Slug__c,
    Full_Name__c, First_Name__c, Last_Name__c,
    Headline__c, Location_Name__c, Country__c,
    Education_JSON__c,
    Enrichment_Status__c, Processing_Status__c,
    Last_Enriched_Date__c, Enrichment_Quarter__c
""".strip()

CONTACT_PROCESS_FIELDS = """
    Id, FirstName, LastName, Title, Email, AccountId,
    Account.Name, Account.Website, Account.Id,
    LinkedIn_URL__c, LinkedIn_Location__c, Accurate__c, Person_Has_Moved__c,
    Left_Company__c, Education__c, LinkedIn_Title__c
""".strip()


def fetch_unprocessed_enrichments(sf, quarter):
    """Fetch unprocessed enrichment records for a specific quarter that have data.
    Data presence (company, title, or LinkedIn URL) means Clay enriched the row."""
    print(f"Fetching unprocessed enrichments for {quarter} with data...")
    records = query_all(
        sf,
        f"SELECT {ENRICHMENT_FIELDS} FROM Contact_Enrichment__c "
        f"WHERE Processing_Status__c = 'Unprocessed' "
        f"AND Enrichment_Quarter__c = '{quarter}' "
        f"AND (CE_Company__c != null OR CE_Title__c != null "
        f"OR LinkedIn_Profile_URL__c != null)"
    )
    print(f"  Found {len(records)} unprocessed enrichments with data")
    return [_strip_attributes(r) for r in records]


def count_remaining_unprocessed(sf, quarter):
    """Count enrichment records for a specific quarter still unprocessed with no data."""
    result = sf.query(
        f"SELECT COUNT(Id) total FROM Contact_Enrichment__c "
        f"WHERE Processing_Status__c = 'Unprocessed' "
        f"AND Enrichment_Quarter__c = '{quarter}' "
        f"AND CE_Company__c = null AND CE_Title__c = null "
        f"AND LinkedIn_Profile_URL__c = null"
    )
    return result["records"][0]["total"]


def fetch_contacts_by_ids(sf, contact_ids):
    """Fetch Contact records for the given IDs, batched by SOQL_BATCH."""
    print(f"Fetching {len(contact_ids)} contacts for processing...")
    contacts = {}
    for i in range(0, len(contact_ids), SOQL_BATCH):
        batch = contact_ids[i:i + SOQL_BATCH]
        in_clause = ", ".join(f"'{cid}'" for cid in batch)
        records = query_all(
            sf,
            f"SELECT {CONTACT_PROCESS_FIELDS} FROM Contact "
            f"WHERE Id IN ({in_clause})"
        )
        for r in records:
            contacts[r["Id"]] = _strip_attributes(r)
    print(f"  Fetched {len(contacts)} contacts")
    return contacts


def fetch_all_accounts(sf):
    """Fetch all Accounts for domain and name matching."""
    print("Building account lookup maps...")
    records = query_all(
        sf,
        "SELECT Id, Name, Website FROM Account"
    )
    print(f"  Loaded {len(records)} accounts")
    return [_strip_attributes(r) for r in records]


def create_accounts(sf, accounts, dry_run=False):
    """Create new Account records. Returns list of created Account IDs (in order)."""
    print(f"Creating {len(accounts)} new accounts...")
    if dry_run:
        print(f"  [DRY RUN] Would create {len(accounts)} accounts")
        return []

    created_ids = []
    for acct in accounts:
        payload = {"Name": acct["Name"]}
        if acct.get("Website"):
            payload["Website"] = acct["Website"]
        result = sf.Account.create(payload)
        created_ids.append(result["id"])
    print(f"  Created {len(created_ids)} accounts")
    return created_ids


def bulk_update_contacts(sf, updates, dry_run=False):
    """Bulk update Contact records."""
    if not updates:
        return
    print(f"Updating {len(updates)} contacts...")
    if dry_run:
        print(f"  [DRY RUN] Would update {len(updates)} contacts")
        return
    _bulk_update(sf, "Contact", updates)
    print(f"  Updated {len(updates)} contacts")


def mark_enrichments_processed(sf, enrichment_ids, status="Processed", dry_run=False):
    """Mark enrichment records as Processed (or Error)."""
    if not enrichment_ids:
        return
    print(f"Marking {len(enrichment_ids)} enrichments as {status}...")
    if dry_run:
        print(f"  [DRY RUN] Would mark {len(enrichment_ids)} as {status}")
        return
    updates = [{"Id": eid, "Processing_Status__c": status} for eid in enrichment_ids]
    _bulk_update(sf, "Contact_Enrichment__c", updates)
    print(f"  Marked {len(enrichment_ids)} enrichments as {status}")


# ── Bulk helpers ────────────────────────────────────────────────────────────

def _bulk_update(sf, sobject, records):
    """Update records — REST for small batches, Bulk API 2.0 for large."""
    if len(records) <= 200:
        for rec in records:
            record_id = rec["Id"]
            body = {k: v for k, v in rec.items() if k != "Id"}
            sf.__getattr__(sobject).update(record_id, body)
    else:
        CHUNK = 10_000
        for i in range(0, len(records), CHUNK):
            chunk = records[i:i + CHUNK]
            sf.bulk2.__getattr__(sobject).update(chunk, batch_size=CHUNK)


def _bulk_upsert(sf, sobject, external_id_field, records):
    """Bulk API 2.0 upsert on an external ID field."""
    CHUNK = 10_000
    for i in range(0, len(records), CHUNK):
        chunk = records[i:i + CHUNK]
        sf.bulk2.__getattr__(sobject).upsert(chunk, external_id_field, batch_size=CHUNK)


def _bulk_delete(sf, sobject, record_ids):
    """Delete records — REST for small batches, Bulk API 2.0 for large."""
    if len(record_ids) <= 200:
        for rid in record_ids:
            sf.__getattr__(sobject).delete(rid)
    else:
        CHUNK = 10_000
        delete_records = [{"Id": rid} for rid in record_ids]
        for i in range(0, len(delete_records), CHUNK):
            chunk = delete_records[i:i + CHUNK]
            sf.bulk2.__getattr__(sobject).delete(chunk, batch_size=CHUNK)
