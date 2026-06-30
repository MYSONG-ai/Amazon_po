"""
Amazon Vendor Central daily inventory report.

Pulls the Vendor Inventory report with SP-API and writes rows to a dedicated
Feishu Bitable. This job intentionally does not send Feishu chat messages.
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

INVENTORY_BITABLE_FIELDS = [
    ("日期", 1),
    ("站点", 1),
    ("产品名", 1),
    ("ASIN", 1),
    ("SKU", 1),
    ("可售库存", 2),
    ("不可售库存", 2),
    ("采购中/在途库存", 2),
    ("总库存", 2),
    ("库存金额", 2),
    ("币种", 1),
    ("报告ID", 1),
]


def require_env() -> dict[str, str]:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        raise SystemExit(
            "Missing required GitHub Secrets / environment variables: "
            + ", ".join(missing)
        )
    return {name: os.environ[name] for name in REQUIRED_ENV}


def _bitable_get(token: str, url: str) -> dict:
    return requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30).json()


def _bitable_post(token: str, url: str, body: dict) -> dict:
    return requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body,
        timeout=30,
    ).json()


def inventory_bitable_ensure(token: str) -> tuple[str, str]:
    app_token = os.environ.get("INVENTORY_BITABLE_APP_TOKEN", "")
    table_id = os.environ.get("INVENTORY_BITABLE_TABLE_ID", "")
    if app_token and table_id:
        return app_token, table_id
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        raise RuntimeError(
            "INVENTORY_BITABLE_APP_TOKEN and INVENTORY_BITABLE_TABLE_ID are not set. "
            "Run this workflow manually once to create the inventory Bitable, then add "
            "the printed token values to GitHub Secrets before enabling daily writes."
        )

    resp = _bitable_post(
        token,
        "https://open.feishu.cn/open-apis/bitable/v1/apps",
        {"name": "Vendor Central 库存日报"},
    )
    if resp.get("code") != 0:
        raise RuntimeError(f"Create inventory bitable failed: {resp}")
    app_token = resp["data"]["app"]["app_token"]

    resp2 = _bitable_get(
        token,
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables",
    )
    table_id = resp2["data"]["items"][0]["table_id"]

    existing_resp = _bitable_get(
        token,
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
    )
    existing = {f["field_name"] for f in existing_resp.get("data", {}).get("items", [])}
    for field_name, field_type in INVENTORY_BITABLE_FIELDS:
        if field_name in existing:
            continue
        _bitable_post(
            token,
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
            {"field_name": field_name, "type": field_type},
        )

    logger.info("Inventory Bitable created: https://www.feishu.cn/base/%s", app_token)
    logger.info(
        "Add these to GitHub Secrets to reuse the same table on future runs:\n"
        "  INVENTORY_BITABLE_APP_TOKEN=%s\n"
        "  INVENTORY_BITABLE_TABLE_ID=%s",
        app_token,
        table_id,
    )
    return app_token, table_id


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
) -> FetchResult:
    start = report_date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    end = report_date.replace(hour=23, minute=59, second=59, microsecond=0, tzinfo=timezone.utc)

    report_id = request_report(
        INVENTORY_REPORT_TYPE,
        start,
        end,
        credentials,
        marketplace,
        INVENTORY_OPTIONS,
    )
    if not report_id:
        return FetchResult(ok=False, message=f"Unable to create {INVENTORY_REPORT_TYPE}.")

    doc_id, status = poll_report(report_id, credentials, marketplace)
    if not doc_id:
        return FetchResult(
            ok=False,
            message=f"{INVENTORY_REPORT_TYPE} not ready, status={status}.",
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

    rows = normalize_inventory_report(payload)
    return FetchResult(ok=True, rows=rows, report_id=report_id, status=status)


def write_inventory_to_bitable(
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
        logger.info("Inventory Bitable write skipped: no inventory data")
        return False

    token = feishu_token(feishu_app_id, feishu_app_secret)
    app_token, table_id = inventory_bitable_ensure(token)
    date_str = report_date.strftime("%Y-%m-%d")
    records = []
    for row in inventory.rows:
        asin = row.get("asin", "")
        records.append(
            {
                "fields": {
                    "日期": date_str,
                    "站点": marketplace_name,
                    "产品名": asin_names.get(asin, asin),
                    "ASIN": asin,
                    "SKU": row.get("sku", ""),
                    "可售库存": row.get("sellable", 0),
                    "不可售库存": row.get("unsellable", 0),
                    "采购中/在途库存": row.get("incoming", 0),
                    "总库存": row.get("total", 0),
                    "库存金额": row.get("inventory_value", 0),
                    "币种": row.get("currency", ""),
                    "报告ID": inventory.report_id or "",
                }
            }
        )

    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}"
        f"/tables/{table_id}/records/batch_create"
    )
    success = False
    for i in range(0, len(records), 500):
        batch = records[i:i + 500]
        resp = _bitable_post(token, url, {"records": batch})
        if resp.get("code") != 0:
            raise RuntimeError(f"Inventory Bitable write failed: {resp}")
        logger.info("Inventory Bitable: wrote %d records for %s", len(batch), date_str)
        success = True
    return success


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
    inventory = fetch_inventory_report(report_date, credentials, marketplace)
    logger.info(
        "Inventory result: ok=%s rows=%s message=%s",
        inventory.ok,
        len(inventory.rows),
        inventory.message,
    )
    write_inventory_to_bitable(
        inventory,
        report_date,
        marketplace_name,
        asin_names,
        env["FEISHU_APP_ID"],
        env["FEISHU_APP_SECRET"],
    )
    logger.info("Inventory done. No Feishu chat message sent.")


if __name__ == "__main__":
    main()
