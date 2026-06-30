"""
Amazon Vendor Central daily inventory report.

Pulls the Vendor Inventory report with SP-API and writes rows to a dedicated
Feishu Sheets spreadsheet. This job intentionally does not send Feishu chat
messages.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from vendor_report import (
    CST,
    FetchResult,
    download_report,
    feishu_token,
    github_run_label,
    load_asin_names,
    marketplace_config,
    money_amount,
    money_currency,
    poll_report,
    request_report,
    sp_credentials,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

REQUIRED_ENV = (
    "SP_LWA_APP_ID",
    "SP_LWA_CLIENT_SECRET",
    "SP_REFRESH_TOKEN",
    "SP_AWS_ACCESS_KEY",
    "SP_AWS_SECRET_KEY",
    "SP_ROLE_ARN",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
)

INVENTORY_REPORT_TYPE = os.environ.get(
    "INVENTORY_REPORT_TYPE", "GET_VENDOR_INVENTORY_REPORT"
)
INVENTORY_OPTIONS = {
    "reportPeriod": "DAY",
    "distributorView": "MANUFACTURING",
    "sellingProgram": "RETAIL",
}
INVENTORY_DATA_DELAY_DAYS = int(os.environ.get("INVENTORY_DATA_DELAY_DAYS", "1"))
INVENTORY_MAX_DAYS_BACK = int(os.environ.get("INVENTORY_MAX_DAYS_BACK", "7"))
INVENTORY_DISTRIBUTOR_VIEWS = tuple(
    value.strip().upper()
    for value in os.environ.get(
        "INVENTORY_DISTRIBUTOR_VIEWS", "MANUFACTURING,SOURCING"
    ).split(",")
    if value.strip()
)

INVENTORY_HEADERS = [
    "日期",
    "站点",
    "产品名",
    "ASIN",
    "SKU",
    "可售库存",
    "不可售库存",
    "采购中/在途库存",
    "总库存",
    "库存金额",
    "币种",
    "报告ID",
]


def require_env() -> dict[str, str]:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        raise SystemExit(
            "Missing required GitHub Secrets / environment variables: "
            + ", ".join(missing)
        )
    return {name: os.environ[name] for name in REQUIRED_ENV}


def _feishu_get(token: str, url: str) -> dict:
    return requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30).json()


def _feishu_post(token: str, url: str, body: dict) -> dict:
    return requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body,
        timeout=30,
    ).json()


def _sheets_put_values(
    token: str,
    spreadsheet_token: str,
    sheet_id: str,
    start_cell: str,
    values: list[list[Any]],
) -> None:
    if not values:
        return
    end_col = _column_letter(len(values[0]))
    end_row = _cell_row(start_cell) + len(values) - 1
    value_range = {
        "range": f"{sheet_id}!{start_cell}:{end_col}{end_row}",
        "values": values,
    }
    resp = requests.put(
        f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"valueRange": value_range},
        timeout=30,
    ).json()
    if resp.get("code") != 0:
        raise RuntimeError(f"Feishu Sheets values write failed: {resp}")


def _sheets_append_values(
    token: str,
    spreadsheet_token: str,
    sheet_id: str,
    values: list[list[Any]],
) -> None:
    if not values:
        return
    end_col = _column_letter(len(values[0]))
    end_row = len(values)
    resp = requests.post(
        f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values_append",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"valueRange": {"range": f"{sheet_id}!A1:{end_col}{end_row}", "values": values}},
        timeout=30,
    ).json()
    if resp.get("code") != 0:
        raise RuntimeError(f"Feishu Sheets values append failed: {resp}")


def _column_letter(column_count: int) -> str:
    result = ""
    n = column_count
    while n:
        n, remainder = divmod(n - 1, 26)
        result = chr(65 + remainder) + result
    return result or "A"


def _cell_row(cell: str) -> int:
    digits = "".join(ch for ch in cell if ch.isdigit())
    return int(digits or "1")


def _first_sheet_id(token: str, spreadsheet_token: str) -> str:
    resp = _feishu_get(
        token,
        f"https://open.feishu.cn/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query",
    )
    if resp.get("code") != 0:
        raise RuntimeError(f"Feishu Sheets metadata query failed: {resp}")
    sheets = resp.get("data", {}).get("sheets", [])
    if not sheets:
        raise RuntimeError(f"Feishu Sheets metadata has no sheets: {resp}")
    return sheets[0]["sheet_id"]


def inventory_spreadsheet_ensure(token: str) -> tuple[str, str]:
    spreadsheet_token = os.environ.get("INVENTORY_SPREADSHEET_TOKEN", "")
    sheet_id = os.environ.get("INVENTORY_SHEET_ID", "")
    if spreadsheet_token:
        return spreadsheet_token, sheet_id or _first_sheet_id(token, spreadsheet_token)
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        raise RuntimeError(
            "INVENTORY_SPREADSHEET_TOKEN is not set. Run this workflow manually once "
            "to create the inventory spreadsheet, then add the printed token value to "
            "GitHub Secrets before enabling daily writes."
        )

    resp = _feishu_post(
        token,
        "https://open.feishu.cn/open-apis/sheets/v3/spreadsheets",
        {"title": "Vendor Central 库存日报"},
    )
    if resp.get("code") != 0:
        raise RuntimeError(f"Create inventory spreadsheet failed: {resp}")
    spreadsheet = resp.get("data", {}).get("spreadsheet", {})
    spreadsheet_token = spreadsheet.get("spreadsheet_token")
    if not spreadsheet_token:
        raise RuntimeError(f"Create inventory spreadsheet returned no token: {resp}")
    sheet_id = _first_sheet_id(token, spreadsheet_token)
    _sheets_put_values(token, spreadsheet_token, sheet_id, "A1", [INVENTORY_HEADERS])
    logger.info("Inventory spreadsheet created: %s", spreadsheet.get("url", ""))
    logger.info(
        "Add this to GitHub Secrets to reuse the same spreadsheet on future runs:\n"
        "  INVENTORY_SPREADSHEET_TOKEN=%s",
        spreadsheet_token,
    )
    return spreadsheet_token, sheet_id


def _report_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in (
        "reportData",
        "inventoryByAsin",
        "inventory",
        "items",
        "data",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def log_inventory_payload_sample(payload: Any) -> None:
    if not os.environ.get("INVENTORY_DEBUG_RAW"):
        return
    if isinstance(payload, dict):
        logger.info("Inventory raw payload top-level keys: %s", sorted(payload.keys()))
    else:
        logger.info("Inventory raw payload type: %s", type(payload).__name__)
    records = _report_records(payload)
    logger.info("Inventory raw payload record count: %d", len(records))
    for index, record in enumerate(records[:5], start=1):
        logger.info(
            "Inventory raw record %d: %s",
            index,
            json.dumps(record, ensure_ascii=False, sort_keys=True),
        )


def _pick(item: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in item and item[name] not in (None, ""):
            return item[name]
    return None


def _number(item: dict[str, Any], *names: str) -> int:
    value = _pick(item, *names)
    if isinstance(value, dict):
        value = value.get("amount")
    try:
        return int(float(str(value).replace(",", "")))
    except Exception:
        return 0


def _money(item: dict[str, Any], *names: str) -> tuple[float, str]:
    value = _pick(item, *names)
    return money_amount(value), money_currency(value)


def normalize_inventory_report(payload: Any) -> list[dict[str, Any]]:
    rows = []
    for item in _report_records(payload):
        if not isinstance(item, dict):
            continue
        asin = str(_pick(item, "asin", "ASIN") or "").strip()
        sku = str(_pick(item, "vendorSku", "vendorSKU", "sku", "SKU") or "").strip()
        sellable = _number(
            item,
            "sellableOnHandUnits",
            "sellableInventoryUnits",
            "sellableUnits",
            "availableInventory",
            "availableUnits",
        )
        unsellable = _number(
            item,
            "unsellableOnHandUnits",
            "unsellableInventoryUnits",
            "unsellableUnits",
        )
        incoming = _number(
            item,
            "openPurchaseOrderUnits",
            "openPurchaseOrderQuantity",
            "inTransitQuantity",
            "inTransitUnits",
        )
        total = _number(item, "totalInventoryUnits", "totalUnits", "onHandUnits")
        if not total:
            total = sellable + unsellable + incoming
        value, currency = _money(
            item,
            "sellableOnHandInventory",
            "sellableInventory",
            "inventoryValue",
        )
        rows.append(
            {
                "asin": asin,
                "sku": sku,
                "sellable": sellable,
                "unsellable": unsellable,
                "incoming": incoming,
                "total": total,
                "inventory_value": round(value, 2),
                "currency": currency,
                "raw": json.dumps(item, ensure_ascii=False),
            }
        )
    return rows


def fetch_inventory_report(
    report_date: datetime,
    credentials: dict[str, str],
    marketplace: Any,
    distributor_view: str,
) -> FetchResult:
    start = report_date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    end = report_date.replace(hour=23, minute=59, second=59, microsecond=0, tzinfo=timezone.utc)
    report_options = dict(INVENTORY_OPTIONS)
    report_options["distributorView"] = distributor_view

    report_id = request_report(
        INVENTORY_REPORT_TYPE,
        start,
        end,
        credentials,
        marketplace,
        report_options,
    )
    if not report_id:
        return FetchResult(
            ok=False,
            message=f"Unable to create {INVENTORY_REPORT_TYPE} ({distributor_view}).",
        )

    doc_id, status = poll_report(report_id, credentials, marketplace)
    if not doc_id:
        return FetchResult(
            ok=False,
            message=(
                f"{INVENTORY_REPORT_TYPE} {report_date.strftime('%Y-%m-%d')} "
                f"{distributor_view} status={status}."
            ),
            report_id=report_id,
            status=status,
        )

    payload = download_report(doc_id, credentials, marketplace)
    if payload is None:
        return FetchResult(
            ok=False,
            message="Inventory report download/parse failed.",
            report_id=report_id,
            status=status,
        )

    log_inventory_payload_sample(payload)
    rows = normalize_inventory_report(payload)
    return FetchResult(ok=True, rows=rows, report_id=report_id, status=status)


def fetch_first_available_inventory_report(
    start_date: datetime,
    credentials: dict[str, str],
    marketplace: Any,
) -> tuple[FetchResult, datetime, str]:
    failures: list[str] = []
    for days_offset in range(INVENTORY_MAX_DAYS_BACK):
        report_date = start_date - timedelta(days=days_offset)
        for distributor_view in INVENTORY_DISTRIBUTOR_VIEWS:
            logger.info(
                "Trying inventory report: date=%s distributorView=%s",
                report_date.strftime("%Y-%m-%d"),
                distributor_view,
            )
            result = fetch_inventory_report(
                report_date,
                credentials,
                marketplace,
                distributor_view,
            )
            if result.ok:
                logger.info(
                    "Inventory report selected: date=%s distributorView=%s rows=%s",
                    report_date.strftime("%Y-%m-%d"),
                    distributor_view,
                    len(result.rows),
                )
                return result, report_date, distributor_view
            failures.append(result.message)
    return (
        FetchResult(
            ok=False,
            message="No available inventory report. Tried: " + " | ".join(failures),
        ),
        start_date,
        "",
    )


def write_inventory_to_sheet(
    inventory: FetchResult,
    report_date: datetime,
    marketplace_name: str,
    asin_names: dict[str, str],
    feishu_app_id: str,
    feishu_app_secret: str,
) -> bool:
    if not inventory.ok:
        raise RuntimeError(inventory.message)
    if not inventory.rows:
        logger.info("Inventory spreadsheet write skipped: no inventory data")
        return False

    token = feishu_token(feishu_app_id, feishu_app_secret)
    spreadsheet_token, sheet_id = inventory_spreadsheet_ensure(token)
    date_str = report_date.strftime("%Y-%m-%d")
    values = []
    for row in inventory.rows:
        asin = row.get("asin", "")
        values.append(
            [
                date_str,
                marketplace_name,
                asin_names.get(asin, asin),
                asin,
                row.get("sku", ""),
                row.get("sellable", 0),
                row.get("unsellable", 0),
                row.get("incoming", 0),
                row.get("total", 0),
                row.get("inventory_value", 0),
                row.get("currency", ""),
                inventory.report_id or "",
            ]
        )

    for i in range(0, len(values), 500):
        batch = values[i:i + 500]
        _sheets_append_values(token, spreadsheet_token, sheet_id, batch)
        logger.info("Inventory spreadsheet: wrote %d rows for %s", len(batch), date_str)
    return True


def main() -> None:
    env = require_env()
    marketplace, marketplace_name = marketplace_config()
    credentials = sp_credentials(env)
    report_date = datetime.now(CST) - timedelta(days=INVENTORY_DATA_DELAY_DAYS)
    xlsx_path = os.environ.get(
        "ASIN_NAMES_XLSX",
        os.path.join(os.path.dirname(__file__), "Xiaomi电视产品价格表2025-2026.xlsx"),
    )
    asin_names = load_asin_names(xlsx_path)

    logger.info(
        "Starting Vendor Central inventory report. marketplace=%s inventory_date=%s run=%s",
        marketplace_name,
        report_date.strftime("%Y-%m-%d"),
        github_run_label(),
    )
    inventory, selected_date, distributor_view = fetch_first_available_inventory_report(
        report_date,
        credentials,
        marketplace,
    )
    logger.info(
        "Inventory result: ok=%s rows=%s distributorView=%s message=%s",
        inventory.ok,
        len(inventory.rows),
        distributor_view,
        inventory.message,
    )
    if os.environ.get("INVENTORY_SKIP_WRITE"):
        logger.info("Inventory spreadsheet write skipped by INVENTORY_SKIP_WRITE.")
        return
    write_inventory_to_sheet(
        inventory,
        selected_date,
        marketplace_name,
        asin_names,
        env["FEISHU_APP_ID"],
        env["FEISHU_APP_SECRET"],
    )
    logger.info("Inventory done. No Feishu chat message sent.")


if __name__ == "__main__":
    main()
