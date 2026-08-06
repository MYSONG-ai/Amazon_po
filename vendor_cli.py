"""
Unified Amazon Vendor Central CLI.

This command-line entrypoint reuses the existing SP-API helpers in
vendor_report.py and inventory_report.py, but does not require Feishu settings.
Credentials are read from environment variables or a local .env file.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


SP_ENV = (
    "SP_LWA_APP_ID",
    "SP_LWA_CLIENT_SECRET",
    "SP_REFRESH_TOKEN",
)

CST = timezone(timedelta(hours=8))

MARKETPLACE_CONFIG = {
    "DE": {
        "marketplace_id": "A1PA6795UKMFR9",
        "endpoint": "https://sellingpartnerapi-eu.amazon.com",
    },
    "UK": {
        "marketplace_id": "A1F83G8C2ARO7P",
        "endpoint": "https://sellingpartnerapi-eu.amazon.com",
    },
    "FR": {
        "marketplace_id": "A13V1IB3VIYZZH",
        "endpoint": "https://sellingpartnerapi-eu.amazon.com",
    },
    "IT": {
        "marketplace_id": "APJ6JRA9NG5V4",
        "endpoint": "https://sellingpartnerapi-eu.amazon.com",
    },
    "ES": {
        "marketplace_id": "A1RKKUPIHCS9HS",
        "endpoint": "https://sellingpartnerapi-eu.amazon.com",
    },
    "US": {
        "marketplace_id": "ATVPDKIKX0DER",
        "endpoint": "https://sellingpartnerapi-na.amazon.com",
    },
}

REPORT_PRESETS = {
    "sales": {
        "type": "GET_VENDOR_SALES_REPORT",
        "options": {
            "reportPeriod": "DAY",
            "distributorView": "MANUFACTURING",
            "sellingProgram": "RETAIL",
        },
        "normalized": "sales",
    },
    "realtime-sales": {
        "type": "GET_VENDOR_REAL_TIME_SALES_REPORT",
        "options": {
            "distributorView": "MANUFACTURING",
            "sellingProgram": "RETAIL",
        },
        "normalized": "sales",
    },
    "inventory": {
        "type": "GET_VENDOR_INVENTORY_REPORT",
        "options": {
            "reportPeriod": "DAY",
            "distributorView": "MANUFACTURING",
            "sellingProgram": "RETAIL",
        },
        "normalized": "inventory",
    },
    "forecast": {
        "type": "GET_VENDOR_FORECASTING_REPORT",
        "options": {
            "reportPeriod": "WEEK",
            "distributorView": "MANUFACTURING",
            "sellingProgram": "RETAIL",
        },
    },
    "traffic": {
        "type": "GET_VENDOR_TRAFFIC_REPORT",
        "options": {
            "reportPeriod": "DAY",
            "distributorView": "MANUFACTURING",
            "sellingProgram": "RETAIL",
        },
    },
    "realtime-inventory": {
        "type": "GET_VENDOR_REAL_TIME_INVENTORY_REPORT",
        "options": {
            "distributorView": "MANUFACTURING",
            "sellingProgram": "RETAIL",
        },
    },
    "realtime-traffic": {
        "type": "GET_VENDOR_REAL_TIME_TRAFFIC_REPORT",
        "options": {
            "distributorView": "MANUFACTURING",
            "sellingProgram": "RETAIL",
        },
    },
    "margin": {
        "type": "GET_VENDOR_NET_PURE_PRODUCT_MARGIN_REPORT",
        "options": {
            "reportPeriod": "MONTH",
            "distributorView": "MANUFACTURING",
            "sellingProgram": "RETAIL",
        },
    },
}


@dataclass
class FetchResult:
    ok: bool
    rows: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""
    report_id: str | None = None
    status: str | None = None


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def require_sp_env() -> dict[str, str]:
    missing = [name for name in SP_ENV if not os.environ.get(name)]
    if missing:
        raise SystemExit("Missing SP-API environment variables: " + ", ".join(missing))
    return {name: os.environ[name] for name in SP_ENV}


def marketplace_config() -> dict[str, str]:
    marketplace_name = os.environ.get("SP_MARKETPLACE", "DE").upper()
    config = MARKETPLACE_CONFIG.get(marketplace_name)
    if config is None:
        supported = ", ".join(sorted(MARKETPLACE_CONFIG))
        raise SystemExit(f"Unsupported SP_MARKETPLACE={marketplace_name}. Supported: {supported}")
    return config


class HttpResponse:
    def __init__(self, status_code: int, content: bytes) -> None:
        self.status_code = status_code
        self.content = content
        self.text = content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)


class HttpClient:
    @staticmethod
    def request(
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: int = 60,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        data: dict[str, Any] | None = None,
    ) -> HttpResponse:
        if params:
            query = urllib.parse.urlencode(params)
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{query}"

        body: bytes | None = None
        request_headers = dict(headers or {})
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            request_headers.setdefault("content-type", "application/json")
        elif data is not None:
            body = urllib.parse.urlencode(data).encode("utf-8")
            request_headers.setdefault("content-type", "application/x-www-form-urlencoded")

        request = urllib.request.Request(
            url,
            data=body,
            headers=request_headers,
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(response.status, response.read())
        except urllib.error.HTTPError as exc:
            return HttpResponse(exc.code, exc.read())

    @classmethod
    def get(cls, url: str, timeout: int = 60) -> HttpResponse:
        return cls.request("GET", url, timeout=timeout)

    @classmethod
    def post(cls, url: str, data: dict[str, Any], timeout: int = 60) -> HttpResponse:
        return cls.request("POST", url, data=data, timeout=timeout)


class SpApiClient:
    def __init__(self, env: dict[str, str], config: dict[str, str]) -> None:
        self.env = env
        self.marketplace_id = config["marketplace_id"]
        self.endpoint = config["endpoint"].rstrip("/")
        self.http = HttpClient()
        self._access_token: str | None = None

    def access_token(self) -> str:
        if self._access_token:
            return self._access_token
        resp = self.http.post(
            "https://api.amazon.com/auth/o2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.env["SP_REFRESH_TOKEN"],
                "client_id": self.env["SP_LWA_APP_ID"],
                "client_secret": self.env["SP_LWA_CLIENT_SECRET"],
            },
            timeout=30,
        )
        self._raise_for_response(resp)
        self._access_token = resp.json()["access_token"]
        return self._access_token

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.endpoint}{path}"
        headers = kwargs.pop("headers", {})
        if "json" in kwargs:
            kwargs["json_body"] = kwargs.pop("json")
        headers.update(
            {
                "accept": "application/json",
                "content-type": "application/json",
                "user-agent": "amazon-vendor-cli/1.0",
                "x-amz-access-token": self.access_token(),
                "x-amz-date": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            }
        )
        resp = self.http.request(method, url, headers=headers, timeout=60, **kwargs)
        self._raise_for_response(resp)
        return resp.json() if resp.content else {}

    @staticmethod
    def _raise_for_response(resp: Any) -> None:
        if resp.status_code < 400:
            return
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise SystemExit(f"Amazon API request failed: HTTP {resp.status_code} {detail}")


def money_amount(value: Any) -> float:
    if isinstance(value, dict):
        value = value.get("amount", 0)
    try:
        return float(str(value).replace(",", "").replace("€", "").replace("£", "").strip())
    except Exception:
        return 0.0


def money_currency(value: Any) -> str:
    if isinstance(value, dict):
        return value.get("currencyCode", "")
    return ""


def parse_date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise SystemExit(f"Invalid date {value!r}; use YYYY-MM-DD.") from exc


def date_window(args: argparse.Namespace) -> tuple[datetime, datetime]:
    if args.date:
        start = parse_date(args.date)
        end = start.replace(hour=23, minute=59, second=59)
        return start, end
    if args.start or args.end:
        if not args.start or not args.end:
            raise SystemExit("Use --start and --end together, or use --date.")
        start = parse_date(args.start)
        end = parse_date(args.end).replace(hour=23, minute=59, second=59)
        if end < start:
            raise SystemExit("--end must be on or after --start.")
        return start, end
    start = (datetime.now(timezone.utc) - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end = start.replace(hour=23, minute=59, second=59)
    return start, end


def _records_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("reportData", "salesByAsin", "inventoryByAsin", "trafficByAsin", "items", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return [payload]


def normalize_sales_report(payload: Any) -> list[dict[str, Any]]:
    records = _records_from_payload(payload)
    by_asin: dict[str, dict[str, Any]] = {}
    for item in records:
        asin = str(item.get("asin") or item.get("ASIN") or "").strip()
        if not asin:
            continue
        row = by_asin.setdefault(
            asin,
            {"asin": asin, "ordered_units": 0, "ordered_revenue": 0.0, "currency": ""},
        )
        row["ordered_units"] += int(item.get("orderedUnits") or item.get("ordered_units") or 0)
        revenue = item.get("orderedRevenue") or item.get("ordered_revenue") or {}
        row["ordered_revenue"] += money_amount(revenue)
        if not row["currency"]:
            row["currency"] = money_currency(revenue)
    return sorted(by_asin.values(), key=lambda item: item["ordered_units"], reverse=True)


def _pick(item: dict[str, Any], *names: str) -> Any:
    for name in names:
        if item.get(name) not in (None, ""):
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


def normalize_inventory_report(payload: Any) -> list[dict[str, Any]]:
    rows = []
    for item in _records_from_payload(payload):
        asin = str(_pick(item, "asin", "ASIN") or "").strip()
        sku = str(_pick(item, "vendorSku", "vendorSKU", "sku", "SKU") or "").strip()
        sellable = _number(
            item,
            "sellableOnHandInventoryUnits",
            "sellableOnHandUnits",
            "sellableInventoryUnits",
            "sellableUnits",
            "availableInventory",
            "availableUnits",
        )
        unsellable = _number(
            item,
            "unsellableOnHandInventoryUnits",
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
        value = _pick(
            item,
            "sellableOnHandInventoryCost",
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
                "total": sellable + unsellable,
                "inventory_value": round(money_amount(value), 2),
                "currency": money_currency(value),
            }
        )
    return rows


def request_report(
    client: SpApiClient,
    report_type: str,
    start: datetime,
    end: datetime,
    report_options: dict[str, str],
) -> str:
    payload = client.request(
        "POST",
        "/reports/2021-06-30/reports",
        json={
            "reportType": report_type,
            "marketplaceIds": [client.marketplace_id],
            "dataStartTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "dataEndTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "reportOptions": report_options,
        },
    )
    report_id = payload.get("reportId")
    if not report_id:
        raise SystemExit(f"Create report returned no reportId: {payload}")
    return report_id


def poll_report(client: SpApiClient, report_id: str) -> tuple[str | None, str]:
    wait_seconds = int(os.environ.get("REPORT_WAIT_SECONDS", "60"))
    max_polls = int(os.environ.get("REPORT_MAX_POLLS", "20"))
    for _ in range(max_polls):
        time.sleep(wait_seconds)
        payload = client.request("GET", f"/reports/2021-06-30/reports/{report_id}")
        status = payload.get("processingStatus", "")
        if status == "DONE":
            return payload.get("reportDocumentId"), status
        if status in {"FATAL", "CANCELLED"}:
            return None, status
    return None, "TIMEOUT"


def download_report(client: SpApiClient, document_id: str) -> Any:
    document = client.request("GET", f"/reports/2021-06-30/documents/{document_id}")
    url = document.get("url")
    if not url:
        raise SystemExit(f"Report document returned no download URL: {document}")
    resp = client.http.get(url, timeout=90)
    SpApiClient._raise_for_response(resp)
    raw = resp.content
    if document.get("compressionAlgorithm") == "GZIP":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def rows_from_report_payload(payload: Any, kind: str, raw: bool, limit: int | None) -> list[dict[str, Any]]:
    preset = REPORT_PRESETS[kind]
    if not raw and preset.get("normalized") == "sales":
        return rows_for_output(normalize_sales_report(payload), limit)
    if not raw and preset.get("normalized") == "inventory":
        return rows_for_output(normalize_inventory_report(payload), limit)
    return rows_for_output(_records_from_payload(payload), limit)


def extract_po_summary(order: dict[str, Any]) -> dict[str, Any]:
    details = order.get("orderDetails", {}) or {}
    items = details.get("items", []) or []
    total_qty = sum(int((item.get("orderedQuantity") or {}).get("amount") or 0) for item in items)
    total_net = sum(money_amount(item.get("netCost")) for item in items)
    currency = money_currency(items[0].get("netCost")) if items else ""
    return {
        "po_number": order.get("purchaseOrderNumber", ""),
        "status": order.get("purchaseOrderState", ""),
        "po_date": str(details.get("purchaseOrderDate", ""))[:10],
        "ship_window": details.get("shipWindow", ""),
        "sku_count": len(items),
        "total_qty": total_qty,
        "total_net": round(total_net, 2),
        "currency": currency,
    }


def fetch_po_list(client: SpApiClient, days_back: int) -> FetchResult:
    days_back = max(1, min(days_back, 7))
    now = datetime.now(timezone.utc)
    params: dict[str, Any] = {
        "createdAfter": (now - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "createdBefore": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sortOrder": "DESC",
        "limit": 100,
    }
    rows = []
    while True:
        payload = client.request("GET", "/vendor/orders/v1/purchaseOrders", params=params)
        rows.extend(extract_po_summary(order) for order in payload.get("orders", []) or [])
        next_token = payload.get("pagination", {}).get("nextToken") or payload.get("nextToken")
        if not next_token:
            break
        params = {"nextToken": next_token}
    return FetchResult(ok=True, rows=rows)


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            child_key = f"{prefix}.{key}" if prefix else str(key)
            result.update(flatten(item, child_key))
        return result
    if isinstance(value, list):
        return {prefix: json.dumps(value, ensure_ascii=False)}
    return {prefix: value}


def rows_for_output(rows: Iterable[dict[str, Any]], limit: int | None = None) -> list[dict[str, Any]]:
    flat_rows = [flatten(row) for row in rows]
    if limit is not None:
        return flat_rows[:limit]
    return flat_rows


def print_table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("No rows.")
        return
    columns = list(dict.fromkeys(key for row in rows for key in row.keys()))
    widths = {
        col: min(max(len(str(col)), *(len(str(row.get(col, ""))) for row in rows)), 80)
        for col in columns
    }
    header = "  ".join(str(col)[: widths[col]].ljust(widths[col]) for col in columns)
    print(header)
    print("  ".join("-" * widths[col] for col in columns))
    for row in rows:
        print("  ".join(str(row.get(col, ""))[: widths[col]].ljust(widths[col]) for col in columns))


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = list(dict.fromkeys(key for row in rows for key in row.keys()))
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} rows to {path}")


def emit_rows(rows: list[dict[str, Any]], args: argparse.Namespace) -> None:
    if args.output:
        write_csv(rows, Path(args.output))
        return
    if args.format == "json":
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    print_table(rows)


def emit_result(result: Any, args: argparse.Namespace) -> None:
    if not result.ok:
        raise SystemExit(result.message or "SP-API request failed.")
    rows = rows_for_output(result.rows, args.limit)
    emit_rows(rows, args)


def command_po_list(args: argparse.Namespace) -> None:
    env = require_sp_env()
    client = SpApiClient(env, marketplace_config())
    emit_result(fetch_po_list(client, args.days), args)


def command_report(args: argparse.Namespace) -> None:
    env = require_sp_env()
    client = SpApiClient(env, marketplace_config())
    start, end = date_window(args)
    preset = REPORT_PRESETS[args.report_name]

    options = dict(preset["options"])
    if args.distributor_view:
        options["distributorView"] = args.distributor_view
    if args.report_period:
        options["reportPeriod"] = args.report_period
    if args.selling_program:
        options["sellingProgram"] = args.selling_program

    report_id = request_report(client, preset["type"], start, end, options)
    ready_seconds = int(os.environ.get("REPORT_READY_SECONDS", "60"))
    print(f"我问完亚马逊了，reportId={report_id}，大概 {ready_seconds} 秒有回复。")
    print(
        "稍后查询："
        f" python vendor_cli.py report-status {report_id} --kind {args.report_name}"
    )
    if not args.wait:
        return

    doc_id, status = poll_report(client, report_id)
    if not doc_id:
        raise SystemExit(f"Report {report_id} did not complete. status={status}")

    payload = download_report(client, doc_id)
    rows = rows_from_report_payload(payload, args.report_name, args.raw, args.limit)
    emit_rows(rows, args)


def command_report_status(args: argparse.Namespace) -> None:
    env = require_sp_env()
    client = SpApiClient(env, marketplace_config())
    status_payload = client.request("GET", f"/reports/2021-06-30/reports/{args.report_id}")
    status = status_payload.get("processingStatus", "")
    if status in {"FATAL", "CANCELLED"}:
        print(f"亚马逊报告处理失败：reportId={args.report_id}, status={status}")
        return
    if status != "DONE":
        print(f"亚马逊还没准备好：reportId={args.report_id}, status={status}")
        return
    document_id = status_payload.get("reportDocumentId")
    if not document_id:
        raise SystemExit(f"Report is DONE but has no reportDocumentId: {status_payload}")
    payload = download_report(client, document_id)
    rows = rows_from_report_payload(payload, args.kind, args.raw, args.limit)
    emit_rows(rows, args)


def command_capabilities(_: argparse.Namespace) -> None:
    rows = [
        {
            "command": "po list",
            "api": "Vendor Orders API v1",
            "role": "Amazon Fulfillment or Inventory and Order Tracking",
        },
        {
            "command": "report sales",
            "api": "Reports API / GET_VENDOR_SALES_REPORT",
            "role": "Brand Analytics",
        },
        {
            "command": "report inventory",
            "api": "Reports API / GET_VENDOR_INVENTORY_REPORT",
            "role": "Brand Analytics",
        },
        {
            "command": "report forecast",
            "api": "Reports API / GET_VENDOR_FORECASTING_REPORT",
            "role": "Brand Analytics",
        },
        {
            "command": "report traffic",
            "api": "Reports API / GET_VENDOR_TRAFFIC_REPORT",
            "role": "Brand Analytics",
        },
        {
            "command": "report margin",
            "api": "Reports API / GET_VENDOR_NET_PURE_PRODUCT_MARGIN_REPORT",
            "role": "Brand Analytics",
        },
    ]
    print_table(rows)


def command_check_env(_: argparse.Namespace) -> None:
    rows = []
    for name in SP_ENV:
        rows.append(
            {
                "variable": name,
                "status": "ok" if os.environ.get(name) else "missing",
                "purpose": {
                    "SP_LWA_APP_ID": "LWA client id",
                    "SP_LWA_CLIENT_SECRET": "LWA client secret",
                    "SP_REFRESH_TOKEN": "Vendor authorization refresh token",
                }[name],
            }
        )
    rows.append(
        {
            "variable": "SP_MARKETPLACE",
            "status": os.environ.get("SP_MARKETPLACE", "DE"),
            "purpose": "Marketplace selector, default DE",
        }
    )
    print_table(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Amazon Vendor Central CLI")
    parser.add_argument("--env-file", default=".env", help="Path to .env file. Default: .env")
    subparsers = parser.add_subparsers(dest="command", required=True)

    capabilities = subparsers.add_parser("capabilities", help="Show implemented first-batch commands")
    capabilities.set_defaults(func=command_capabilities)

    check_env = subparsers.add_parser("check-env", help="Check required SP-API environment variables")
    check_env.set_defaults(func=command_check_env)

    report_status = subparsers.add_parser("report-status", help="Check and download a requested report")
    report_status.add_argument("report_id", help="Report ID returned by a report command")
    report_status.add_argument(
        "--kind",
        choices=tuple(REPORT_PRESETS),
        required=True,
        help="Report preset used when the report was requested",
    )
    report_status.add_argument("--raw", action="store_true", help="Skip normalized helpers and output raw records")
    add_output_args(report_status)
    report_status.set_defaults(func=command_report_status)

    po = subparsers.add_parser("po", help="Vendor purchase order commands")
    po_sub = po.add_subparsers(dest="po_command", required=True)
    po_list = po_sub.add_parser("list", help="List recent purchase orders")
    po_list.add_argument("--days", type=int, default=7, help="Look back 1-7 days. Default: 7")
    add_output_args(po_list)
    po_list.set_defaults(func=command_po_list)

    report = subparsers.add_parser("report", help="Vendor report commands")
    report_sub = report.add_subparsers(dest="report_name", required=True)
    for name in REPORT_PRESETS:
        child = report_sub.add_parser(name, help=f"Fetch {REPORT_PRESETS[name]['type']}")
        add_date_args(child)
        add_report_option_args(child)
        add_output_args(child)
        child.add_argument("--wait", action="store_true", help="Poll until ready and print rows")
        child.set_defaults(func=command_report)

    return parser


def add_date_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--date", help="Single report date, YYYY-MM-DD")
    parser.add_argument("--start", help="Start date, YYYY-MM-DD")
    parser.add_argument("--end", help="End date, YYYY-MM-DD")


def add_report_option_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--raw", action="store_true", help="Skip normalized helpers and output raw records")
    parser.add_argument("--report-period", choices=("DAY", "WEEK", "MONTH", "QUARTER", "YEAR"))
    parser.add_argument("--distributor-view", default="MANUFACTURING")
    parser.add_argument("--selling-program", default="RETAIL")


def add_output_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=("table", "json"), default="table")
    parser.add_argument("--limit", type=int, default=50, help="Rows to print. Use 0 for none.")
    parser.add_argument("--output", help="Write all selected rows to CSV instead of printing")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    load_dotenv(Path(args.env_file))
    if getattr(args, "limit", None) == 0:
        args.limit = None
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
