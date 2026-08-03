"""
Post-enrichment metrics report.

Measures target contact accuracy across account segments:
  - All Owned Accounts (Excl. Customers)
  - Target Accounts
  - ICPs
  - Tiered Accounts
  - Customer Accounts
"""

import os
import sys
from collections import defaultdict
from datetime import date

from .config import get_fiscal_quarter
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
       Ideal_Customer_Profile__c,
       ABX_Tier__c, Account_Stage__c, Account_Executive_Owner__c
FROM Account
""".strip()


# ── Segment definitions ────────────────────────────────────────────────────

SEGMENTS = [
    {
        "name": "All Owned Accounts\n(Excl. Customers)",
        "short": "all_owned",
        "account_filter": lambda a: (a.get("Account_Stage__c") or "") != "Customer" and a.get("Account_Executive_Owner__c"),
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
            "Account_Executive_Owner__c": a.get("Account_Executive_Owner__c"),
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


# ── HTML report ───────────────────────────────────────────────────────────

def write_html_report(results, quarter, out_path="reports/metrics.html"):
    """Write an HTML metrics report, appending each quarter's results."""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    fy_year = quarter.split("-")[0]
    q_label = quarter.split("-")[1]
    run_date = date.today().strftime("%B %d, %Y")

    # Build this quarter's table HTML
    segment_headers = "".join(
        f"<th>{r['name'].replace(chr(10), '<br>')}</th>" for r in results
    )

    def count_row(label, key):
        cells = "".join(f"<td>{r[key]:,}</td>" for r in results)
        return f"<tr><td class='label'>{label}</td>{cells}</tr>"

    def pct_row(label, count_key, pct_key):
        cells = "".join(
            f"<td>{r[pct_key]:.0f}% ({r[count_key]:,})</td>" for r in results
        )
        return f"<tr><td class='label'>{label}</td>{cells}</tr>"

    table_html = f"""
    <div class="quarter-section">
      <h2>FY{fy_year} {q_label} — Quarterly Contact Enrichment</h2>
      <p class="run-date">Run date: {run_date}</p>
      <table>
        <thead>
          <tr><th class="label"></th>{segment_headers}</tr>
        </thead>
        <tbody>
          {count_row("# of Target Contacts", "target_contacts")}
          {pct_row("% Target Contact Accuracy", "accurate_contacts", "accuracy_pct")}
          {count_row("# of Accounts", "total_accounts")}
          {pct_row("% Accounts with Target Contacts", "accounts_with_tc", "pct_accounts_with_tc")}
          {pct_row("% Accounts w/ &ge; 90% TC Accuracy", "accounts_90pct", "pct_accounts_90pct")}
        </tbody>
      </table>
    </div>
"""

    # Check if file exists and has previous quarters
    if os.path.exists(out_path):
        with open(out_path, "r") as f:
            existing = f.read()
        if f"FY{fy_year} {q_label}" in existing:
            # Replace existing quarter section
            import re
            pattern = rf'<div class="quarter-section">\s*<h2>FY{fy_year} {q_label}.*?</div>'
            existing = re.sub(pattern, table_html.strip(), existing, flags=re.DOTALL)
            with open(out_path, "w") as f:
                f.write(existing)
            print(f"  Updated {quarter} in {out_path}")
            return
        # Insert new quarter after <body>
        existing = existing.replace(
            "<!-- QUARTER_SECTIONS -->",
            f"<!-- QUARTER_SECTIONS -->\n{table_html}",
        )
        with open(out_path, "w") as f:
            f.write(existing)
        print(f"  Added {quarter} to {out_path}")
        return

    # Create new file
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Contact Enrichment Metrics — CloudZero</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 40px auto; max-width: 1100px; color: #1a1a1a; background: #f8f9fa; }}
  h1 {{ color: #0f172a; border-bottom: 3px solid #3b82f6; padding-bottom: 12px; }}
  h2 {{ color: #1e40af; margin-top: 40px; }}
  .run-date {{ color: #64748b; font-size: 14px; margin-top: -8px; }}
  table {{ border-collapse: collapse; width: 100%; margin: 16px 0 32px; }}
  th, td {{ padding: 10px 16px; text-align: right; border: 1px solid #e2e8f0; }}
  th {{ background: #1e40af; color: white; font-weight: 600; }}
  td.label, th.label {{ text-align: left; background: #f1f5f9; color: #1a1a1a; font-weight: 600; min-width: 260px; }}
  tbody tr:nth-child(even) {{ background: #f8fafc; }}
  tbody tr:hover {{ background: #eff6ff; }}
</style>
</head>
<body>
<h1>Contact Enrichment Metrics</h1>
<!-- QUARTER_SECTIONS -->
{table_html}
</body>
</html>"""

    with open(out_path, "w") as f:
        f.write(html)
    print(f"  Created {out_path}")


# ── Entry point ────────────────────────────────────────────────────────────

def run_metrics():
    """Connect to SFDC, compute metrics, print, and write HTML report."""
    sf = connect_salesforce()
    quarter = get_fiscal_quarter()
    results = compute_metrics(sf)
    print_metrics(results)
    write_html_report(results, quarter)
    return results
