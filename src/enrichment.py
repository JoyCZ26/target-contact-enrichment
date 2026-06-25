"""
Contact enrichment processor.

For each enriched contact, compares LinkedIn data against SFDC and determines:
  - Is the contact still at the same company?
  - Have they moved to a known SFDC account?
  - Have they moved to a new company we need to create?
  - Is the data unusable?

Uses CE_ (Current Experience) fields first. If empty, falls back to
LE_ (Latest Experience) fields. Uses Is_Current to determine if the
person is still at that company.

Decision tree:
  Step 1: Does LinkedIn company match current SFDC Account? (domain, then name)
    → Match + is current:    Accurate, Person Has Moved = No
    → Match + not current:   Accurate, Person Has Moved = Yes

  Step 2: No match → search all SFDC Accounts for LinkedIn company
    → Found + is current:    Reassign, Accurate, Person Has Moved = No
    → Found + not current:   Reassign, Accurate, Person Has Moved = Yes

  Step 3: Not in SFDC → is it a real company?
    → Viable + is current:   Create Account, reassign, Accurate, Person Has Moved = No
    → Viable + not current:  Create Account, reassign, Accurate, Person Has Moved = Yes
    → Not viable:            Accurate = false, Person Has Moved = Yes

  No enrichment data at all:
    → Accurate = true, Person Has Moved = Uncertain

Always: backfill LinkedIn URL, LinkedIn Location, and Education on Contact if missing.
"""

from .matching import (
    match_company_to_current_account,
    lookup_company_in_sfdc,
    is_invalid_company,
    is_domain_dead,
    extract_domain,
)


def process_enrichment(enrichment, contact, domain_map, name_map, linkedin_slug_map=None):
    """Process a single enrichment record against its contact.

    Returns:
        (scenario, contact_updates, new_account_info, needs_url_review) where:
          scenario:         int (1-4)
          contact_updates:  dict of Contact field updates (may be empty)
          new_account_info: dict for account creation (scenario 3 only) or None
          needs_url_review: bool — True if had LinkedIn URL but enrichment failed
    """
    contact_updates = {}
    new_account_info = None

    # ── Extract enrichment data (CE_ first, fall back to LE_) ──────────
    ce_company = enrichment.get("CE_Company__c") or ""
    ce_title = enrichment.get("CE_Title__c") or ""
    ce_domain = enrichment.get("CE_Company_Domain__c") or ""

    le_company = enrichment.get("LE_Company__c") or ""
    le_title = enrichment.get("LE_Title__c") or ""
    le_domain = enrichment.get("LE_Company_Domain__c") or ""

    # CE_ has data → person is currently at that company
    # CE_ empty, LE_ has data → person left their last company
    ce_company_url = enrichment.get("CE_Company_URL__c") or ""
    le_company_url = enrichment.get("LE_Company_URL__c") or ""

    if ce_company:
        li_company = ce_company
        li_title = ce_title
        li_domain = ce_domain
        li_company_url = ce_company_url
        is_current = True
    elif le_company:
        li_company = le_company
        li_title = le_title
        li_domain = le_domain
        li_company_url = le_company_url
        is_current = False
    else:
        li_company = ""
        li_title = ""
        li_domain = ""
        li_company_url = ""
        is_current = False

    li_url = enrichment.get("LinkedIn_Profile_URL__c") or ""
    li_education = enrichment.get("Education_JSON__c") or ""
    li_location = enrichment.get("Location_Name__c") or ""
    li_headline = enrichment.get("Headline__c") or ""

    # Determine if enrichment data exists
    has_enrichment_data = bool(li_company or li_url or li_title)

    # Current SFDC data
    account = contact.get("Account") or {}
    sfdc_account_domain = account.get("Website") or ""
    sfdc_account_name = account.get("Name") or ""
    sfdc_title = contact.get("Title") or ""
    sfdc_linkedin_url = contact.get("LinkedIn_URL__c") or ""
    sfdc_education = contact.get("Education__c") or ""
    sfdc_location = contact.get("LinkedIn_Location__c") if "LinkedIn_Location__c" in contact else None

    # ── Always: backfill LinkedIn URL if missing ────────────────────────
    if li_url and not sfdc_linkedin_url:
        contact_updates["LinkedIn_URL__c"] = li_url

    # ── Always: backfill Education if missing ───────────────────────────
    if li_education and not sfdc_education:
        contact_updates["Education__c"] = li_education

    # ── Always: backfill LinkedIn Location if missing ───────────────────
    if li_location and sfdc_location is None:
        contact_updates["LinkedIn_Location__c"] = li_location

    # ── No enrichment data → uncertain ─────────────────────────────────
    if not has_enrichment_data:
        contact_updates["Person_Has_Moved__c"] = "Uncertain"
        contact_updates["Accurate__c"] = True
        # Had a LinkedIn URL in SFDC but Clay returned nothing → URL may be bad
        needs_url_review = bool(sfdc_linkedin_url)
        return 4, contact_updates, None, needs_url_review

    if not li_company:
        contact_updates["Person_Has_Moved__c"] = "Uncertain"
        contact_updates["Accurate__c"] = True
        return 4, contact_updates, None, False

    # ── Helper: apply title updates ─────────────────────────────────────
    def _apply_title():
        if li_title and li_title.lower() != sfdc_title.lower():
            contact_updates["Title"] = li_title
            contact_updates["LinkedIn_Title__c"] = li_title

    # ── Step 1: Does LinkedIn company match current SFDC Account? ───────
    sfdc_account_linkedin_urls = [
        u for u in (
            account.get("Company_LinkedIn_URL__c") or "",
            account.get("KN_LinkedIn_URL__c") or "",
        ) if u
    ]
    is_same = match_company_to_current_account(
        li_domain, li_company, sfdc_account_domain, sfdc_account_name,
        linkedin_company_url=li_company_url,
        sfdc_account_linkedin_urls=sfdc_account_linkedin_urls,
    )

    if is_same:
        contact_updates["Accurate__c"] = True
        _apply_title()

        if is_current:
            contact_updates["Person_Has_Moved__c"] = "No"
        else:
            contact_updates["Person_Has_Moved__c"] = "Yes"

        return 1, contact_updates, None, False

    # ── Step 2: Search all SFDC Accounts for the LinkedIn company ───────
    matched_account = lookup_company_in_sfdc(
        li_domain, li_company, domain_map, name_map,
        linkedin_company_url=li_company_url,
        linkedin_slug_map=linkedin_slug_map,
    )

    if matched_account:
        contact_updates["AccountId"] = matched_account["Id"]
        contact_updates["Accurate__c"] = True
        _apply_title()

        if is_current:
            contact_updates["Person_Has_Moved__c"] = "No"
        else:
            contact_updates["Person_Has_Moved__c"] = "Yes"

        return 2, contact_updates, None, False

    # ── Step 3: Not in SFDC — is it a viable company? ──────────────────
    if is_invalid_company(li_company, title=li_title, headline=li_headline) or is_domain_dead(li_domain):
        contact_updates["Person_Has_Moved__c"] = "Yes"
        contact_updates["Accurate__c"] = False
        return 4, contact_updates, None, False

    # Viable company — but can't create Account without a website
    if not li_domain:
        contact_updates["Accurate__c"] = True
        contact_updates["Person_Has_Moved__c"] = "Yes"
        contact_updates["Left_Company__c"] = True
        _apply_title()
        return 3, contact_updates, None, False

    new_account_info = {
        "Name": li_company,
        "Website": li_domain,
    }

    contact_updates["Accurate__c"] = True
    _apply_title()

    if is_current:
        contact_updates["Person_Has_Moved__c"] = "No"
    else:
        contact_updates["Person_Has_Moved__c"] = "Yes"

    return 3, contact_updates, new_account_info, False
