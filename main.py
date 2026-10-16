import csv
import json
import os
import re
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

HISTORY_JSON = DATA / "leads.json"
HISTORY_CSV = DATA / "leads.csv"
FAILURES_JSON = DATA / "manual_review.json"
FAILURES_CSV = DATA / "manual_review.csv"

MANUAL_HEADERS = [
    "Date Detected",
    "Keyword",
    "Solicitation ID",
    "Official Source URL",
    "Failure Stage",
    "Failure Reason",
    "Retry Status",
    "Last Attempt",
]

# These are your exact 35 main-sheet headers.
HEADERS = [
    "Opportunity ID",
    "Date Found",
    "State",
    "Agency",
    "Opportunity Title",
    "Solicitation / Reference No.",
    "Opportunity Type",
    "Service Category",
    "Official Source URL",
    "Published Date",
    "Deadline Date",
    "Deadline Time / Time Zone",
    "Estimated Value (if published)",
    "Scope Summary",
    "Eligibility / Mandatory Requirements",
    "Required Certifications / Experience",
    "Contract Duration / Options",
    "Incumbent / Prior Award (verified only)",
    "Healthcare IT Fit (0-25)",
    "Eligibility & Compliance (0-20)",
    "Customer / Contract Fit (0-15)",
    "Delivery Capability (0-15)",
    "Deadline Feasibility (0-10)",
    "Commercial Attractiveness (0-10)",
    "Information Completeness (0-5)",
    "Total Score (0-100)",
    "Mandatory Gate (Pass / Fail / Unknown)",
    "Recommendation (Go / Investigate / No-Go)",
    "Fit Rationale",
    "Risks / Gaps",
    "Next Action",
    "Last Verified Date",
    "Owner",
    "Review Status",
    "Reviewer Notes",
]

# Do not auto-fill these six fields.
NEVER_AUTO_FILL = {
    "Risks / Gaps",
    "Next Action",
    "Last Verified Date",
    "Owner",
    "Review Status",
    "Reviewer Notes",
}

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def norm(value):
    return re.sub(r"[^a-z0-9]", "", value.lower())


def canonical_url(url):
    if not url:
        return ""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/+$", "", parts.path) or "/"
    return urlunsplit((parts.scheme.lower(), host, path, "", ""))


def read_json(path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, value):
    with path.open("w", encoding="utf-8") as f:
        json.dump(value, f, indent=2, ensure_ascii=False)


def save_csv(path, records, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)


def parse_date(value):
    value = clean(value)
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def is_open(record, config):
    accepted = {
        clean(s).lower() for s in config["accepted_statuses"]
    }
    if clean(record.get("status")).lower() not in accepted:
        return False

    if config.get("exclude_expired", True):
        due = parse_date(record.get("due_date", ""))
        if due and due < datetime.now(
            ZoneInfo("America/Chicago")
        ).date():
            return False

    return True


def record_key(record):
    sid = clean(record.get("solicitation_id")).lower()
    if sid:
        return "id:" + sid
    url = canonical_url(record.get("url", ""))
    return "url:" + url if url else ""


def log_failure(failures, keyword, record, stage, reason):
    now = datetime.now(ZoneInfo("America/Chicago")).isoformat(
        timespec="seconds"
    )
    sid = clean(record.get("solicitation_id", ""))
    url = record.get("url", "")

    # Reuse an unresolved failure rather than creating duplicates.
    for item in failures:
        if (
            item.get("Solicitation ID", "") == sid
            and item.get("Keyword", "") == keyword
            and item.get("Failure Stage", "") == stage
            and item.get("Retry Status") != "Resolved"
        ):
            item["Failure Reason"] = clean(reason)
            item["Last Attempt"] = now
            item["Official Source URL"] = url
            return

    failures.append({
        "Date Detected": now,
        "Keyword": keyword,
        "Solicitation ID": sid,
        "Official Source URL": url,
        "Failure Stage": stage,
        "Failure Reason": clean(reason)[:1000],
        "Retry Status": "Pending manual review",
        "Last Attempt": now,
    })


def resolve_failures(failures, solicitation_id):
    for item in failures:
        if (
            item.get("Solicitation ID", "").lower()
            == solicitation_id.lower()
            and item.get("Retry Status") != "Resolved"
        ):
            item["Retry Status"] = "Resolved"


def find_column(row, *aliases):
    fields = {norm(k): v for k, v in row.items()}
    for alias in aliases:
        value = fields.get(norm(alias))
        if value is not None:
            return clean(value)
    return ""


def parse_csv_record(row, keyword, base_url):
    sid = find_column(
        row, "Solicitation ID", "Solicitation Number",
        "Solicitation / Reference No.", "Bid Number", "ID"
    )
    title = find_column(
        row, "Title", "Solicitation Title", "Opportunity Title", "Name"
    )
    url = find_column(
        row, "Solicitation URL", "Official Source URL",
        "URL", "Link", "Details URL"
    )
    if not url and sid:
        url = base_url.rstrip("/") + "/" + quote(sid, safe="")

    return {
        "solicitation_id": sid,
        "title": title,
        "status": find_column(row, "Status"),
        "agency": find_column(
            row, "Agency", "Agency Name",
            "Agency/Texas SmartBuy Member Name"
        ),
        "agency_number": find_column(
            row, "Agency Number",
            "Agency/Texas SmartBuy Member Number"
        ),
        "posting_date": find_column(
            row, "Posting Date", "Solicitation Posting Date",
            "Published Date"
        ),
        "due_date": find_column(
            row, "Due Date", "Response Due Date", "Deadline Date"
        ),
        "due_time": find_column(
            row, "Due Time", "Response Due Time", "Deadline Time"
        ),
        "opportunity_type": find_column(
            row, "Solicitation Type", "Opportunity Type", "Type"
        ),
        "estimated_value": find_column(
            row, "Estimated Value", "Estimated Amount", "Value"
        ),
        "class_item_codes": find_column(
            row, "Class/Item Codes", "Class Item Codes", "NIGP Codes"
        ),
        "description": find_column(
            row, "Description", "Solicitation Description"
        ),
        "url": url,
        "matched_keywords": [keyword],
        "date_found": date.today().isoformat(),
    }


def search_keyword(page, keyword, config):
    page.goto(
        config["search_url"],
        wait_until="domcontentloaded",
        timeout=60000
    )

    # Use the public ESBD keyword search and CSV export.
    keyword_box = page.get_by_label(
        re.compile("Keyword Search", re.I)
    )
    keyword_box.fill(keyword)

    page.get_by_role(
        "button", name=re.compile(r"^Search$", re.I)
    ).click()
    page.wait_for_timeout(2500)

    with page.expect_download(timeout=60000) as download_info:
        page.get_by_role(
            "button", name=re.compile("Export to CSV", re.I)
        ).click()

    download = download_info.value
    safe_name = re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-")
    export_path = DATA / f"esbd-export-{safe_name}.csv"
    download.save_as(str(export_path))

    with export_path.open(
        "r", encoding="utf-8-sig", newline=""
    ) as f:
        rows = list(csv.DictReader(f))

    if rows:
        available = {norm(k) for k in rows[0]}
        if not available.intersection({
            "solicitationid", "solicitationnumber",
            "bidnumber", "id"
        }):
            raise RuntimeError(
                "CSV ID column not recognized. Found headers: "
                + ", ".join(rows[0].keys())
            )

    print(f"{keyword}: {len(rows)} CSV rows")
    return [
        parse_csv_record(row, keyword, config["search_url"])
        for row in rows
    ]


def extract_label(lines, *labels):
    wanted = {x.lower().rstrip(":") for x in labels}
    for i, line in enumerate(lines):
        if line.lower().strip().rstrip(":") in wanted:
            for following in lines[i + 1:]:
                if following.strip():
                    return clean(following)
    return ""


def extract_description(lines):
    collecting = False
    parts = []
    stops = {"attachments", "download pdf", "follow solicitation"}

    for line in lines:
        stripped = line.strip()
        label = stripped.lower().rstrip(":")

        if label in {"solicitation description", "description"}:
            collecting = True
            continue

        if collecting and label in stops:
            break

        if collecting and stripped:
            parts.append(stripped)

    return clean(" ".join(parts))


def enrich_detail(page, record, keyword, failures):
    try:
        page.goto(
            record["url"],
            wait_until="domcontentloaded",
            timeout=60000
        )
        page.wait_for_timeout(800)
        lines = page.locator("body").inner_text().splitlines()

        headings = page.locator("h1, h2").all_text_contents()
        for heading in headings:
            heading = clean(heading)
            if heading and "electronic state business daily" not in heading.lower():
                record["title"] = heading
                break

        record["status"] = (
            extract_label(lines, "Status") or record["status"]
        )
        record["agency"] = (
            extract_label(
                lines, "Agency/Texas SmartBuy Member Name", "Agency"
            ) or record["agency"]
        )
        record["agency_number"] = (
            extract_label(
                lines, "Agency/Texas SmartBuy Member Number"
            ) or record["agency_number"]
        )
        record["posting_date"] = (
            extract_label(lines, "Solicitation Posting Date", "Posting Date")
            or record["posting_date"]
        )
        record["due_date"] = (
            extract_label(lines, "Response Due Date", "Due Date")
            or record["due_date"]
        )
        record["due_time"] = (
            extract_label(lines, "Response Due Time", "Due Time")
            or record["due_time"]
        )
        record["contact_name"] = extract_label(lines, "Contact Name")
        record["contact_email"] = extract_label(lines, "Contact Email")
        record["contact_phone"] = extract_label(
            lines, "Contact Number", "Contact Phone"
        )
        record["opportunity_type"] = (
            extract_label(lines, "Solicitation Type", "Type")
            or record["opportunity_type"]
        )
        record["estimated_value"] = (
            extract_label(lines, "Estimated Value", "Estimated Amount")
            or record["estimated_value"]
        )
        record["description"] = (
            extract_description(lines) or record["description"]
        )

        # Successful page retrieval resolves previous detail failures.
        resolve_failures(failures, record["solicitation_id"])
        record["detail_read_ok"] = True

    except Exception as exc:
        record["detail_read_ok"] = False
        log_failure(
            failures, keyword, record, "Detail extraction", str(exc)
        )
        print(
            f"DETAIL FAILURE: {record.get('solicitation_id')} — {exc}"
        )

    return record


def to_opportunity_row(record):
    # Empty strings for unavailable information; no invented values.
    row = {header: "" for header in HEADERS}

    row.update({
        "Opportunity ID": record.get("solicitation_id", ""),
        "Date Found": record.get("date_found", date.today().isoformat()),
        "State": "Texas",
        "Agency": record.get("agency", ""),
        "Opportunity Title": record.get("title", ""),
        "Solicitation / Reference No.": record.get("solicitation_id", ""),
        "Opportunity Type": record.get("opportunity_type", ""),
        "Service Category": ", ".join(
            record.get("matched_keywords", [])
        ),
        "Official Source URL": record.get("url", ""),
        "Published Date": record.get("posting_date", ""),
        "Deadline Date": record.get("due_date", ""),
        "Deadline Time / Time Zone": record.get("due_time", ""),
        "Estimated Value (if published)": record.get("estimated_value", ""),
        "Scope Summary": record.get("description", ""),
        "Mandatory Gate (Pass / Fail / Unknown)": "Unknown",
    })

    # Explicitly protect the six manually maintained fields.
    for header in NEVER_AUTO_FILL:
        row[header] = ""

    return row


def connect_google(config):
    secret = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not secret:
        raise RuntimeError("GitHub secret GOOGLE_SERVICE_ACCOUNT_JSON is missing")

    credentials = Credentials.from_service_account_info(
        json.loads(secret), scopes=SCOPES
    )
    client = gspread.authorize(credentials)
    spreadsheet = client.open_by_key(config["spreadsheet_id"])
    return client, spreadsheet


def sync_opportunities(spreadsheet, config, history):
    sheet = spreadsheet.get_worksheet_by_id(
        int(config["worksheet_id"])
    )
    if sheet is None:
        raise RuntimeError("Configured main worksheet ID was not found")

    values = sheet.get_all_values()
    if not values:
        sheet.append_row(HEADERS, value_input_option="RAW")
        values = [HEADERS]

    header = values[0]
    if header[:len(HEADERS)] != HEADERS:
        raise RuntimeError(
            "Main sheet headers do not match the configured 35-column "
            "layout. No rows were appended. Check the tab and header order."
        )

    positions = {name: i for i, name in enumerate(header)}
    id_col = positions["Opportunity ID"]
    ref_col = positions["Solicitation / Reference No."]
    url_col = positions["Official Source URL"]

    existing_ids, existing_urls = set(), set()
    for row in values[1:]:
        if len(row) > id_col and row[id_col]:
            existing_ids.add(row[id_col].strip().lower())
        if len(row) > ref_col and row[ref_col]:
            existing_ids.add(row[ref_col].strip().lower())
        if len(row) > url_col and row[url_col]:
            existing_urls.add(canonical_url(row[url_col]))

    append_rows = []
    for record in history:
        row_dict = to_opportunity_row(record)
        sid = clean(record.get("solicitation_id")).lower()
        url = canonical_url(record.get("url", ""))

        if sid and sid in existing_ids:
            continue
        if url and url in existing_urls:
            continue

        append_rows.append([row_dict.get(col, "") for col in header])
        if sid:
            existing_ids.add(sid)
        if url:
            existing_urls.add(url)

    if append_rows:
        sheet.append_rows(append_rows, value_input_option="RAW")

    print(f"Google Sheets: appended {len(append_rows)} new opportunities")


def sync_manual_review(spreadsheet, config, failures):
    name = config["manual_review_worksheet"]

    try:
        sheet = spreadsheet.worksheet(name)
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(
            title=name, rows=2000, cols=len(MANUAL_HEADERS)
        )

    values = sheet.get_all_values()
    if not values:
        sheet.append_row(MANUAL_HEADERS, value_input_option="RAW")
        values = [MANUAL_HEADERS]

    header = values[0]
    if not all(col in header for col in MANUAL_HEADERS):
        raise RuntimeError(
            f"{name} does not contain all required manual-review headers"
        )

    positions = {name: i for i, name in enumerate(header)}
    existing_keys = set()

    for row in values[1:]:
        def cell(col):
            i = positions[col]
            return row[i] if len(row) > i else ""

        existing_keys.add((
            cell("Keyword"),
            cell("Solicitation ID"),
            cell("Failure Stage")
        ))

    additions = []
    for failure in failures:
        key = (
            failure.get("Keyword", ""),
            failure.get("Solicitation ID", ""),
            failure.get("Failure Stage", "")
        )
        if key in existing_keys:
            continue

        additions.append([failure.get(col, "") for col in header])
        existing_keys.add(key)

    if additions:
        sheet.append_rows(additions, value_input_option="RAW")

    print(f"Manual review: appended {len(additions)} new failure records")


def main():
    config = read_json(ROOT / "config.json", {})
    history = read_json(HISTORY_JSON, [])
    failures = read_json(FAILURES_JSON, [])

    history_by_key = {
        record_key(item): item
        for item in history if record_key(item)
    }

    discovered = {}
    search_failures = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(accept_downloads=True)

        try:
            for keyword in config["keywords"]:
                try:
                    records = search_keyword(page, keyword, config)
                except Exception as exc:
                    log_failure(
                        failures, keyword, {},
                        "Search/export", str(exc)
                    )
                    search_failures.append(keyword)
                    print(f"SEARCH FAILURE for {keyword}: {exc}")
                    continue

                for record in records:
                    sid = record.get("solicitation_id", "")
                    if not sid:
                        log_failure(
                            failures, keyword, record,
                            "CSV parsing", "CSV row had no recognized solicitation ID"
                        )
                        continue

                    key = record_key(record)
                    if not key:
                        continue

                    if key not in discovered:
                        discovered[key] = record
                    elif keyword not in discovered[key]["matched_keywords"]:
                        discovered[key]["matched_keywords"].append(keyword)

            candidates = [
                record for record in discovered.values()
                if is_open(record, config)
            ]

            # New records first, then oldest posting dates.
            candidates.sort(key=lambda r: (
                record_key(r) in history_by_key,
                r.get("posting_date", "")
            ))

            cap = int(config.get("max_details_per_run", 100))
            if len(candidates) > cap:
                print(
                    f"WARNING: {len(candidates)} candidates; processing "
                    f"{cap} this run. A persistent queue is recommended "
                    "if the cap is regularly reached."
                )

            for record in candidates[:cap]:
                keyword = record["matched_keywords"][0]
                enrich_detail(page, record, keyword, failures)

                if is_open(record, config) and record.get("solicitation_id"):
                    key = record_key(record)
                    if key in history_by_key:
                        old = history_by_key[key]
                        old_keywords = set(old.get("matched_keywords", []))
                        old_keywords.update(record.get("matched_keywords", []))
                        old["matched_keywords"] = sorted(old_keywords)

                        for field, value in record.items():
                            if value not in ("", None, []):
                                old[field] = value
                    else:
                        history_by_key[key] = record

                time.sleep(float(config.get("request_delay_seconds", 2)))

        finally:
            browser.close()

    history = list(history_by_key.values())

    # Persist local records before contacting Google Sheets.
    save_json(HISTORY_JSON, history)
    save_csv(HISTORY_CSV, [
        {
            "Opportunity ID": r.get("solicitation_id", ""),
            "Opportunity Title": r.get("title", ""),
            "Status": r.get("status", ""),
            "Agency": r.get("agency", ""),
            "Published Date": r.get("posting_date", ""),
            "Deadline Date": r.get("due_date", ""),
            "Official Source URL": r.get("url", ""),
            "Matched Keywords": ", ".join(r.get("matched_keywords", [])),
            "Date Found": r.get("date_found", ""),
        }
        for r in history
    ], [
        "Opportunity ID", "Opportunity Title", "Status", "Agency",
        "Published Date", "Deadline Date", "Official Source URL",
        "Matched Keywords", "Date Found"
    ])

    save_json(FAILURES_JSON, failures)
    save_csv(FAILURES_CSV, failures, MANUAL_HEADERS)

    # Sync the complete history so records missed during a temporary
    # Sheets outage can be appended on a later run.
    _, spreadsheet = connect_google(config)
    sync_opportunities(spreadsheet, config, history)
    sync_manual_review(spreadsheet, config, failures)

    print(f"Unique history records: {len(history)}")
    print(f"Failure/manual-review records: {len(failures)}")

    if search_failures:
        raise RuntimeError(
            "One or more ESBD searches failed: "
            + ", ".join(search_failures)
        )


if __name__ == "__main__":
    main()
