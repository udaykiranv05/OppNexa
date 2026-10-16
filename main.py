import csv
import json
import os
import re
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit, urljoin, parse_qs, urlencode
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

HISTORY_JSON = DATA / "leads.json"
HISTORY_CSV = DATA / "leads.csv"
FAILURES_JSON = DATA / "manual_review.json"
FAILURES_CSV = DATA / "manual_review.csv"

MANUAL_HEADERS = [
    "Date Detected", "Keyword", "Solicitation ID", "Official Source URL",
    "Failure Stage", "Failure Reason", "Retry Status", "Last Attempt",
]

HEADERS = [
    "Opportunity ID", "Date Found", "State", "Agency", "Opportunity Title",
    "Solicitation / Reference No.", "Opportunity Type", "Service Category",
    "Official Source URL", "Published Date", "Deadline Date",
    "Deadline Time / Time Zone", "Estimated Value (if published)",
    "Scope Summary", "Eligibility / Mandatory Requirements",
    "Required Certifications / Experience", "Contract Duration / Options",
    "Incumbent / Prior Award (verified only)", "Healthcare IT Fit (0-25)",
    "Eligibility & Compliance (0-20)", "Customer / Contract Fit (0-15)",
    "Delivery Capability (0-15)", "Deadline Feasibility (0-10)",
    "Commercial Attractiveness (0-10)", "Information Completeness (0-5)",
    "Total Score (0-100)", "Mandatory Gate (Pass / Fail / Unknown)",
    "Recommendation (Go / Investigate / No-Go)", "Fit Rationale",
    "Risks / Gaps", "Next Action", "Last Verified Date", "Owner",
    "Review Status", "Reviewer Notes",
]

# These six fields are intentionally never auto-filled.
NEVER_AUTO_FILL = {
    "Risks / Gaps", "Next Action", "Last Verified Date",
    "Owner", "Review Status", "Reviewer Notes",
}
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
CENTRAL_TZ = ZoneInfo("America/Chicago")


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def norm(value):
    return re.sub(r"[^a-z0-9]", "", clean(value).lower())


def canonical_url(url):
    if not url:
        return ""
    parts = urlsplit(str(url).strip())
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/+$", "", parts.path) or "/"
    return urlunsplit((parts.scheme.lower(), host, path, "", ""))


def read_json(path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"WARNING: Could not read {path.name}: {exc}")
        return default


def save_json(path, value):
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)


def save_csv(path, records, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


def parse_date(value):
    value = clean(value)
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None


def accepted_statuses(config):
    statuses = config.get("accepted_statuses", ["Posted", "Open"])
    return {clean(status).lower() for status in statuses}


def is_open(record, config):
    status = clean(record.get("status")).lower()
    accepted = accepted_statuses(config)
    # If the CSV omits status, do not silently treat the record as open.
    if not status or status not in accepted:
        return False
    if config.get("exclude_expired", True):
        due = parse_date(record.get("due_date", ""))
        if due and due < datetime.now(CENTRAL_TZ).date():
            return False
    return True


def record_key(record):
    sid = clean(record.get("solicitation_id")).lower()
    if sid:
        return "id:" + sid
    url = canonical_url(record.get("url", ""))
    return "url:" + url if url else ""


def log_failure(failures, keyword, record, stage, reason):
    now = datetime.now(CENTRAL_TZ).isoformat(timespec="seconds")
    sid = clean(record.get("solicitation_id", ""))
    url = record.get("url", "")
    reason = clean(reason)[:1000]

    # Update the existing unresolved failure rather than creating a duplicate.
    for item in failures:
        if (
            clean(item.get("Solicitation ID", "")).lower() == sid.lower()
            and clean(item.get("Keyword", "")) == keyword
            and clean(item.get("Failure Stage", "")) == stage
            and clean(item.get("Retry Status", "")).lower() != "resolved"
        ):
            item["Failure Reason"] = reason
            item["Last Attempt"] = now
            if url:
                item["Official Source URL"] = url
            return

    failures.append({
        "Date Detected": now,
        "Keyword": keyword,
        "Solicitation ID": sid,
        "Official Source URL": url,
        "Failure Stage": stage,
        "Failure Reason": reason,
        "Retry Status": "Pending manual review",
        "Last Attempt": now,
    })


def resolve_failures(failures, solicitation_id):
    sid = clean(solicitation_id).lower()
    if not sid:
        return
    for item in failures:
        if clean(item.get("Solicitation ID", "")).lower() == sid:
            item["Retry Status"] = "Resolved"


def find_column(row, *aliases):
    fields = {norm(key): value for key, value in row.items()}
    for alias in aliases:
        value = fields.get(norm(alias))
        if value is not None:
            return clean(value)
    return ""


def parse_csv_record(row, keyword, base_url):
    sid = find_column(
        row, "Solicitation ID", "Solicitation Number",
        "Solicitation / Reference No.", "Bid Number", "ID",
    )
    title = find_column(
        row, "Title", "Solicitation Title", "Opportunity Title", "Name",
    )
    url = find_column(
        row, "Solicitation URL", "Official Source URL",
        "URL", "Link", "Details URL",
    )
    if not url and sid:
        url = base_url.rstrip("/") + "/" + quote(sid, safe="")

    return {
        "solicitation_id": sid,
        "title": title,
        "status": find_column(row, "Status"),
        "agency": find_column(
            row, "Agency", "Agency Name",
            "Agency/Texas SmartBuy Member Name",
        ),
        "agency_number": find_column(
            row, "Agency Number", "Agency/Texas SmartBuy Member Number",
        ),
        "posting_date": find_column(
            row, "Posting Date", "Solicitation Posting Date", "Published Date",
        ),
        "due_date": find_column(
            row, "Due Date", "Response Due Date", "Deadline Date",
        ),
        "due_time": find_column(
            row, "Due Time", "Response Due Time", "Deadline Time",
        ),
        "opportunity_type": find_column(
            row, "Solicitation Type", "Opportunity Type", "Type",
        ),
        "estimated_value": find_column(
            row, "Estimated Value", "Estimated Amount", "Value",
        ),
        "class_item_codes": find_column(
            row, "Class/Item Codes", "Class Item Codes", "NIGP Codes",
        ),
        "description": find_column(
            row, "Description", "Solicitation Description",
        ),
        "url": url,
        "matched_keywords": [keyword],
        "date_found": date.today().isoformat(),
    }


def _page_reports_no_results(page):
    """Return True only when the visible ESBD page explicitly indicates zero results."""
    try:
        text = clean(page.locator("body").inner_text()).lower()
    except Exception:
        return False

    no_result_phrases = (
        "no results found",
        "no matching results",
        "no records found",
        "no matching records",
        "no solicitations found",
        "no data available in table",
        "your search returned 0 results",
        "0 results found",
        "there are no results",
    )
    return any(phrase in text for phrase in no_result_phrases)


def _solicitation_links(page):
    """Collect detail links from the currently displayed ESBD results page."""
    anchors = page.locator("body a[href]:visible").evaluate_all(
        """els => els.map(a => ({
            href: a.getAttribute('href') || '',
            text: (a.innerText || a.textContent || '').trim()
        }))"""
    )
    found = {}
    for anchor in anchors:
        href = clean(anchor.get("href", ""))
        if not href:
            continue
        absolute = urljoin(page.url, href)
        parts = urlsplit(absolute)
        path = re.sub(r"/+$", "", parts.path)
        match = re.fullmatch(r"/esbd/([^/]+)", path, re.I)
        if not match:
            continue
        solicitation_id = clean(match.group(1))
        if not solicitation_id or solicitation_id.lower() in {
            "search", "solicitations", "awards", "pre-solicitations"
        }:
            continue
        # Keep query/fragment off canonical detail URLs.
        detail_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        found[solicitation_id.lower()] = {
            "solicitation_id": solicitation_id,
            "title": clean(anchor.get("text", "")),
            "url": detail_url,
        }
    return found


def _page_number_urls(page):
    """Find page numbers exposed by pagination links, without assuming a CSV export."""
    numbers = {1}
    anchors = page.locator("body a[href]:visible").evaluate_all(
        """els => els.map(a => a.getAttribute('href') || '')"""
    )
    for href in anchors:
        absolute = urljoin(page.url, href)
        query = parse_qs(urlsplit(absolute).query)
        for value in query.get("page", []):
            try:
                number = int(value)
                if 1 <= number <= 100:
                    numbers.add(number)
            except (TypeError, ValueError):
                pass
    return sorted(numbers)


def search_keyword(page, keyword, config):
    """
    Search ESBD and collect solicitation detail links from the HTML.
    This deliberately does not rely on the CSV download button because the
    site did not start a Playwright download in the GitHub Actions environment.
    """
    base_url = config.get("search_url", "https://www.txsmartbuy.gov/esbd")
    page.goto(base_url, wait_until="domcontentloaded", timeout=60000)
    # ESBD pages may contain a hidden duplicate #content element from the
    # shopping layout. Use visible form controls instead of relying on that ID.
    page.locator("body").wait_for(state="visible", timeout=30000)
    keyword_candidates = page.locator(
        'input:visible[aria-label*="keyword" i], '
        'input:visible[placeholder*="keyword" i], '
        'input:visible[name*="keyword" i], '
        'input:visible[id*="keyword" i]'
    )
    if keyword_candidates.count() != 1:
        inputs = page.locator("input:visible").evaluate_all(
            """els => els.map(e => ({
                id: e.id, name: e.name, placeholder: e.placeholder,
                ariaLabel: e.getAttribute('aria-label'), type: e.type
            }))"""
        )
        raise RuntimeError(
            "Could not uniquely identify the visible ESBD Keyword Search field. "
            f"Visible inputs found: {inputs}"
        )

    keyword_box = keyword_candidates.first
    keyword_box.fill(keyword)
    search_buttons = page.get_by_role(
        "button", name=re.compile(r"^Search$", re.I)
    ).locator(":visible")
    if search_buttons.count() != 1:
        raise RuntimeError(
            "Expected one visible ESBD Search button; "
            f"found {search_buttons.count()}"
        )
    search_buttons.first.click()
    try:
        page.wait_for_load_state("domcontentloaded", timeout=10000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(1800)

    if _page_reports_no_results(page):
        print(f"{keyword}: 0 results (ESBD explicitly reported no matches)")
        return []

    # The search URL usually contains page=1 and keyword=<term>. Use visible
    # pagination links when present, and scrape each page's detail links.
    current_parts = urlsplit(page.url)
    current_query = parse_qs(current_parts.query, keep_blank_values=True)
    page_numbers = _page_number_urls(page)
    if not current_query.get("keyword"):
        current_query["keyword"] = [keyword]
    if not current_query.get("page"):
        current_query["page"] = ["1"]

    found = {}
    for page_number in page_numbers:
        query = {key: values[-1] if values else "" for key, values in current_query.items()}
        query["page"] = str(page_number)
        page_url = urlunsplit((
            current_parts.scheme or "https",
            current_parts.netloc or "www.txsmartbuy.gov",
            current_parts.path or "/esbd",
            urlencode(query),
            "",
        ))
        if page.url != page_url:
            page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
            page.locator("body").wait_for(state="visible", timeout=30000)
            page.wait_for_timeout(700)

        if _page_reports_no_results(page):
            continue

        links = _solicitation_links(page)
        for key, item in links.items():
            found.setdefault(key, item)

    # Some pages only reveal pagination after initial results render. If no
    # detail links were found, distinguish an explicit empty search from a
    # changed page layout or a website/network problem.
    if not found:
        if _page_reports_no_results(page):
            print(f"{keyword}: 0 results (ESBD explicitly reported no matches)")
            return []
        excerpt = clean(page.locator("body").inner_text())[:1200]
        raise RuntimeError(
            "No solicitation detail links were found and the page did not "
            "explicitly report zero results. The ESBD layout or search may "
            f"have changed. Current URL: {page.url}. Page excerpt: {excerpt}"
        )

    records = []
    for item in found.values():
        records.append({
            "solicitation_id": item["solicitation_id"],
            "title": item["title"],
            "status": "",
            "agency": "",
            "agency_number": "",
            "posting_date": "",
            "due_date": "",
            "due_time": "",
            "opportunity_type": "",
            "estimated_value": "",
            "class_item_codes": "",
            "description": "",
            "url": item["url"],
            "matched_keywords": [keyword],
            "date_found": date.today().isoformat(),
        })

    print(f"{keyword}: collected {len(records)} solicitation links from ESBD")
    return records

def extract_label(lines, *labels):
    wanted = {clean(label).lower().rstrip(":") for label in labels}
    for index, line in enumerate(lines):
        if clean(line).lower().rstrip(":") in wanted:
            for following in lines[index + 1:]:
                if clean(following):
                    return clean(following)
    return ""


def extract_description(lines):
    collecting = False
    parts = []
    stops = {"attachments", "download pdf", "follow solicitation"}
    for line in lines:
        stripped = clean(line)
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
        page.goto(record["url"], wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(700)
        lines = page.locator("body").inner_text().splitlines()

        for heading in page.locator("h1, h2").all_text_contents():
            heading = clean(heading)
            if heading and "electronic state business daily" not in heading.lower():
                record["title"] = heading
                break

        fields = {
            "status": ("Status",),
            "agency": ("Agency/Texas SmartBuy Member Name", "Agency"),
            "agency_number": ("Agency/Texas SmartBuy Member Number",),
            "posting_date": ("Solicitation Posting Date", "Posting Date"),
            "due_date": ("Response Due Date", "Due Date"),
            "due_time": ("Response Due Time", "Due Time"),
            "opportunity_type": ("Solicitation Type", "Type"),
            "estimated_value": ("Estimated Value", "Estimated Amount"),
        }
        for target, labels in fields.items():
            extracted = extract_label(lines, *labels)
            if extracted:
                record[target] = extracted

        description = extract_description(lines)
        if description:
            record["description"] = description

        record["detail_read_ok"] = True
        resolve_failures(failures, record.get("solicitation_id", ""))
    except Exception as exc:
        record["detail_read_ok"] = False
        log_failure(failures, keyword, record, "Detail extraction", str(exc))
        print(f"DETAIL FAILURE: {record.get('solicitation_id')} — {exc}")
    return record


def to_opportunity_row(record):
    row = {header: "" for header in HEADERS}
    row.update({
        "Opportunity ID": record.get("solicitation_id", ""),
        "Date Found": record.get("date_found", date.today().isoformat()),
        "State": "Texas",
        "Agency": record.get("agency", ""),
        "Opportunity Title": record.get("title", ""),
        "Solicitation / Reference No.": record.get("solicitation_id", ""),
        "Opportunity Type": record.get("opportunity_type", ""),
        "Service Category": ", ".join(record.get("matched_keywords", [])),
        "Official Source URL": record.get("url", ""),
        "Published Date": record.get("posting_date", ""),
        "Deadline Date": record.get("due_date", ""),
        "Deadline Time / Time Zone": record.get("due_time", ""),
        "Estimated Value (if published)": record.get("estimated_value", ""),
        "Scope Summary": record.get("description", ""),
        "Mandatory Gate (Pass / Fail / Unknown)": "Unknown",
    })
    for header in NEVER_AUTO_FILL:
        row[header] = ""
    return row


def connect_google(config):
    secret = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not secret:
        raise RuntimeError("GitHub secret GOOGLE_SERVICE_ACCOUNT_JSON is missing")
    try:
        service_info = json.loads(secret)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON. "
            "Paste the complete service-account JSON into the GitHub secret."
        ) from exc

    credentials = Credentials.from_service_account_info(
        service_info, scopes=SCOPES
    )
    client = gspread.authorize(credentials)
    spreadsheet = client.open_by_key(config["spreadsheet_id"])
    return spreadsheet


def sync_opportunities(spreadsheet, config, history):
    worksheet_id = int(config["worksheet_id"])
    sheet = next(
        (ws for ws in spreadsheet.worksheets()
         if ws.id == worksheet_id),
        None,
    )
    if sheet is None:
        raise RuntimeError(
            f"Configured main worksheet ID {worksheet_id} was not found. "
            "Check the gid in the Google Sheet URL."
        )

    values = sheet.get_all_values()
    if not values:
        sheet.append_row(HEADERS, value_input_option="RAW")
        values = [HEADERS]

    header = values[0]
    if header[:len(HEADERS)] != HEADERS:
        raise RuntimeError(
            "Main sheet headers do not match the expected first 35 columns "
            "in the required order. No opportunity rows were appended."
        )

    positions = {name: index for index, name in enumerate(header)}
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

    additions = []
    for record in history:
        if not is_open(record, config):
            continue
        row_dict = to_opportunity_row(record)
        sid = clean(record.get("solicitation_id")).lower()
        url = canonical_url(record.get("url", ""))
        if sid and sid in existing_ids:
            continue
        if url and url in existing_urls:
            continue
        additions.append([row_dict.get(col, "") for col in header])
        if sid:
            existing_ids.add(sid)
        if url:
            existing_urls.add(url)

    if additions:
        sheet.append_rows(additions, value_input_option="RAW")
    print(f"Google Sheets: appended {len(additions)} new opportunities")


def sync_manual_review(spreadsheet, config, failures):
    name = config.get("manual_review_worksheet", "ESBD Manual Review")
    try:
        sheet = spreadsheet.worksheet(name)
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(
            title=name, rows=max(2000, len(failures) + 10),
            cols=len(MANUAL_HEADERS),
        )

    values = sheet.get_all_values()
    if not values or not any(clean(cell) for cell in values[0]):
        sheet.update("A1", [MANUAL_HEADERS], value_input_option="RAW")
        values = [MANUAL_HEADERS]
    else:
        # Preserve existing columns/data. Add any missing required headers
        # to the right instead of rejecting the whole run.
        header = list(values[0])
        missing = [col for col in MANUAL_HEADERS if col not in header]
        if missing:
            start_col = len(header) + 1
            end_col = start_col + len(missing) - 1
            def col_letter(n):
                result = ""
                while n:
                    n, rem = divmod(n - 1, 26)
                    result = chr(65 + rem) + result
                return result
            sheet.update(
                f"{col_letter(start_col)}1:{col_letter(end_col)}1",
                [missing],
                value_input_option="RAW",
            )
            values = sheet.get_all_values()
            header = values[0]

    header = values[0]
    positions = {name: index for index, name in enumerate(header)}
    existing_keys = set()
    for row in values[1:]:
        def cell(col):
            idx = positions[col]
            return row[idx] if len(row) > idx else ""
        existing_keys.add((
            clean(cell("Keyword")),
            clean(cell("Solicitation ID")).lower(),
            clean(cell("Failure Stage")),
        ))

    additions = []
    for failure in failures:
        key = (
            clean(failure.get("Keyword", "")),
            clean(failure.get("Solicitation ID", "")).lower(),
            clean(failure.get("Failure Stage", "")),
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
    if not config.get("spreadsheet_id") or not config.get("worksheet_id"):
        raise RuntimeError("config.json must contain spreadsheet_id and worksheet_id")
    if not config.get("keywords"):
        raise RuntimeError("config.json must contain the ESBD keyword list")

    history = read_json(HISTORY_JSON, [])
    failures = read_json(FAILURES_JSON, [])
    history_by_key = {
        record_key(item): item for item in history if record_key(item)
    }
    discovered = {}
    search_failures = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(accept_downloads=True)
        try:
            for keyword in config["keywords"]:
                try:
                    records = search_keyword(page, keyword, config)
                except Exception as exc:
                    log_failure(failures, keyword, {}, "Search/export", str(exc))
                    search_failures.append(keyword)
                    print(f"SEARCH FAILURE for {keyword}: {exc}")
                    continue

                for record in records:
                    sid = clean(record.get("solicitation_id", ""))
                    if not sid:
                        log_failure(
                            failures, keyword, record, "CSV parsing",
                            "CSV row had no recognized solicitation ID",
                        )
                        continue
                    key = record_key(record)
                    if not key:
                        continue
                    if key not in discovered:
                        discovered[key] = record
                    elif keyword not in discovered[key]["matched_keywords"]:
                        discovered[key]["matched_keywords"].append(keyword)
                time.sleep(float(config.get("request_delay_seconds", 2)))

            # Read each detail page BEFORE filtering by status/deadline.
            # Search result cards may not expose those fields.
            candidates = list(discovered.values())
            candidates.sort(key=lambda record: (
                record_key(record) in history_by_key,
                record.get("posting_date", ""),
            ))
            cap = int(config.get("max_details_per_run", 100))
            if len(candidates) > cap:
                print(
                    f"WARNING: {len(candidates)} solicitation links found; "
                    f"processing {cap} detail pages this run."
                )

            for record in candidates[:cap]:
                keyword = record["matched_keywords"][0]
                enrich_detail(page, record, keyword, failures)

                if record.get("detail_read_ok") and is_open(record, config) and record.get("solicitation_id"):
                    key = record_key(record)
                    if key in history_by_key:
                        old = history_by_key[key]
                        old_keywords = set(old.get("matched_keywords", []))
                        old_keywords.update(record.get("matched_keywords", []))
                        old["matched_keywords"] = sorted(old_keywords)
                        # Keep existing nonempty values unless fresh data is available.
                        for field, value in record.items():
                            if value not in ("", None, []):
                                old[field] = value
                    else:
                        history_by_key[key] = record
                time.sleep(float(config.get("request_delay_seconds", 2)))
        finally:
            browser.close()

    history = list(history_by_key.values())
    save_json(HISTORY_JSON, history)
    save_csv(HISTORY_CSV, [{
        "Opportunity ID": r.get("solicitation_id", ""),
        "Opportunity Title": r.get("title", ""),
        "Status": r.get("status", ""),
        "Agency": r.get("agency", ""),
        "Published Date": r.get("posting_date", ""),
        "Deadline Date": r.get("due_date", ""),
        "Official Source URL": r.get("url", ""),
        "Matched Keywords": ", ".join(r.get("matched_keywords", [])),
        "Date Found": r.get("date_found", ""),
    } for r in history], [
        "Opportunity ID", "Opportunity Title", "Status", "Agency",
        "Published Date", "Deadline Date", "Official Source URL",
        "Matched Keywords", "Date Found",
    ])
    save_json(FAILURES_JSON, failures)
    save_csv(FAILURES_CSV, failures, MANUAL_HEADERS)

    spreadsheet = connect_google(config)
    sync_opportunities(spreadsheet, config, history)
    sync_manual_review(spreadsheet, config, failures)

    print(f"Unique history records: {len(history)}")
    print(f"Failure/manual-review records: {len(failures)}")
    if search_failures:
        raise RuntimeError(
            "One or more ESBD searches failed: " + ", ".join(search_failures)
        )


if __name__ == "__main__":
    main()
