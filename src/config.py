import os

# ── Salesforce ──────────────────────────────────────────────────────────────
SF_CLIENT_ID = os.environ["SF_CLIENT_ID"]
SF_CLIENT_SECRET = os.environ["SF_CLIENT_SECRET"]
SF_LOGIN_URL = os.environ.get("SF_LOGIN_URL", "https://login.salesforce.com")

# ── Clay ────────────────────────────────────────────────────────────────────
CLAY_WEBHOOK_BATCH_0 = os.environ.get("CLAY_WEBHOOK_BATCH_0", "")
CLAY_WEBHOOK_BATCH_1 = os.environ.get("CLAY_WEBHOOK_BATCH_1", "")

# ── Runtime ─────────────────────────────────────────────────────────────────
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
SOQL_BATCH = 200          # max IDs per SOQL IN clause
CLAY_POST_BATCH = 200     # rows per webhook POST to Clay


# ── Fiscal quarter ─────────────────────────────────────────────────────────
# CloudZero fiscal year starts Feb 1, FY is calendar year + 1.
# Q1: Feb-Apr, Q2: May-Jul, Q3: Aug-Oct, Q4: Nov-Jan
# Format: "2027-Q2"

def get_fiscal_quarter():
    """Return the current fiscal quarter label, e.g. '2027-Q2'."""
    from datetime import date
    today = date.today()
    month = today.month
    year = today.year

    if month >= 2:
        fy = year + 1
    else:
        fy = year

    if month in (2, 3, 4):
        q = 1
    elif month in (5, 6, 7):
        q = 2
    elif month in (8, 9, 10):
        q = 3
    else:  # 11, 12, 1
        q = 4

    return f"{fy}-Q{q}"

# ── Report reference ────────────────────────────────────────────────────────
# Human-readable report: 00OVN000004T0IT2A0 (Target Contact Enrich List)
# The SOQL below replicates the report's filter logic:
#   (1 OR 2 OR 3 OR 4) AND (((5 OR 6 OR 8 OR 9 OR 11 OR 12 OR 13) AND 7) OR 10)
#
#   1  = CFO Contact
#   2  = CTO Contact
#   3  = VP of Infrastructure Contact
#   4  = Target Contact
#   5  = Target Account
#   6  = Ideal Customer Profile
#   7  = Qualified Out Date is null
#   8  = Account Fit = High, Medium
#   9  = ABX Tier = Tier 1, 2, 3
#   10 = Account Stage = Customer, Pipeline, Churned Customer
#   11 = Ideal Customer Profile (FY27)
#   12 = Account Executive Owner is not blank
#   13 = 1st Gen Prospect
TARGET_CONTACTS_SOQL = """
SELECT Id, FirstName, LastName, Email, Title,
       LinkedIn_URL__c, Enrichment_Batch__c, Contact_ID_18__c,
       Account.Name, Account.Id, Account.Website
FROM Contact
WHERE (
    CFO_Contact__c = true
    OR CTO_Contact__c = true
    OR VP_of_Infrastructure_Contact__c = true
    OR Target_Contact__c = true
)
AND (
    (
        (
            Account.Target_Account__c = true
            OR Account.Ideal_Customer_Profile__c = true
            OR Account.Account_Fit__c IN ('High', 'Medium')
            OR Account.ABX_Tier__c IN ('Tier 1', 'Tier 2', 'Tier 3')
            OR Account.Ideal_Customer_Profile_FY27__c = true
            OR Account.Account_Executive_Owner__c != null
            OR Account.X1st_Gen_Prospect__c = true
        )
        AND Account.Qualified_Out_Date__c = null
    )
    OR Account_Stage__c IN ('Customer', 'Pipeline', 'Churned Customer')
)
""".strip()
