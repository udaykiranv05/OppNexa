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

            candidates = [
                record for record in discovered.values()
                if is_open(record, config)
            ]
            candidates.sort(key=lambda record: (
                record_key(record) in history_by_key,
                record.get("posting_date", ""),
            ))
            cap = int(config.get("max_details_per_run", 100))
            if len(candidates) > cap:
                print(
                    f"WARNING: {len(candidates)} candidates found; "
                    f"processing {cap} details this run."
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
