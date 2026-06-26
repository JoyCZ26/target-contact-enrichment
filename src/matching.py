import re

import requests

try:
    import tldextract
except ImportError:
    tldextract = None

_redirect_cache = {}

REDIRECT_SINK_DOMAINS = {
    "google.com", "linkedin.com", "bitly.com", "bit.ly",
    "facebook.com", "twitter.com", "youtube.com",
    "amazon.com", "microsoft.com", "apple.com",
    "godaddy.com", "squarespace.com", "wix.com",
    "wordpress.com", "shopify.com", "hubspot.com",
    "accounts.google.com",
}


# ── Domain extraction ───────────────────────────────────────────────────────

def extract_domain(url_or_domain):
    """Normalize a URL or domain string to its root domain (e.g. 'integralads.com').
    Uses tldextract if available, otherwise falls back to simple parsing."""
    if not url_or_domain:
        return ""

    raw = url_or_domain.strip().lower()
    # Strip protocol
    raw = re.sub(r"^https?://", "", raw)
    # Strip path / query / trailing slash
    raw = raw.split("/")[0].split("?")[0].split("#")[0]
    # Strip www.
    raw = re.sub(r"^www\.", "", raw)

    if tldextract:
        ext = tldextract.extract(raw)
        if ext.domain and ext.suffix:
            return f"{ext.domain}.{ext.suffix}"
        return raw

    # Fallback: take last two dot-separated segments
    parts = raw.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return raw


def extract_linkedin_slug(linkedin_url):
    """Extract a company domain from a LinkedIn company URL if possible.
    e.g. 'linkedin.com/company/cloudzero' → 'cloudzero'
    Returns the slug, not a full domain — used for matching against account LinkedIn URLs."""
    if not linkedin_url:
        return ""
    raw = linkedin_url.strip().lower()
    raw = re.sub(r"^https?://", "", raw)
    raw = re.sub(r"^(www\.)?linkedin\.com/company/", "", raw)
    raw = raw.split("/")[0].split("?")[0]
    return raw


# ── Company name normalization ──────────────────────────────────────────────

LEGAL_SUFFIXES = re.compile(
    r",?\s*\b("
    r"inc\.?|incorporated|llc|llp|lp|ltd\.?|limited|plc"
    r"|corp\.?|corporation|company|co\.?"
    r"|group|holdings|enterprises|partners|solutions"
    r"|gmbh|ag|sa|sas|sarl|bv|nv|pty\.?\s*ltd\.?"
    r"|s\.?a\.?|s\.?r\.?l\.?"
    r")\s*\.?\s*$",
    re.IGNORECASE,
)


def normalize_company_name(name):
    """Strip legal suffixes, punctuation, and extra whitespace.
    Returns lowercase normalized string."""
    if not name:
        return ""
    n = name.strip().lower()
    # Strip legal suffixes (may appear multiple times: "Foo Corp, Inc.")
    for _ in range(3):
        prev = n
        n = LEGAL_SUFFIXES.sub("", n).strip()
        if n == prev:
            break
    # Strip remaining punctuation except hyphens and ampersands
    n = re.sub(r"[^\w\s\-&]", "", n)
    # Collapse whitespace
    n = re.sub(r"\s+", " ", n).strip()
    return n


# ── Account lookup maps ────────────────────────────────────────────────────

def build_account_maps(accounts):
    """Build domain→Account, normalized_name→Account, and linkedin_slug→Account lookup maps.

    Returns:
        domain_map:        {normalized_domain: {"Id", "Name", "Website"}}
        name_map:          {normalized_name:   {"Id", "Name", "Website"}}
        linkedin_slug_map: {linkedin_slug:     {"Id", "Name", "Website"}}
    """
    domain_map = {}
    name_map = {}
    linkedin_slug_map = {}

    for acct in accounts:
        entry = {
            "Id": acct["Id"],
            "Name": acct.get("Name") or "",
            "Website": acct.get("Website") or "",
        }

        # Domain map — from website
        domain = extract_domain(acct.get("Website") or "")
        if domain:
            domain_map[domain] = entry

        # Name map — keyed by normalized name
        norm = normalize_company_name(acct.get("Name") or "")
        if norm:
            name_map[norm] = entry

        # LinkedIn slug map — from Company_LinkedIn_URL__c and KN_LinkedIn_URL__c
        for field in ("Company_LinkedIn_URL__c", "KN_LinkedIn_URL__c"):
            slug = extract_linkedin_slug(acct.get(field) or "")
            if slug:
                linkedin_slug_map[slug] = entry

    return domain_map, name_map, linkedin_slug_map


# ── Company matching — exact only ──────────────────────────────────────────

def match_company_to_current_account(linkedin_domain, linkedin_company,
                                     sfdc_account_domain, sfdc_account_name,
                                     linkedin_company_url=None,
                                     sfdc_account_linkedin_urls=None):
    """Check if the LinkedIn company matches the contact's CURRENT SFDC account.

    Four exact-match signals (any one = match):
      1. Website domain match
      2. Exact normalized name match
      3. LinkedIn company URL slug match
      4. Redirect-resolved domain match (lazy — only if cheaper signals fail)

    Returns True if it's the same company, False otherwise.
    """
    # Signal 1: Website domain match
    li_domain = extract_domain(linkedin_domain)
    sfdc_domain = extract_domain(sfdc_account_domain)

    if li_domain and sfdc_domain and li_domain == sfdc_domain:
        return True

    # Signal 2: Exact normalized name match
    li_norm = normalize_company_name(linkedin_company)
    sfdc_norm = normalize_company_name(sfdc_account_name)

    if li_norm and sfdc_norm and li_norm == sfdc_norm:
        return True

    # Signal 3: LinkedIn company URL slug match
    if linkedin_company_url and sfdc_account_linkedin_urls:
        li_slug = extract_linkedin_slug(linkedin_company_url)
        if li_slug:
            for sfdc_li_url in sfdc_account_linkedin_urls:
                if extract_linkedin_slug(sfdc_li_url) == li_slug:
                    return True

    # Signal 4: Redirect-resolved domain match (last — requires HTTP)
    if li_domain and sfdc_domain and li_domain != sfdc_domain:
        resolved_li = resolve_domain_redirect(li_domain)
        if resolved_li == sfdc_domain:
            return True
        resolved_sfdc = resolve_domain_redirect(sfdc_domain)
        if li_domain == resolved_sfdc or resolved_li == resolved_sfdc:
            return True

    return False


def lookup_company_in_sfdc(linkedin_domain, linkedin_company, domain_map, name_map,
                          linkedin_company_url=None, linkedin_slug_map=None):
    """Search ALL SFDC Accounts for the LinkedIn company.

    Five exact-match signals:
      1. Website domain lookup
      2. Exact normalized name lookup
      3. LinkedIn company URL slug lookup
      4. Redirect-resolved domain lookup

    Returns the matched Account dict {"Id", "Name", "Website"} or None.
    """
    # Signal 1: Domain lookup
    li_domain = extract_domain(linkedin_domain)
    if li_domain and li_domain in domain_map:
        return domain_map[li_domain]

    # Signal 2: Exact normalized name lookup
    li_norm = normalize_company_name(linkedin_company)
    if li_norm and li_norm in name_map:
        return name_map[li_norm]

    # Signal 3: LinkedIn company URL slug lookup
    if linkedin_company_url and linkedin_slug_map:
        li_slug = extract_linkedin_slug(linkedin_company_url)
        if li_slug and li_slug in linkedin_slug_map:
            return linkedin_slug_map[li_slug]

    # Signal 4: Resolve redirects and re-check domain map
    if li_domain:
        resolved = resolve_domain_redirect(li_domain)
        if resolved and resolved != li_domain and resolved not in REDIRECT_SINK_DOMAINS and resolved in domain_map:
            return domain_map[resolved]

    return None


def resolve_domain_redirect(domain, log=False):
    """Follow HTTP redirects for a domain and return the final resolved domain.
    Returns the original domain if no redirect or on error. Results are cached."""
    if not domain:
        return domain
    if domain in _redirect_cache:
        return _redirect_cache[domain]

    try:
        resp = requests.head(
            f"https://{domain}",
            allow_redirects=True,
            timeout=5,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resolved = extract_domain(resp.url)
        if resolved and resolved in REDIRECT_SINK_DOMAINS:
            if log:
                print(f"    redirect: {domain} → {resolved} (sink — ignored)")
            _redirect_cache[domain] = domain
        elif resolved and resolved != domain:
            if log:
                print(f"    redirect: {domain} → {resolved}")
            _redirect_cache[domain] = resolved
        else:
            _redirect_cache[domain] = resolved or domain
    except Exception:
        _redirect_cache[domain] = domain

    return _redirect_cache[domain]


def is_domain_dead(domain):
    """Return True if domain redirects to a known sink (parking page, login wall, etc.)."""
    if not domain:
        return False
    resolved = resolve_domain_redirect(domain)
    return resolved != domain and resolved in REDIRECT_SINK_DOMAINS


# ── Invalid company detection ───────────────────────────────────────────────

INVALID_EXACT = {
    "self-employed", "self employed", "freelance", "freelancer",
    "independent", "independent consultant", "independent contractor",
    "unemployed", "retired", "career break", "sabbatical",
    "stay-at-home", "student", "looking for opportunities",
    "seeking opportunities", "open to work", "between roles",
    "n/a", "na", "none", "-", "--", ".",
}

INVALID_PATTERNS = re.compile(
    r"(?i)"
    r"(^self[- ]employed)"
    r"|(^freelanc)"
    r"|(^independent\b)"
    r"|(^unemploy)"
    r"|(^retire[d]?\b)"
    r"|(career\s*break)"
    r"|(sabbatical)"
    r"|(looking\s*(for)?\s*opportunit)"
    r"|(seeking\s*(new\s*)?(opportunit|role|position))"
    r"|(open\s*to\s*work)"
    r"|(between\s*(roles|jobs|opportunities))"
    r"|(solo\s*entrepreneur)"
    r"|(solopreneur)"
    r"|(stay[- ]?at[- ]?home)"
    r"|(full[- ]?time\s*(parent|mom|dad|caregiver))"
    r"|(not\s*currently\s*employed)"
    r"|(taking\s*a\s*break)"
)

INVALID_TITLE_PATTERNS = re.compile(
    r"(?i)"
    r"(freelanc)"
    r"|(independent\s*(consultant|contractor|advisor))"
    r"|(self[- ]employed)"
    r"|(sole\s*proprietor)"
    r"|(solopreneur)"
    r"|(owner\s*/?\s*operator)"
    r"|(retired)"
)


def is_invalid_company(company_name, title=None, headline=None):
    """Return True if the company/title/headline indicate this is not a real company.
    Uses company name, title, and headline for maximum signal."""
    if not company_name:
        return True

    name = company_name.strip().lower()

    # Check company name
    if name in INVALID_EXACT:
        return True
    if INVALID_PATTERNS.search(name):
        return True

    # Check title for signals that reinforce non-company
    if title:
        t = title.strip().lower()
        if INVALID_TITLE_PATTERNS.search(t):
            if len(name.split()) <= 2 and not extract_domain(company_name):
                return True

    # Check headline for signals
    if headline:
        h = headline.strip().lower()
        if INVALID_PATTERNS.search(h):
            if len(name.split()) <= 2 and not extract_domain(company_name):
                return True

    return False
