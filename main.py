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

CORE_HEADERS = [
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
# Extra ESBD detail fields are appended to the right of the original 35 columns.
# This keeps existing sheets compatible while preserving the detailed source fields.
DETAIL_HEADERS = [
    "Stored At (America/Chicago)",
    "Stored At (Asia/Kolkata)",
    "Last Modified",
    "Solicitation Status",
    "Contact Name",
    "Contact Number",
    "Contact Email",
    "Bid Response Email",
    "Bid Response URL",
    "Posting Requirement",
    "State Agency Procurement Certification",
    "Class/Item Codes",
    "Highway Districts",
    "Zones",
    "Addendum Details",
    "Keyword Relevance",
    "Relevance Notes",
    "Detail Extraction Status",
    "Source Page Title",
]
HEADERS = CORE_HEADERS + DETAIL_HEADERS

# These six fields are intentionally never auto-filled.
NEVER_AUTO_FILL = {
    "Risks / Gaps", "Next Action", "Last Verified Date",
    "Owner", "Review Status", "Reviewer Notes",
}
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
CENTRAL_TZ = ZoneInfo("America/Chicago")
INDIA_TZ = ZoneInfo("Asia/Kolkata")
UTC_TZ = ZoneInfo("UTC")


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


def persist_local_progress(history_by_key, failures):
    """Persist progress during the run so cancellation doesn't discard all work."""
    history_now = list(history_by_key.values())
    save_json(HISTORY_JSON, history_now)
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
        "Stored At (UTC)": r.get("stored_at_utc", ""),
        "Stored At (America/Chicago)": r.get("stored_at_local", ""),
    } for r in history_now], [
        "Opportunity ID", "Opportunity Title", "Status", "Agency",
        "Published Date", "Deadline Date", "Official Source URL",
        "Matched Keywords", "Date Found", "Stored At (UTC)",
        "Stored At (America/Chicago)",
    ])
    save_json(FAILURES_JSON, failures)
    save_csv(FAILURES_CSV, failures, MANUAL_HEADERS)


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
    """Strict check used to distinguish confirmed open records."""
    status = clean(record.get("status")).lower()
    accepted = accepted_statuses(config)
    if not status or status not in accepted:
        return False
    if config.get("exclude_expired", True):
        due = parse_date(record.get("due_date", ""))
        if due and due < datetime.now(CENTRAL_TZ).date():
            return False
    return True


def is_candidate_for_review(record, config):
    """Keep verified open records and uncertain-status records for human review.

    Explicitly closed/cancelled/awarded/withdrawn/expired opportunities are
    excluded. Missing or unfamiliar statuses are retained as Investigate rather
    than silently dropping every result when ESBD's labels differ from config.
    Past deadlines are excluded when exclude_expired is enabled.
    """
    if not clean(record.get("solicitation_id")) or not clean(record.get("url")):
        return False
    if config.get("exclude_expired", True):
        due = parse_date(record.get("due_date", ""))
        if due and due < datetime.now(CENTRAL_TZ).date():
            return False
    status = clean(record.get("status")).lower()
    if not status:
        return True
    if status in accepted_statuses(config):
        return True
    closed_terms = (
        "closed", "cancelled", "canceled", "awarded", "withdrawn",
        "expired", "terminated", "rejected", "completed",
    )
    if any(term in status for term in closed_terms):
        return False
    # A nonempty but unfamiliar status is retained for manual investigation.
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
        "stored_at_utc": datetime.now(UTC_TZ).replace(microsecond=0).isoformat(),
        "stored_at_local": datetime.now(UTC_TZ).astimezone(CENTRAL_TZ).replace(microsecond=0).isoformat(),
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


def wait_for_esbd_app(page, timeout=30000, stage="page"):
    """Wait for the actual ESBD application container, not merely <body>."""
    try:
        page.locator("#main").wait_for(state="visible", timeout=timeout)
        return
    except Exception as exc:
        print(f"ESBD APP NOT VISIBLE during {stage}: {exc}", flush=True)
        print(f"DEBUG URL [{stage}]: {page.url}", flush=True)
        try:
            print(f"DEBUG TITLE [{stage}]: {page.title()}", flush=True)
            print(
                f"DEBUG #main state [{stage}]: ",
                page.locator("#main").evaluate(
                    "el => ({display:getComputedStyle(el).display, visibility:getComputedStyle(el).visibility, height:el.getBoundingClientRect().height, html:el.outerHTML.slice(0,1200)})"
                ) if page.locator("#main").count() else "#main not found",
                flush=True,
            )
        except Exception as detail_exc:
            print(f"DEBUG app diagnostics unavailable [{stage}]: {detail_exc}", flush=True)
        try:
            page.screenshot(path=f"esbd_debug_{re.sub(r'[^a-z0-9]+', '_', stage.lower())}.png", full_page=True, timeout=10000)
            print(f"DEBUG screenshot saved for stage: {stage}", flush=True)
        except Exception as shot_exc:
            print(f"DEBUG screenshot failed [{stage}]: {shot_exc}", flush=True)
        raise RuntimeError(
            f"ESBD JavaScript application did not become visible during {stage}. "
            "See browser console, pageerror, requestfailed, and HTTP ERROR logs."
        ) from exc


def search_keyword(page, keyword, config):
    """
    Search ESBD and collect solicitation detail links from the HTML.
    This deliberately does not rely on the CSV download button because the
    site did not start a Playwright download in the GitHub Actions environment.
    """
    base_url = config.get("search_url", "https://www.txsmartbuy.gov/esbd")
    print(f"DEBUG keyword: {keyword}", flush=True)
    print(f"DEBUG requested URL: {base_url}", flush=True)
    try:
        response = page.goto(base_url, wait_until="domcontentloaded", timeout=60000)
        print(f"DEBUG navigation status: {response.status if response else 'no response object'}", flush=True)
    except Exception as navigation_error:
        print(f"DEBUG navigation error: {type(navigation_error).__name__}: {navigation_error}", flush=True)
        print(f"DEBUG current URL after navigation error: {page.url}", flush=True)
        raise

    print(f"DEBUG final URL: {page.url}", flush=True)
    try:
        print(f"DEBUG page title: {page.title()}", flush=True)
    except Exception as title_error:
        print(f"DEBUG title error: {title_error}", flush=True)

    # The <body> can be visible while the actual app is hidden. Wait for #main.
    wait_for_esbd_app(page, timeout=45000, stage=f"initial page for {keyword}")

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

    # Do not search for a global button named "Search": the site can render
    # hidden duplicate controls and custom submit controls. Prefer the submit
    # control associated with this exact input's form; if it has no usable
    # submit control, submit the keyword field with Enter.
    submitted = False
    try:
        form = keyword_box.locator("xpath=ancestor::form[1]")
        if form.count() > 0:
            submit_controls = form.locator(
                'button[type="submit"], input[type="submit"], '
                'button:visible, input[type="button"]:visible, [role="button"]:visible'
            )
            visible_controls = []
            for index in range(submit_controls.count()):
                control = submit_controls.nth(index)
                try:
                    if control.is_visible() and re.search(
                        r"search", clean(control.inner_text() or control.get_attribute("value") or control.get_attribute("aria-label") or ""), re.I
                    ):
                        visible_controls.append(control)
                except Exception:
                    continue
            if len(visible_controls) == 1:
                visible_controls[0].click(timeout=30000)
                submitted = True
    except Exception as exc:
        print(f"INFO: Form-specific Search click unavailable for {keyword}: {exc}")

    if not submitted:
        keyword_box.press("Enter", timeout=30000)

    try:
        page.wait_for_load_state("domcontentloaded", timeout=30000)
    except PlaywrightTimeoutError:
        pass
    # Wait for client-rendered app and results; a fixed sleep alone is not enough.
    wait_for_esbd_app(page, timeout=45000, stage=f"results for {keyword}")
    page.wait_for_timeout(1500)

    if _page_reports_no_results(page):
        print(f"{keyword}: 0 results (ESBD explicitly reported no matches)")
        return []

    # Scrape the current results page first. For subsequent pages, click the
    # visible pagination link rather than navigating directly to a URL: this
    # site hides its application shell on direct query-string navigation until
    # its client-side router initializes.
    page_numbers = _page_number_urls(page)
    found = {}

    def collect_current_page():
        if _page_reports_no_results(page):
            return
        for key, item in _solicitation_links(page).items():
            found.setdefault(key, item)

    collect_current_page()
    for page_number in [number for number in page_numbers if number != 1]:
        if len(found) >= int(config.get("max_search_results_per_keyword", 100)):
            print(f"INFO: Reached max_search_results_per_keyword for {keyword}", flush=True)
            break
        # Match a visible pagination anchor by its URL query parameter.
        candidates = page.locator("body a[href]:visible")
        target = None
        for idx in range(candidates.count()):
            link = candidates.nth(idx)
            href = link.get_attribute("href") or ""
            absolute = urljoin(page.url, href)
            query = parse_qs(urlsplit(absolute).query)
            if query.get("page", [""])[-1] == str(page_number):
                target = link
                break
        if target is None:
            print(f"INFO: Could not find visible pagination link for page {page_number}; stopping pagination", flush=True)
            break
        try:
            target.click(timeout=15000)
            wait_for_esbd_app(page, timeout=30000, stage=f"pagination page {page_number} for {keyword}")
            page.wait_for_timeout(1200)
            collect_current_page()
        except Exception as pagination_error:
            print(f"PAGINATION WARNING for {keyword}, page {page_number}: {pagination_error}", flush=True)
            break

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
            "contact_name": "", "contact_number": "", "contact_email": "",
            "bid_response_email": "", "bid_response_url": "", "posting_requirement": "",
            "procurement_certification": "", "last_modified": "", "highway_districts": "",
            "zones": "", "addendum_details": "", "keyword_relevance": "Unverified / likely unrelated",
            "relevance_notes": "Not yet checked against detail-page content.",
            "detail_extraction_status": "Pending detail extraction", "source_page_title": "",
            "url": item["url"],
            "matched_keywords": [keyword],
            "date_found": date.today().isoformat(),
            "stored_at_utc": datetime.now(UTC_TZ).replace(microsecond=0).isoformat(),
            "stored_at_local": datetime.now(UTC_TZ).astimezone(CENTRAL_TZ).replace(microsecond=0).isoformat(),
        })

    print(f"{keyword}: collected {len(records)} solicitation links from ESBD", flush=True)
    return records

def extract_label(lines, *labels):
    """Extract a labelled value from ESBD's visible text, handling inline labels too."""
    wanted = {clean(label).lower().rstrip(":") for label in labels}
    # ESBD can render a label and value on one line or on separate lines.
    for index, raw_line in enumerate(lines):
        line = clean(raw_line).replace("\xa0", " ")
        normalized = line.lower().strip()
        for label in sorted(wanted, key=len, reverse=True):
            if normalized == label or normalized == label + ":":
                for following in lines[index + 1:]:
                    value = clean(following).replace("\xa0", " ").strip(" :")
                    if value:
                        # Do not accidentally capture the next label as a value.
                        if value.lower().rstrip(":") in wanted:
                            break
                        return value
            prefix = label + ":"
            if normalized.startswith(prefix):
                inline = line[len(prefix):].strip(" :\t")
                if inline:
                    return inline
    return ""


def extract_multiline_field(lines, *labels):
    """Collect a multiline value until the next recognizable ESBD label."""
    wanted = {clean(label).lower().rstrip(":") for label in labels}
    common_labels = {
        "solicitation id", "status", "contact name", "contact number", "contact email",
        "bid response email", "response due date", "response due time",
        "agency/texas smartbuy member number", "agency/texas smartbuy member name",
        "posting requirement", "state agency procurement certification",
        "solicitation posting date", "last modified", "class/item code", "class/item codes",
        "highway districts", "zones", "bid response url", "addendum",
        "solicitation description", "attachments", "download pdf", "follow solicitation",
        "bid response url", "solicitation type", "estimated value", "estimated amount",
    }
    for index, raw_line in enumerate(lines):
        line = clean(raw_line).replace("\xa0", " ")
        normalized = line.lower().rstrip(":").strip()
        matched = next((label for label in wanted if normalized == label), None)
        if not matched:
            continue
        parts = []
        for raw_value in lines[index + 1:]:
            value = clean(raw_value).replace("\xa0", " ")
            normalized_value = value.lower().rstrip(":").strip()
            if normalized_value in common_labels or any(normalized_value.startswith(x + ":") for x in common_labels):
                break
            if value:
                parts.append(value)
        return clean(" ".join(parts))
    return ""


def extract_description(lines):
    return extract_multiline_field(lines, "Solicitation Description", "Description")


def keyword_relevance(record):
    """Conservative text signal, not a promise that the bid is a fit."""
    text = clean(" ".join([
        record.get("title", ""), record.get("description", ""),
        record.get("class_item_codes", ""), record.get("opportunity_type", ""),
    ])).lower()
    groups = {
        "healthcare IT": ("healthcare it", "health information", "health informatics", "clinical system", "medical record", "patient portal", "hospital information system"),
        "health data analytics": ("health data", "clinical data", "healthcare analytics", "medical analytics", "population health"),
        "EHR": ("ehr", "electronic health record", "electronic medical record", "emr", "epic", "cerner", "oracle health"),
        "interoperability": ("interoperability", "hl7", "fhir", "health information exchange", "interface engine"),
        "cybersecurity": ("cybersecurity", "cyber security", "information security", "security assessment", "penetration test", "siem", "zero trust"),
    }
    hits = [group for group, terms in groups.items() if any(term in text for term in terms)]
    if hits:
        return "Potential match", "Content contains: " + ", ".join(hits)
    return "Unverified / likely unrelated", "Search result was returned for a configured keyword, but title/description does not independently confirm healthcare IT relevance. Review manually."


def enrich_detail(page, record, keyword, failures):
    try:
        page.goto(record["url"], wait_until="domcontentloaded", timeout=120000)
        # Wait for the ESBD client-rendered page, not just the initial HTML shell.
        try:
            page.locator("#main").wait_for(state="visible", timeout=45000)
        except PlaywrightTimeoutError:
            pass
        try:
            page.locator("h1, h2").first.wait_for(state="visible", timeout=20000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(900)
        lines = [clean(line).replace("\xa0", " ") for line in page.locator("body").inner_text(timeout=30000).splitlines()]

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
            "contact_name": ("Contact Name",),
            "contact_number": ("Contact Number",),
            "contact_email": ("Contact Email",),
            "bid_response_email": ("Bid Response Email",),
            "bid_response_url": ("Bid Response URL", "Bid Response URL"),
            "posting_requirement": ("Posting Requirement",),
            "procurement_certification": ("State Agency Procurement Certification",),
            "last_modified": ("Last Modified",),
            "class_item_codes": ("Class/Item Code", "Class/Item Codes", "Class Item Code", "Class Item Codes"),
            "highway_districts": ("Highway Districts",),
            "zones": ("Zones",),
            "addendum_details": ("Addendum",),
        }
        for target, labels in fields.items():
            extracted = extract_label(lines, *labels)
            if target in {"highway_districts", "zones", "addendum_details"}:
                multiline = extract_multiline_field(lines, *labels)
                if multiline:
                    extracted = multiline
            elif not extracted and target == "class_item_codes":
                extracted = extract_multiline_field(lines, *labels)
            if extracted:
                record[target] = extracted

        description = extract_description(lines)
        if description:
            record["description"] = description

        record["source_page_title"] = clean(page.title())
        record["detail_read_ok"] = True
        record["detail_extraction_status"] = "Completed; some fields may be unavailable on the source page"
        relevance, relevance_notes = keyword_relevance(record)
        record["keyword_relevance"] = relevance
        record["relevance_notes"] = relevance_notes
        resolve_failures(failures, record.get("solicitation_id", ""))
        print(
            f"DETAIL EXTRACT: ID={record.get('solicitation_id')} title={bool(record.get('title'))} "
            f"agency={bool(record.get('agency'))} due={bool(record.get('due_date'))} "
            f"description={bool(record.get('description'))} relevance={relevance}", flush=True
        )
    except Exception as exc:
        record["detail_read_ok"] = False
        record["detail_extraction_status"] = f"Failed: {type(exc).__name__}: {clean(exc)}"[:500]
        log_failure(failures, keyword, record, "Detail extraction", str(exc))
        print(f"DETAIL FAILURE: {record.get('solicitation_id')} — {exc}", flush=True)
    return record

def to_opportunity_row(record):
    now_utc = datetime.now(UTC_TZ).replace(microsecond=0)
    now_texas = now_utc.astimezone(CENTRAL_TZ)
    now_india = now_utc.astimezone(INDIA_TZ)
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
        "Recommendation (Go / Investigate / No-Go)": "Investigate",
        "Fit Rationale": "Verify status, eligibility, requirements, and relevance against the official solicitation before deciding.",
        "Stored At (America/Chicago)": now_texas.strftime(
    "%Y-%m-%d %I:%M:%S %p %Z"
),
"Stored At (Asia/Kolkata)": now_india.strftime(
    "%Y-%m-%d %I:%M:%S %p IST"
),
        "Last Modified": record.get("last_modified", ""),
        "Solicitation Status": record.get("status", ""),
        "Contact Name": record.get("contact_name", ""),
        "Contact Number": record.get("contact_number", ""),
        "Contact Email": record.get("contact_email", ""),
        "Bid Response Email": record.get("bid_response_email", ""),
        "Bid Response URL": record.get("bid_response_url", ""),
        "Posting Requirement": record.get("posting_requirement", ""),
        "State Agency Procurement Certification": record.get("procurement_certification", ""),
        "Class/Item Codes": record.get("class_item_codes", ""),
        "Highway Districts": record.get("highway_districts", ""),
        "Zones": record.get("zones", ""),
        "Addendum Details": record.get("addendum_details", ""),
        "Keyword Relevance": record.get("keyword_relevance", "Unverified / likely unrelated"),
        "Relevance Notes": record.get("relevance_notes", ""),
        "Detail Extraction Status": record.get("detail_extraction_status", "Not verified"),
        "Source Page Title": record.get("source_page_title", ""),
    })
    # Keep human-owned review fields empty; never invent scores or certifications.
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

    header = list(values[0])

    # Inspect the live worksheet header row instead of assuming the user's
    # existing 35 columns are in a particular order. Preserve all existing
    # columns and append only genuinely missing expected headers.
    existing_header_names = {clean(name) for name in header if clean(name)}
    missing_core_headers = [name for name in CORE_HEADERS if name not in existing_header_names]
    if missing_core_headers:
        print(
            "SHEETS HEADER CHECK: missing expected core columns; appending only these: "
            + json.dumps(missing_core_headers, ensure_ascii=False),
            flush=True,
        )

    # Append missing core fields first, then extra ESBD detail fields. Never
    # reorder/delete existing columns or overwrite the user's six manual fields.
    missing_headers = [name for name in HEADERS if name not in existing_header_names]
    if missing_headers:
        start_col = len(header) + 1
        end_col = start_col + len(missing_headers) - 1
        def col_letter(number):
            result = ""
            while number:
                number, remainder = divmod(number - 1, 26)
                result = chr(65 + remainder) + result
            return result
        # Google Sheets does not automatically expand a worksheet's grid.
        # Ensure the target columns exist before writing the new headers.
        if sheet.col_count < end_col:
            sheet.add_cols(end_col - sheet.col_count)
        sheet.update(
            range_name=f"{col_letter(start_col)}1:{col_letter(end_col)}1",
            values=[missing_headers],
            value_input_option="RAW",
        )
        values = sheet.get_all_values()
        header = list(values[0])

    positions = {name: index for index, name in enumerate(header)}
    print(f"SHEETS TARGET: title={sheet.title!r} worksheet_id={sheet.id}", flush=True)
    print(
        "SHEETS HEADER CHECK: "
        f"core_required={len(CORE_HEADERS)} total_headers={len(header)} "
        f"headers_added={json.dumps(missing_headers, ensure_ascii=False)}",
        flush=True,
    )
    print(
        "SHEETS EXISTING HEADER ROW: "
        + json.dumps(header, ensure_ascii=False),
        flush=True,
    )
    id_col = positions["Opportunity ID"]
    ref_col = positions["Solicitation / Reference No."]
    url_col = positions["Official Source URL"]

    existing_ids, existing_urls = set(), set()
    existing_row_by_id = {}
    existing_row_by_url = {}
    vacant_template_rows = []
    for row_number, row in enumerate(values[1:], start=2):
        # Reuse the user's pre-created template rows that contain only State=Texas.
        # This prevents valid opportunities being appended far below dozens of blank rows.
        meaningful_cells = [clean(cell) for idx, cell in enumerate(row) if clean(cell) and idx != positions.get("State", -1)]
        row_id = clean(row[id_col]) if len(row) > id_col else ""
        row_title = clean(row[positions["Opportunity Title"]]) if len(row) > positions["Opportunity Title"] else ""
        row_url = clean(row[url_col]) if len(row) > url_col else ""
        if not row_id and not row_title and not row_url and not meaningful_cells:
            vacant_template_rows.append(row_number)
        sid_values = []
        if len(row) > id_col and row[id_col]:
            sid_values.append(row[id_col].strip().lower())
        if len(row) > ref_col and row[ref_col]:
            sid_values.append(row[ref_col].strip().lower())
        for existing_id in sid_values:
            existing_ids.add(existing_id)
            existing_row_by_id.setdefault(existing_id, (row_number, row))
        if len(row) > url_col and row[url_col]:
            existing_url = canonical_url(row[url_col])
            existing_urls.add(existing_url)
            if existing_url:
                existing_row_by_url.setdefault(existing_url, (row_number, row))

    additions = []
    slot_fills = []
    backfills = []
    protected_manual = set(NEVER_AUTO_FILL)
    for record in history:
        if not is_candidate_for_review(record, config):
            continue
        row_dict = to_opportunity_row(record)
        sid = clean(record.get("solicitation_id")).lower()
        url = canonical_url(record.get("url", ""))
        existing_match = existing_row_by_id.get(sid) if sid else None
        if existing_match is None and url:
            existing_match = existing_row_by_url.get(url)

        if existing_match:
            row_number, old_values = existing_match
            merged = list(old_values) + [""] * max(0, len(header) - len(old_values))
            changed = False
            for column_name, column_index in positions.items():
                if column_name in protected_manual:
                    continue
                new_value = clean(row_dict.get(column_name, ""))
                old_value = clean(merged[column_index])
                # Backfill missing cells only; never overwrite populated cells or human edits.
                if not old_value and new_value:
                    merged[column_index] = row_dict.get(column_name, "")
                    changed = True
            if changed:
                backfills.append((row_number, merged[:len(header)]))
            continue

        new_row = [row_dict.get(col, "") for col in header]
        if vacant_template_rows:
            slot_fills.append((vacant_template_rows.pop(0), new_row))
        else:
            additions.append(new_row)
        if sid:
            existing_ids.add(sid)
        if url:
            existing_urls.add(url)
    
    if backfills or slot_fills:
        def col_letter(number):
            result = ""
            while number:
                number, remainder = divmod(number - 1, 26)
                result = chr(65 + remainder) + result
            return result

        last_col = col_letter(len(header))

        # Prepare all row updates for batched Google Sheets requests.
        updates = []

        for row_number, merged_row in backfills:
            updates.append({
                "range": f"A{row_number}:{last_col}{row_number}",
                "values": [merged_row],
            })

        for row_number, new_row in slot_fills:
            updates.append({
                "range": f"A{row_number}:{last_col}{row_number}",
                "values": [new_row],
            })

        # Send up to 50 row updates per API request.
        # Retry temporary quota/server errors with exponential backoff.
        batch_size = 50

        for start in range(0, len(updates), batch_size):
            batch = updates[start:start + batch_size]

            for attempt in range(5):
                try:
                    sheet.batch_update(
                        batch,
                        value_input_option="RAW",
                    )
                    print(
                        f"Google Sheets: batch "
                        f"{start // batch_size + 1} completed "
                        f"({len(batch)} row updates)",
                        flush=True,
                    )
                    break

                except gspread.exceptions.APIError as exc:
                    status = getattr(
                        getattr(exc, "response", None),
                        "status_code",
                        None,
                    )

                    retryable = status in (429, 500, 503)

                    if not retryable or attempt == 4:
                        raise

                    wait_seconds = min(60, 2 ** (attempt + 1))
                    print(
                        f"Google Sheets API returned HTTP {status}. "
                        f"Retrying in {wait_seconds} seconds "
                        f"(attempt {attempt + 2}/5).",
                        flush=True,
                    )
                    time.sleep(wait_seconds)

        if backfills:
            print(
                f"Google Sheets: backfilled "
                f"{len(backfills)} existing opportunity rows",
                flush=True,
            )

        if slot_fills:
            print(
                f"Google Sheets: filled "
                f"{len(slot_fills)} pre-existing blank template rows",
                flush=True,
            )


    if additions:
        first = additions[0]
        preview = {name: first[positions[name]] if positions[name] < len(first) else "" for name in (
            "Opportunity ID", "State", "Agency", "Opportunity Title", "Official Source URL",
            "Solicitation Status", "Contact Email", "Deadline Date", "Stored At (UTC)"
        ) if name in positions}
        print(f"SHEETS APPEND PREVIEW: {json.dumps(preview, ensure_ascii=False)}", flush=True)
        response = sheet.append_rows(additions, value_input_option="RAW", insert_data_option="INSERT_ROWS")
        print(f"SHEETS APPEND RESPONSE: {json.dumps(response, default=str, ensure_ascii=False)[:2000]}", flush=True)
    else:
        print("Google Sheets: no new rows to append (records may already exist by ID/URL or were written into template rows).", flush=True)

    # Always read back after appends, backfills, or template-row fills.
    try:
        refreshed = sheet.get_all_values()
        print(f"SHEETS READ-BACK: title={sheet.title!r} worksheet_id={sheet.id} total_rows={len(refreshed)}", flush=True)
        if len(refreshed) > 1:
            id_index = positions.get("Opportunity ID", 0)
            nonempty_rows = [row for row in refreshed[1:] if len(row) > id_index and row[id_index].strip()]
            sample_rows = nonempty_rows[-min(3, len(nonempty_rows)):]
            for sample in sample_rows:
                readback = {name: (sample[positions[name]] if positions[name] < len(sample) else "") for name in (
                    "Opportunity ID", "State", "Agency", "Opportunity Title", "Official Source URL",
                    "Solicitation Status", "Contact Email", "Deadline Date", "Stored At (UTC)"
                ) if name in positions}
                print(f"SHEETS NONEMPTY ROW READ-BACK: {json.dumps(readback, ensure_ascii=False)}", flush=True)
            if not nonempty_rows:
                print("SHEETS READ-BACK WARNING: no nonempty Opportunity ID found in the target worksheet after sync", flush=True)
    except Exception as verify_exc:
        print(f"SHEETS READ-BACK WARNING: {type(verify_exc).__name__}: {verify_exc}", flush=True)
    print(f"Google Sheets: appended {len(additions)} new opportunities; filled template rows={len(slot_fills)}; backfilled existing rows={len(backfills)}", flush=True)


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
    print(f"Manual review: appended {len(additions)} new failure records", flush=True)


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
        page.on(
            "console",
            lambda msg: print(
                f"BROWSER CONSOLE [{msg.type}]: {msg.text}",
                flush=True,
            ),
        )
        page.on(
            "pageerror",
            lambda exc: print(
                f"BROWSER JAVASCRIPT ERROR: {exc}",
                flush=True,
            ),
        )
        page.on(
            "requestfailed",
            lambda request: print(
                f"REQUEST FAILED: {request.url} | {request.failure}",
                flush=True,
            ),
        )
        page.on(
            "response",
            lambda response: print(
                f"HTTP ERROR: {response.status} {response.url}",
                flush=True,
            ) if response.status >= 400 else None,
        )
        page.set_default_timeout(30000)
        page.set_default_navigation_timeout(60000)
        try:
            for keyword in config["keywords"]:
                try:
                    records = search_keyword(page, keyword, config)
                except Exception as exc:
                    log_failure(failures, keyword, {}, "Search/export", str(exc))
                    search_failures.append(keyword)
                    print(f"SEARCH FAILURE for {keyword}: {exc}", flush=True)
                    continue

                for record in records:
                    sid = clean(record.get("solicitation_id", ""))
                    if not sid:
                        log_failure(
                            failures, keyword, record, "Result parsing",
                            "Collected result link had no recognized solicitation ID",
                        )
                        continue
                    key = record_key(record)
                    if not key:
                        continue
                    if key not in discovered:
                        discovered[key] = record
                    elif keyword not in discovered[key]["matched_keywords"]:
                        discovered[key]["matched_keywords"].append(keyword)
                persist_local_progress(history_by_key, failures)
                print(
                    f"Search stage complete for keyword: {keyword}; "
                    f"unique links so far: {len(discovered)}",
                    flush=True,
                )
                time.sleep(float(config.get("request_delay_seconds", 2)))

            # Read each detail page BEFORE filtering by status/deadline.
            # Search result cards may not expose those fields.
            candidates = list(discovered.values())
            candidates.sort(key=lambda record: (
                record_key(record) in history_by_key,
                record.get("posting_date", ""),
            ))
            cap = max(1, min(int(config.get("max_details_per_run", 20)), 100))
            if len(candidates) > cap:
                print(
                    f"WARNING: {len(candidates)} solicitation links found; "
                    f"processing {cap} detail pages this run."
                )

            for record in candidates[:cap]:
                keyword = record["matched_keywords"][0]
                enrich_detail(page, record, keyword, failures)

                print(
                    "DETAIL CHECK:",
                    f"ID={record.get('solicitation_id')}",
                    f"OK={record.get('detail_read_ok')}",
                    f"STATUS={record.get('status')!r}",
                    f"DEADLINE={record.get('due_date')!r}",
                    f"STRICT_OPEN={is_open(record, config)}",
                    f"REVIEW_CANDIDATE={is_candidate_for_review(record, config)}",
                    flush=True,
                )
                if record.get("detail_read_ok") and is_candidate_for_review(record, config) and record.get("solicitation_id"):
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
                        old.setdefault("stored_at_utc", datetime.now(UTC_TZ).replace(microsecond=0).isoformat())
                        old.setdefault("stored_at_local", datetime.now(UTC_TZ).astimezone(CENTRAL_TZ).replace(microsecond=0).isoformat())
                    else:
                        history_by_key[key] = record
                persist_local_progress(history_by_key, failures)
                print(
                    f"Detail stage progress: {record.get('solicitation_id', '')}; "
                    f"accepted history records: {len(history_by_key)}",
                    flush=True,
                )
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
        "Stored At (UTC)": r.get("stored_at_utc", ""),
        "Stored At (America/Chicago)": r.get("stored_at_local", ""),
    } for r in history], [
        "Opportunity ID", "Opportunity Title", "Status", "Agency",
        "Published Date", "Deadline Date", "Official Source URL",
        "Matched Keywords", "Date Found", "Stored At (UTC)",
        "Stored At (America/Chicago)",
    ])
    save_json(FAILURES_JSON, failures)
    save_csv(FAILURES_CSV, failures, MANUAL_HEADERS)

    spreadsheet = connect_google(config)
    sync_opportunities(spreadsheet, config, history)
    sync_manual_review(spreadsheet, config, failures)

    print(f"Unique history records saved locally: {len(history)}", flush=True)
    print(
        "Review candidates eligible for sheet sync: "
        f"{sum(1 for item in history if is_candidate_for_review(item, config))}",
        flush=True,
    )
    print(f"Failure/manual-review records: {len(failures)}", flush=True)
    if search_failures:
        raise RuntimeError(
            "One or more ESBD searches failed: " + ", ".join(search_failures)
        )


if __name__ == "__main__":
    main()
