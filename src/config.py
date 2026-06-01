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

# ── Report reference ────────────────────────────────────────────────────────
# Human-readable report: 00OVN000004T0IT2A0 (Target Contact Enrich List)
# The SOQL below replicates the report's filter logic:
#   (1 OR 2 OR 3 OR 4) AND (((5 OR 6 OR 8 OR 9) AND 7) OR 10)
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
        )
        AND Account.Qualified_Out_Date__c = null
    )
    OR Account_Stage__c IN ('Customer', 'Pipeline', 'Churned Customer')
)
""".strip()
