import re
from difflib import SequenceMatcher

try:
    import tldextract
except ImportError:
    tldextract = None


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

# Words too common to be meaningful in token matching
STOP_WORDS = {
    "the", "and", "of", "for", "a", "an", "in", "at", "by", "to",
    "technologies", "technology", "tech", "software", "services",
    "solutions", "systems", "consulting", "digital", "global",
    "international", "labs", "studio", "studios", "media",
}


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


def _tokenize(name):
    """Split a normalized name into significant tokens (no stop words)."""
    tokens = re.split(r"[\s\-&]+", name)
    return [t for t in tokens if t and t not in STOP_WORDS]


# ── Account lookup maps ────────────────────────────────────────────────────

def build_account_maps(accounts):
    """Build domain→Account and normalized_name→Account lookup maps.

    Returns:
        domain_map:     {normalized_domain: {"Id", "Name", "Website"}}
        name_map:       {normalized_name:   {"Id", "Name", "Website"}}
    """
    domain_map = {}
    name_map = {}

    for acct in accounts:
        entry = {
            "Id": acct["Id"],
            "Name": acct.get("Name") or "",
            "Website": acct.get("Website") or "",
        }

        # Domain map
        domain = extract_domain(acct.get("Website") or "")
        if domain:
            domain_map[domain] = entry

        # Name map — keyed by normalized name
        norm = normalize_company_name(acct.get("Name") or "")
        if norm:
            name_map[norm] = entry

    return domain_map, name_map


# ── Company name matching ───────────────────────────────────────────────────

FUZZY_THRESHOLD = 0.92


def _names_match(li_name_normalized, sfdc_name_normalized):
    """Multi-layer name comparison. Returns True if names represent the same company.

    Layer 1: Exact match after normalization
    Layer 2: Token containment — all tokens from the shorter name appear in the longer
    Layer 3: High-threshold fuzzy match (0.92+) for typos/minor variations
    """
    if not li_name_normalized or not sfdc_name_normalized:
        return False

    # Layer 1: exact match
    if li_name_normalized == sfdc_name_normalized:
        return True

    # Layer 2: token containment
    li_tokens = _tokenize(li_name_normalized)
    sfdc_tokens = _tokenize(sfdc_name_normalized)

    if li_tokens and sfdc_tokens:
        shorter, longer = (li_tokens, sfdc_tokens) if len(li_tokens) <= len(sfdc_tokens) else (sfdc_tokens, li_tokens)
        longer_set = set(longer)
        if all(t in longer_set for t in shorter):
            return True

    # Layer 3: fuzzy match (high threshold to avoid false positives)
    score = SequenceMatcher(None, li_name_normalized, sfdc_name_normalized).ratio()
    if score >= FUZZY_THRESHOLD:
        return True

    return False


def match_company_to_current_account(linkedin_domain, linkedin_company,
                                     sfdc_account_domain, sfdc_account_name):
    """Check if the LinkedIn company matches the contact's CURRENT SFDC account.

    Uses domain comparison first, then name comparison as fallback.

    Returns True if it's the same company, False otherwise.
    """
    # Domain match (primary — most reliable)
    li_domain = extract_domain(linkedin_domain)
    sfdc_domain = extract_domain(sfdc_account_domain)

    if li_domain and sfdc_domain and li_domain == sfdc_domain:
        return True

    # Name match (fallback for accounts without matching domains)
    li_norm = normalize_company_name(linkedin_company)
    sfdc_norm = normalize_company_name(sfdc_account_name)

    if _names_match(li_norm, sfdc_norm):
        return True

    return False


def lookup_company_in_sfdc(linkedin_domain, linkedin_company, domain_map, name_map):
    """Search ALL SFDC Accounts for the LinkedIn company.

    Domain lookup first, then normalized name matching.

    Returns the matched Account dict {"Id", "Name", "Website"} or None.
    """
    # Domain lookup (primary)
    li_domain = extract_domain(linkedin_domain)
    if li_domain and li_domain in domain_map:
        return domain_map[li_domain]

    # Normalized name lookup
    li_norm = normalize_company_name(linkedin_company)
    if not li_norm:
        return None

    # Exact normalized match
    if li_norm in name_map:
        return name_map[li_norm]

    # Token containment + fuzzy against all accounts
    for sfdc_norm, acct in name_map.items():
        if _names_match(li_norm, sfdc_norm):
            return acct

    return None


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
            # Title says freelancer/independent — but only flag if company is
            # also suspicious (very short name, or matches the person's own name pattern)
            if len(name.split()) <= 2 and not extract_domain(company_name):
                return True

    # Check headline for signals
    if headline:
        h = headline.strip().lower()
        if INVALID_PATTERNS.search(h):
            if len(name.split()) <= 2 and not extract_domain(company_name):
                return True

    return False
