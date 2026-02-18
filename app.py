"""
PrivateHealthData.com - Companies House iXBRL Financial Parser
Flask webhook that Make.com calls to extract financial figures from Companies House accounts.

Endpoint: POST /parse
Body: { "company_number": "05238658", "api_key": "your_ch_api_key" }
Returns: JSON with extracted financial figures ready to push to Airtable
"""

from flask import Flask, request, jsonify
import requests
from bs4 import BeautifulSoup
import re
import json
from datetime import datetime

app = Flask(__name__)

# ── Companies House API base URLs ─────────────────────────────────────────────
CH_API_BASE = "https://api.companieshouse.gov.uk"
CH_DOC_API  = "https://document-api.companieshouse.gov.uk"

# ── XBRL tag mappings ─────────────────────────────────────────────────────────
# Maps common UK GAAP / FRS102 / IFRS tag names to human-readable field names.
# Tags are case-insensitive and matched with partial string matching for robustness.
FINANCIAL_TAGS = {
    # Revenue / Turnover
    "turnover":                          "revenue",
    "revenue":                           "revenue",
    "turnoverorrevenue":                 "revenue",

    # Operating Profit
    "operatingprofit":                   "operating_profit",
    "profitlossonordinaryactivities":    "operating_profit",
    "profitlossbeforetax":               "profit_before_tax",
    "profitbeforetax":                   "profit_before_tax",

    # Net Profit
    "profitloss":                        "net_profit",
    "profitlossforperiod":               "net_profit",

    # EBITDA components
    "depreciationtangibleassets":        "depreciation",
    "amortisationintangibleassets":      "amortisation",

    # Balance Sheet
    "netassets":                         "net_assets",
    "netassetsliabilities":              "net_assets",
    "totalnetassets":                    "net_assets",
    "fixedassets":                       "fixed_assets",
    "tangibleassets":                    "tangible_assets",
    "currentassets":                     "current_assets",
    "cashatbankandinhhand":              "cash",
    "cash":                              "cash",

    # Liabilities
    "creditors":                         "total_creditors",
    "totalcreditors":                    "total_creditors",
    "creditorswithinoneyear":            "short_term_creditors",
    "creditorsduewithinoneyear":         "short_term_creditors",
    "creditorsafteroneyear":             "long_term_creditors",
    "creditorsdueafteroneyear":          "long_term_creditors",
    "netdebt":                           "net_debt",

    # Equity
    "shareholdersfunds":                 "shareholders_funds",
    "equity":                            "shareholders_funds",
    "calledupsharecapital":              "share_capital",

    # Employees
    "averagenumberemployees":            "average_employees",
    "averagenumberofemployees":          "average_employees",
}


def get_filing_history(company_number: str, api_key: str) -> dict | None:
    """Fetch the filing history for a company and return the latest accounts entry."""
    url = f"{CH_API_BASE}/company/{company_number}/filing-history"
    params = {"category": "accounts", "items_per_page": 10}
    resp = requests.get(url, auth=(api_key, ""), params=params, timeout=15)

    if resp.status_code != 200:
        return None

    items = resp.json().get("items", [])
    if not items:
        return None

    # Return the most recent accounts filing
    return items[0]


def get_document_metadata(document_url: str, api_key: str) -> dict | None:
    """Get metadata for a document to find its download URL and available formats."""
    resp = requests.get(document_url, auth=(api_key, ""), timeout=15, allow_redirects=True)
    if resp.status_code != 200:
        return None
    return resp.json()


def download_ixbrl(document_url: str, api_key: str) -> str | None:
    """Download the iXBRL document content. Returns HTML string or None."""
    headers = {"Accept": "application/xhtml+xml"}
    resp = requests.get(
        f"{document_url}/content",
        auth=(api_key, ""),
        headers=headers,
        timeout=30,
        allow_redirects=True,
    )

    if resp.status_code == 200 and resp.content:
        return resp.text

    # Fallback: try without explicit accept header
    resp = requests.get(
        f"{document_url}/content",
        auth=(api_key, ""),
        timeout=30,
        allow_redirects=True,
    )
    if resp.status_code == 200:
        return resp.text

    return None


def clean_numeric_value(raw: str) -> float | None:
    """Clean a raw string value from an XBRL tag into a float."""
    if not raw:
        return None
    # Remove whitespace, commas, currency symbols
    cleaned = re.sub(r"[£$€,\s]", "", raw.strip())
    # Handle negative values in brackets e.g. (123456)
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def parse_ixbrl(html_content: str, balance_sheet_date: str = None) -> dict:
    """
    Parse iXBRL HTML content and extract financial figures.
    Returns a dict of field_name: value pairs.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    results = {}
    dates_found = []

    # Find all ix:nonFraction and ix:nonNumeric tags (iXBRL financial tags)
    # Also look for plain XBRL in older documents
    ix_tags = soup.find_all(re.compile(r"^ix:", re.IGNORECASE))

    # If no ix: tags found, try looking for xbrli: namespace
    if not ix_tags:
        ix_tags = soup.find_all(True, attrs={"contextref": True})

    for tag in ix_tags:
        tag_name = tag.get("name", "") or tag.name or ""
        tag_name_clean = re.sub(r"^[^:]+:", "", tag_name).lower().replace("-", "").replace("_", "")

        # Get the context reference to determine the period
        context_ref = tag.get("contextref", "")
        raw_value = tag.get_text(strip=True)

        # Try to extract date from context ref
        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", context_ref)
        if date_match:
            dates_found.append(date_match.group(1))

        # Match against our known tags
        for known_tag, field_name in FINANCIAL_TAGS.items():
            if known_tag in tag_name_clean:
                value = clean_numeric_value(raw_value)
                if value is not None:
                    # Handle scale factor (some values are in thousands)
                    scale = tag.get("scale", "0")
                    try:
                        scale_factor = 10 ** int(scale)
                        value = value * scale_factor
                    except (ValueError, TypeError):
                        pass

                    # Handle sign attribute
                    sign = tag.get("sign", "")
                    if sign == "-":
                        value = -abs(value)

                    # Only update if we don't have this field yet, or if this is a more recent value
                    if field_name not in results:
                        results[field_name] = value
                break

    # Determine the balance sheet date from context refs
    if dates_found:
        results["balance_sheet_date"] = max(dates_found)
    elif balance_sheet_date:
        results["balance_sheet_date"] = balance_sheet_date

    # Calculate derived metrics
    if "revenue" in results and results["revenue"] and results["revenue"] > 0:
        if "operating_profit" in results and results["operating_profit"] is not None:
            results["operating_margin_pct"] = round(
                (results["operating_profit"] / results["revenue"]) * 100, 2
            )

        # Estimate EBITDA if we have the components
        ebitda = results.get("operating_profit", 0) or 0
        ebitda += results.get("depreciation", 0) or 0
        ebitda += results.get("amortisation", 0) or 0
        if ebitda != 0:
            results["ebitda_estimate"] = ebitda
            results["ebitda_margin_pct"] = round((ebitda / results["revenue"]) * 100, 2)

    return results


def format_for_airtable(financial_data: dict, company_number: str, filing_date: str) -> dict:
    """Format extracted financial data for Airtable field names."""
    def fmt_currency(val):
        return round(val) if val is not None else None

    return {
        "Companies House Number":    company_number,
        "Accounts Filed Date":       filing_date,
        "Balance Sheet Date":        financial_data.get("balance_sheet_date"),
        "Revenue":                   fmt_currency(financial_data.get("revenue")),
        "Operating Profit":          fmt_currency(financial_data.get("operating_profit")),
        "Profit Before Tax":         fmt_currency(financial_data.get("profit_before_tax")),
        "Net Profit":                fmt_currency(financial_data.get("net_profit")),
        "EBITDA Estimate":           fmt_currency(financial_data.get("ebitda_estimate")),
        "EBITDA Margin %":           financial_data.get("ebitda_margin_pct"),
        "Operating Margin %":        financial_data.get("operating_margin_pct"),
        "Net Assets":                fmt_currency(financial_data.get("net_assets")),
        "Fixed Assets":              fmt_currency(financial_data.get("fixed_assets")),
        "Current Assets":            fmt_currency(financial_data.get("current_assets")),
        "Cash":                      fmt_currency(financial_data.get("cash")),
        "Total Creditors":           fmt_currency(financial_data.get("total_creditors")),
        "Short Term Creditors":      fmt_currency(financial_data.get("short_term_creditors")),
        "Long Term Creditors":       fmt_currency(financial_data.get("long_term_creditors")),
        "Shareholders Funds":        fmt_currency(financial_data.get("shareholders_funds")),
        "Average Employees":         financial_data.get("average_employees"),
        "Last Financial Updated":    datetime.utcnow().strftime("%Y-%m-%d"),
    }


@app.route("/parse", methods=["POST"])
def parse_company_accounts():
    """
    Main webhook endpoint.
    Expects JSON body: { "company_number": "05238658", "api_key": "your_ch_key" }
    Returns: financial data formatted for Airtable, or error details.
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "No JSON body provided"}), 400

    company_number = data.get("company_number", "").strip().upper()
    api_key = data.get("api_key", "").strip()

    if not company_number:
        return jsonify({"error": "company_number is required"}), 400
    if not api_key:
        return jsonify({"error": "api_key is required"}), 400

    # Step 1: Get filing history
    filing = get_filing_history(company_number, api_key)
    if not filing:
        return jsonify({
            "error": "No accounts filing found",
            "company_number": company_number,
            "status": "no_filing"
        }), 404

    filing_date = filing.get("date", "")
    description = filing.get("description", "")

    # Step 2: Get document metadata to find download URL
    links = filing.get("links", {})
    document_meta_url = links.get("document_metadata", "")

    if not document_meta_url:
        return jsonify({
            "error": "No document metadata URL found in filing",
            "filing_description": description,
            "status": "no_document_url"
        }), 404

    # Construct full URL if relative
    if document_meta_url.startswith("/"):
        document_meta_url = f"{CH_DOC_API}{document_meta_url}"

    metadata = get_document_metadata(document_meta_url, api_key)
    if not metadata:
        return jsonify({
            "error": "Could not retrieve document metadata",
            "status": "metadata_error"
        }), 500

    # Check if iXBRL format is available
    resources = metadata.get("resources", {})
    if "application/xhtml+xml" not in resources and "application/xhtml" not in str(resources):
        return jsonify({
            "error": "No iXBRL format available — filing may be PDF only",
            "available_formats": list(resources.keys()),
            "filing_date": filing_date,
            "description": description,
            "status": "pdf_only"
        }), 200  # Not an error, just no parseable data

    # Step 3: Download the iXBRL document
    doc_links = metadata.get("links", {})
    document_url = doc_links.get("document", "")

    if not document_url:
        return jsonify({"error": "No document download URL", "status": "no_download_url"}), 500

    html_content = download_ixbrl(document_url, api_key)
    if not html_content:
        return jsonify({"error": "Failed to download iXBRL document", "status": "download_error"}), 500

    # Step 4: Parse the iXBRL
    financial_data = parse_ixbrl(html_content, filing_date)

    if not financial_data:
        return jsonify({
            "error": "No financial data extracted from document",
            "status": "parse_empty",
            "filing_date": filing_date
        }), 200

    # Step 5: Format for Airtable
    airtable_data = format_for_airtable(financial_data, company_number, filing_date)

    return jsonify({
        "status": "success",
        "company_number": company_number,
        "filing_date": filing_date,
        "fields_extracted": len([v for v in financial_data.items() if v[1] is not None]),
        "airtable_data": airtable_data,
        "raw_financial_data": financial_data
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "PrivateHealthData iXBRL Parser"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
