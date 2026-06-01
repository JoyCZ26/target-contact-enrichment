"""
Post-enrichment metrics report.

Measures target contact accuracy across account segments:
  - All Owned Accounts (Excl. Customers)
  - Target Accounts
  - ICPs
  - Tiered Accounts
  - Customer Accounts
"""

import sys
from collections import defaultdict

from .sfdc import connect_salesforce, query_all


# ── SOQL ───────────────────────────────────────────────────────────────────

METRICS_CONTACT_SOQL = """
SELECT Id, Accurate__c, AccountId,
       Account.Name, Account.Target_Account__c,
       Account.Ideal_Customer_Profile__c,
       Account.ABX_Tier__c, Account_Stage__c
FROM Contact
WHERE Quarterly_Enrich__c = true
""".strip()

METRICS_ACCOUNT_SOQL = """
SELECT Id, Name, Target_Account__c,
       Ideal_Customer_Profile__c, ABX_Tier__c,
       Account_Stage__c
FROM Account
""".strip()


# ── Segment definitions ────────────────────────────────────────────────────

SEGMENTS = [
    {
        "name": "All Owned Accounts\n(Excl. Customers)",
        "short": "all_owned",
        "account_filter": lambda a: (a.get("Account_Stage__c") or "") != "Customer",
    },
    {
        "name": "Target Accounts",
        "short": "target",
        "account_filter": lambda a: a.get("Target_Account__c") is True,
    },
    {
        "name": "ICPs",
        "short": "icps",
        "account_filter": lambda a: a.get("Ideal_Customer_Profile__c") is True,
    },
    {
        "name": "Tiered Accounts",
        "short": "tiered",
        "account_filter": lambda a: (a.get("ABX_Tier__c") or "") in ("Tier 1", "Tier 2", "Tier 3"),
    },
    {
        "name": "Customer Accounts",
        "short": "customers",
        "account_filter": lambda a: (a.get("Account_Stage__c") or "") == "Customer",
    },
]


# ── Metrics calculation ────────────────────────────────────────────────────

def compute_metrics(sf):
    """Query SFDC and compute accuracy metrics across segments.

    Returns list of dicts, one per segment, with:
      - name, target_contacts, accurate_contacts, accuracy_pct
      - total_accounts, accounts_with_tc, pct_accounts_with_tc
      - accounts_90pct, pct_accounts_90pct
    """
    print("Fetching target contacts for metrics...")
    contacts = query_all(sf, METRICS_CONTACT_SOQL)
    print(f"  {len(contacts)} target contacts")

    print("Fetching all accounts for metrics...")
    all_accounts = query_all(sf, METRICS_ACCOUNT_SOQL)
    print(f"  {len(all_accounts)} accounts")

    # Build account lookup
    account_map = {}
    for a in all_accounts:
        account_map[a["Id"]] = {
            "Id": a["Id"],
            "Name": a.get("Name") or "",
            "Target_Account__c": a.get("Target_Account__c"),
            "Ideal_Customer_Profile__c": a.get("Ideal_Customer_Profile__c"),
            "ABX_Tier__c": a.get("ABX_Tier__c"),
            "Account_Stage__c": a.get("Account_Stage__c"),
        }

    # Group contacts by account
    # {account_id: {"total": int, "accurate": int}}
    account_contacts = defaultdict(lambda: {"total": 0, "accurate": 0})
    for c in contacts:
        acct_id = c.get("AccountId")
        if not acct_id:
            continue
        account_contacts[acct_id]["total"] += 1
        if c.get("Accurate__c"):
            account_contacts[acct_id]["accurate"] += 1

        # Attach stage to account map from contact if missing
        if acct_id not in account_map:
            account_map[acct_id] = {
                "Id": acct_id,
                "Account_Stage__c": c.get("Account_Stage__c"),
            }

    # Compute per-segment
    results = []
    for seg in SEGMENTS:
        filt = seg["account_filter"]

        # Filter accounts in this segment
        seg_accounts = {aid: a for aid, a in account_map.items() if filt(a)}

        # Target contacts in this segment
        target_contacts = 0
        accurate_contacts = 0
        accounts_with_tc = 0
        accounts_90pct = 0

        for aid, acct in seg_accounts.items():
            stats = account_contacts.get(aid)
            if not stats or stats["total"] == 0:
                continue

            target_contacts += stats["total"]
            accurate_contacts += stats["accurate"]
            accounts_with_tc += 1

            pct = stats["accurate"] / stats["total"]
            if pct >= 0.90:
                accounts_90pct += 1

        total_accounts = len(seg_accounts)
        accuracy_pct = (accurate_contacts / target_contacts * 100) if target_contacts > 0 else 0
        pct_with_tc = (accounts_with_tc / total_accounts * 100) if total_accounts > 0 else 0
        pct_90 = (accounts_90pct / accounts_with_tc * 100) if accounts_with_tc > 0 else 0

        results.append({
            "name": seg["name"],
            "short": seg["short"],
            "target_contacts": target_contacts,
            "accurate_contacts": accurate_contacts,
            "accuracy_pct": accuracy_pct,
            "total_accounts": total_accounts,
            "accounts_with_tc": accounts_with_tc,
            "pct_accounts_with_tc": pct_with_tc,
            "accounts_90pct": accounts_90pct,
            "pct_accounts_90pct": pct_90,
        })

    return results


# ── Display ────────────────────────────────────────────────────────────────

def print_metrics(results):
    """Print metrics table to stdout."""
    # Column widths
    label_w = 38
    col_w = 22

    # Header
    header = f"{'':>{label_w}}"
    for r in results:
        header += f"  {r['short']:>{col_w}}"
    print("\n" + "=" * len(header))
    print("  POST-ENRICHMENT ACCURACY METRICS")
    print("=" * len(header))

    # Column names
    line = f"{'':>{label_w}}"
    for r in results:
        line += f"  {r['short']:>{col_w}}"
    print(line)
    print("-" * len(line))

    # Row 1: # of Target Contacts
    line = f"{'# of Target Contacts':>{label_w}}"
    for r in results:
        line += f"  {r['target_contacts']:>{col_w},}"
    print(line)

    # Row 2: % Target Contact Accuracy
    line = f"{'% Target Contact Accuracy':>{label_w}}"
    for r in results:
        val = f"{r['accuracy_pct']:.0f}% ({r['accurate_contacts']:,})"
        line += f"  {val:>{col_w}}"
    print(line)

    # Row 3: # of Accounts
    line = f"{'# of Accounts':>{label_w}}"
    for r in results:
        line += f"  {r['total_accounts']:>{col_w},}"
    print(line)

    # Row 4: % Accounts with Target Contacts
    line = f"{'% Accounts with Target Contacts':>{label_w}}"
    for r in results:
        val = f"{r['pct_accounts_with_tc']:.0f}% ({r['accounts_with_tc']:,})"
        line += f"  {val:>{col_w}}"
    print(line)

    # Row 5: % Accounts with >= 90% TC Accuracy
    line = f"{'% Accounts w/ >= 90% TC Accuracy':>{label_w}}"
    for r in results:
        val = f"{r['pct_accounts_90pct']:.0f}% ({r['accounts_90pct']:,})"
        line += f"  {val:>{col_w}}"
    print(line)

    print("=" * len(line))


# ── Entry point ────────────────────────────────────────────────────────────

def run_metrics():
    """Connect to SFDC, compute metrics, and print."""
    sf = connect_salesforce()
    results = compute_metrics(sf)
    print_metrics(results)
    return results
