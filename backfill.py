"""
Backfill last N days of sales into Feishu Bitable.

  days 1-3  →  GET_VENDOR_REAL_TIME_SALES_REPORT  (yesterday-ish, ordered only)
  days 4-7  →  GET_VENDOR_SALES_REPORT             (older, has shipped + ordered)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from vendor_report import (
    CST,
    FetchResult,
    download_report,
    load_asin_names,
    marketplace_config,
    normalize_sales_report,
    poll_report,
    require_env,
    request_report,
    sp_credentials,
    write_to_bitable,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DAYS_BACK = int(os.environ.get("BACKFILL_DAYS_BACK", "7"))
DAYS_START = int(os.environ.get("BACKFILL_DAYS_START", "1"))  # inclusive, oldest relative day to fetch
REALTIME_CUTOFF = 3  # use real-time for days 1..3, sales report for 4+

SALES_OPTIONS = {
    "reportPeriod": "DAY",
    "distributorView": "MANUFACTURING",
    "sellingProgram": "RETAIL",
}
RT_OPTIONS = {
    "distributorView": "MANUFACTURING",
    "sellingProgram": "RETAIL",
}


def fetch_day(
    date: datetime,
    report_type: str,
    options: dict,
    credentials: dict,
    marketplace,
) -> FetchResult:
    from datetime import timezone as tz
    start = date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=tz.utc)
    end   = date.replace(hour=23, minute=59, second=59, microsecond=0, tzinfo=tz.utc)

    report_id = request_report(report_type, start, end, credentials, marketplace, options)
    if not report_id:
        return FetchResult(ok=False, message=f"Failed to create {report_type}")

    doc_id, status = poll_report(report_id, credentials, marketplace)
    if not doc_id:
        return FetchResult(ok=False, message=f"{report_type} status={status}", report_id=report_id, status=status)

    payload = download_report(doc_id, credentials, marketplace)
    if payload is None:
        return FetchResult(ok=False, message="Download failed", report_id=report_id, status=status)

    rows = normalize_sales_report(payload)
    return FetchResult(ok=True, rows=rows, report_id=report_id, status=status)


def main():
    env = require_env()
    marketplace, marketplace_name = marketplace_config()
    credentials = sp_credentials(env)
    xlsx_path = os.environ.get(
        "ASIN_NAMES_XLSX",
        os.path.join(os.path.dirname(__file__), "Xiaomi电视产品价格表2025-2026.xlsx"),
    )
    asin_names = load_asin_names(xlsx_path)

    today = datetime.now(CST)
    logger.info("Backfill start: %s days, marketplace=%s", DAYS_BACK, marketplace_name)

    for days_ago in range(DAYS_START, DAYS_BACK + 1):
        date = today - timedelta(days=days_ago)
        date_str = date.strftime("%Y-%m-%d")

        if days_ago <= REALTIME_CUTOFF:
            report_type, options = "GET_VENDOR_REAL_TIME_SALES_REPORT", RT_OPTIONS
        else:
            report_type, options = "GET_VENDOR_SALES_REPORT", SALES_OPTIONS

        logger.info("── %s  (-%d days)  %s", date_str, days_ago, report_type)
        result = fetch_day(date, report_type, options, credentials, marketplace)
        logger.info("   ok=%s  rows=%s  %s", result.ok, len(result.rows), result.message)

        if result.ok and result.rows:
            write_to_bitable(result, date, asin_names, env["FEISHU_APP_ID"], env["FEISHU_APP_SECRET"])
        else:
            logger.warning("   Skipped Bitable write for %s", date_str)


if __name__ == "__main__":
    main()
