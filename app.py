"""
PrivateHealthData.com - Companies House iXBRL Financial Parser
"""

from flask import Flask, request, jsonify
import requests
from bs4 import BeautifulSoup
import re
from datetime import datetime

app = Flask(__name__)

CH_API_BASE = "https://api.companieshouse.gov.uk"
CH_DOC_API  = "https://document-api.companieshouse.gov.uk"

FINANCIAL_TAGS = {
    "turnover": "revenue", "revenue": "revenue", "turnoverorrevenue": "revenue",
    "operatingprofit": "operating_profit", "profitlossonordinaryactivities": "operating_profit",
    "profitlossbeforetax": "profit_before_tax", "profitbeforetax": "profit_before_tax",
    "profitloss": "net_profit", "profitlossforperiod": "net_profit",
    "depreciationtangibleassets": "depreciation", "amortisationintangibleassets": "amortisation",
    "netassets": "net_assets", "netassetsliabilities": "net_assets", "totalnetassets": "net_assets",
    "fixedassets": "fixed_assets", "tangibleassets": "tangible_assets",
    "currentassets": "current_assets", "cashatbankandinhhand": "cash", "cash": "cash",
    "creditors": "total_creditors", "totalcreditors": "total_creditors",
    "creditorswithinoneyear": "short_term_creditors", "creditorsduewithinoneyear": "short_term_creditors",
    "creditorsafteroneyear": "long_term_creditors", "creditorsdueafteroneyear": "long_term_creditors",
    "shareholdersfunds": "shareholders_funds", "equity": "shareholders_funds",
    "averagenumberemployees": "average_employees", "averagenumberofemployees": "average_employees",
}


def get_filing_history(company_number, api_key):
    url = f"{CH_API_BASE}/company/{company_number}/filing-history"
    resp = requests.get(url, auth=(api_key, ""), params={"category": "accounts", "items_per_page": 10}, timeout=15)
    if resp.status_code != 200:
        return None, f"Filing history HTTP {resp.status_code}"
    items = resp.json().get("items", [])
    return (items[0], None) if items else (None, "No accounts filings found")


def get_document_metadata(document_url, api_key):
    resp = requests.get(document_url, auth=(api_key, ""), timeout=15)
    if resp.status_code != 200:
        return None, f"Metadata HTTP {resp.status_code}: {resp.text[:200]}"
    return resp.json(), None


def download_ixbrl(document_url, api_key):
    """
    CH Document API returns 302 to S3. Must NOT send auth to S3.
    """
    content_url = f"{document_url}/content"
    debug = {"content_url": content_url}

    # Try 1: no-redirect, manual follow
    resp = requests.get(
        content_url,
        auth=(api_key, ""),
        headers={"Accept": "application/xhtml+xml"},
        timeout=30,
        allow_redirects=False,
    )
    debug["try1_status"] = resp.status_code
    debug["try1_headers"] = dict(resp.headers)

    if resp.status_code in (301, 302, 303, 307, 308):
        redirect_url = resp.headers.get("Location", "")
        debug["redirect_url"] = redirect_url
        if redirect_url:
            s3_resp = requests.get(redirect_url, timeout=30)
            debug["s3_status"] = s3_resp.status_code
            if s3_resp.status_code == 200:
                return s3_resp.text, debug

    # Try 2: auto-redirect with xhtml accept
    resp2 = requests.get(content_url, auth=(api_key, ""), headers={"Accept": "application/xhtml+xml"}, timeout=30, allow_redirects=True)
    debug["try2_status"] = resp2.status_code
    if resp2.status_code == 200 and resp2.content:
        return resp2.text, debug

    # Try 3: auto-redirect no accept header
    resp3 = requests.get(content_url, auth=(api_key, ""), timeout=30, allow_redirects=True)
    debug["try3_status"] = resp3.status_code
    if resp3.status_code == 200 and resp3.content:
        return resp3.text, debug

    return None, debug


def clean_numeric_value(raw):
    if not raw:
        return None
    cleaned = re.sub(r"[£$€,\s]", "", raw.strip())
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def parse_ixbrl(html_content, balance_sheet_date=None):
    soup = BeautifulSoup(html_content, "html.parser")
    results = {}
    dates_found = []

    ix_tags = soup.find_all(re.compile(r"^ix:", re.IGNORECASE))
    if not ix_tags:
        ix_tags = soup.find_all(True, attrs={"contextref": True})

    for tag in ix_tags:
        tag_name = tag.get("name", "") or tag.name or ""
        tag_name_clean = re.sub(r"^[^:]+:", "", tag_name).lower().replace("-", "").replace("_", "")
        context_ref = tag.get("contextref", "")
        raw_value = tag.get_text(strip=True)

        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", context_ref)
        if date_match:
            dates_found.append(date_match.group(1))

        for known_tag, field_name in FINANCIAL_TAGS.items():
            if known_tag in tag_name_clean:
                value = clean_numeric_value(raw_value)
                if value is not None:
                    try:
                        value = value * (10 ** int(tag.get("scale", "0")))
                    except (ValueError, TypeError):
                        pass
                    if tag.get("sign", "") == "-":
                        value = -abs(value)
                    if field_name not in results:
                        results[field_name] = value
                break

    if dates_found:
        results["balance_sheet_date"] = max(dates_found)
    elif balance_sheet_date:
        results["balance_sheet_date"] = balance_sheet_date

    if "revenue" in results and results["revenue"] and results["revenue"] > 0:
        if "operating_profit" in results and results["operating_profit"] is not None:
            results["operating_margin_pct"] = round((results["operating_profit"] / results["revenue"]) * 100, 2)
        ebitda = (results.get("operating_profit") or 0) + (results.get("depreciation") or 0) + (results.get("amortisation") or 0)
        if ebitda != 0:
            results["ebitda_estimate"] = ebitda
            results["ebitda_margin_pct"] = round((ebitda / results["revenue"]) * 100, 2)

    return results


def format_for_airtable(financial_data, company_number, filing_date):
    fmt = lambda val: round(val) if val is not None else None
    return {
        "Companies House Number": company_number,
        "Accounts Filed Date": filing_date,
        "Balance Sheet Date": financial_data.get("balance_sheet_date"),
        "Revenue": fmt(financial_data.get("revenue")),
        "Operating Profit": fmt(financial_data.get("operating_profit")),
        "Profit Before Tax": fmt(financial_data.get("profit_before_tax")),
        "Net Profit": fmt(financial_data.get("net_profit")),
        "EBITDA Estimate": fmt(financial_data.get("ebitda_estimate")),
        "EBITDA Margin %": financial_data.get("ebitda_margin_pct"),
        "Operating Margin %": financial_data.get("operating_margin_pct"),
        "Net Assets": fmt(financial_data.get("net_assets")),
        "Fixed Assets": fmt(financial_data.get("fixed_assets")),
        "Current Assets": fmt(financial_data.get("current_assets")),
        "Cash": fmt(financial_data.get("cash")),
        "Total Creditors": fmt(financial_data.get("total_creditors")),
        "Short Term Creditors": fmt(financial_data.get("short_term_creditors")),
        "Long Term Creditors": fmt(financial_data.get("long_term_creditors")),
        "Shareholders Funds": fmt(financial_data.get("shareholders_funds")),
        "Average Employees": financial_data.get("average_employees"),
        "Last Financial Updated": datetime.utcnow().strftime("%Y-%m-%d"),
    }


@app.route("/parse", methods=["POST"])
def parse_company_accounts():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No JSON body"}), 400

    company_number = data.get("company_number", "").strip().upper()
    api_key = data.get("api_key", "").strip()
    debug_mode = data.get("debug", False)

    if not company_number:
        return jsonify({"error": "company_number required"}), 400
    if not api_key:
        return jsonify({"error": "api_key required"}), 400

    filing, err = get_filing_history(company_number, api_key)
    if not filing:
        return jsonify({"error": err, "status": "no_filing"}), 404

    filing_date = filing.get("date", "")
    links = filing.get("links", {})
    document_meta_url = links.get("document_metadata", "")

    if not document_meta_url:
        return jsonify({"error": "No document_metadata link in filing", "filing_keys": list(links.keys()), "status": "no_document_url"}), 404

    if document_meta_url.startswith("/"):
        document_meta_url = f"{CH_DOC_API}{document_meta_url}"

    metadata, err = get_document_metadata(document_meta_url, api_key)
    if not metadata:
        return jsonify({"error": err, "status": "metadata_error"}), 500

    resources = metadata.get("resources", {})
    if "application/xhtml+xml" not in resources:
        return jsonify({"error": "PDF only — no iXBRL", "available_formats": list(resources.keys()), "filing_date": filing_date, "status": "pdf_only"}), 200

    doc_links = metadata.get("links", {})
    document_url = doc_links.get("document", "")
    if not document_url:
        return jsonify({"error": "No document URL in metadata", "metadata_keys": list(doc_links.keys()), "status": "no_download_url"}), 500

    html_content, dl_debug = download_ixbrl(document_url, api_key)
    if not html_content:
        return jsonify({"error": "Failed to download iXBRL", "status": "download_error", "debug": dl_debug}), 500

    financial_data = parse_ixbrl(html_content, filing_date)
    if not financial_data:
        return jsonify({"error": "No data extracted", "status": "parse_empty"}), 200

    airtable_data = format_for_airtable(financial_data, company_number, filing_date)

    response = {
        "status": "success",
        "company_number": company_number,
        "filing_date": filing_date,
        "fields_extracted": len([v for v in financial_data.items() if v[1] is not None]),
        "airtable_data": airtable_data,
        "raw_financial_data": financial_data,
    }
    if debug_mode:
        response["download_debug"] = dl_debug

    return jsonify(response)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "PrivateHealthData iXBRL Parser"})


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
