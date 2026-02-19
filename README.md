# PrivateHealthData — iXBRL Financial Parser 

Flask webhook that extracts financial figures from Companies House iXBRL accounts and returns them ready to push into Airtable via Make.com.

---

## What it does

1. Takes a Companies House number
2. Calls the CH API to find the latest accounts filing
3. Downloads the iXBRL document
4. Parses all tagged financial values (revenue, profit, net assets, debt, cash, employees etc)
5. Returns clean JSON formatted for Airtable

---

## Fields extracted (where available)

| Field | Description |
|---|---|
| Revenue | Annual turnover |
| Operating Profit | Profit from operations |
| Profit Before Tax | Pre-tax profit |
| Net Profit | Post-tax profit |
| EBITDA Estimate | Operating profit + depreciation + amortisation |
| EBITDA Margin % | EBITDA / Revenue |
| Operating Margin % | Operating profit / Revenue |
| Net Assets | Total net assets / shareholders funds |
| Fixed Assets | Property, plant and equipment |
| Current Assets | Short-term assets |
| Cash | Cash at bank |
| Total Creditors | Total liabilities |
| Short Term Creditors | Due within 1 year |
| Long Term Creditors | Due after 1 year |
| Shareholders Funds | Equity |
| Average Employees | Average headcount |
| Balance Sheet Date | Year end date |
| Accounts Filed Date | Date filed at Companies House |

---

## Deploy to Railway (recommended — free tier available)

1. Create account at railway.app
2. New Project → Deploy from GitHub repo
3. Upload these files to a GitHub repo first
4. Railway auto-detects Python and runs gunicorn via Procfile
5. Get your public URL e.g. `https://ixbrl-parser.railway.app`

## Deploy to Render (alternative)

1. Create account at render.com
2. New Web Service → connect GitHub repo
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app --bind 0.0.0.0:$PORT`
5. Free tier spins down after inactivity — use paid tier (£7/month) for production

---

## API Usage

### Endpoint
```
POST /parse
Content-Type: application/json
```

### Request body
```json
{
  "company_number": "05238658",
  "api_key": "your_companies_house_api_key"
}
```

### Success response
```json
{
  "status": "success",
  "company_number": "05238658",
  "filing_date": "2023-12-31",
  "fields_extracted": 14,
  "airtable_data": {
    "Companies House Number": "05238658",
    "Accounts Filed Date": "2024-03-15",
    "Balance Sheet Date": "2023-09-30",
    "Revenue": 2450000,
    "Operating Profit": 312000,
    "EBITDA Estimate": 445000,
    "EBITDA Margin %": 18.16,
    "Net Assets": 890000,
    "Cash": 145000,
    "Average Employees": 48,
    ...
  }
}
```

### PDF only response (not an error — just no parseable data)
```json
{
  "status": "pdf_only",
  "error": "No iXBRL format available — filing may be PDF only",
  "filing_date": "2023-12-31"
}
```

### Health check
```
GET /health
```

---

## Make.com Integration

Add an HTTP module to your Companies House workflow:

- **Method**: POST
- **URL**: `https://your-app.railway.app/parse`
- **Body type**: Raw
- **Content type**: application/json
- **Body**:
```json
{
  "company_number": "{{2.data.companiesHouseNumber}}",
  "api_key": "your_ch_api_key"
}
```

Then map `airtable_data.*` fields into your Airtable Update Record module.

---

## Coverage notes

- ~75% of Companies House filings are in iXBRL format
- Micro and abridged accounts contain limited data (often just net assets)
- Full accounts (larger operators) contain complete P&L and balance sheet
- The script returns `pdf_only` status for non-iXBRL filings — handle this in Make with a Router

---

## Local testing

```bash
pip install -r requirements.txt
python app.py
```

Then test with curl:
```bash
curl -X POST http://localhost:5000/parse \
  -H "Content-Type: application/json" \
  -d '{"company_number": "05238658", "api_key": "your_key_here"}'
```
