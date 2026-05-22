"""
Amazon Vendor Central daily report.

Pulls Vendor Sales and Vendor Purchase Orders with SP-API, then sends a Feishu
direct message. Amazon permission/report availability failures are reported in
the message instead of being hidden as empty data.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from sp_api.api import Reports, VendorOrders
from sp_api.base import Marketplaces, SellingApiException

try:
    import openpyxl
    _OPENPYXL_AVAILABLE = True
except ImportError:
    _OPENPYXL_AVAILABLE = False

CST = timezone(timedelta(hours=8))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MARKETPLACE_MAP = {
    "DE": Marketplaces.DE,
    "UK": Marketplaces.UK,
    "FR": Marketplaces.FR,
    "IT": Marketplaces.IT,
    "ES": Marketplaces.ES,
}

REQUIRED_ENV = (
    "SP_LWA_APP_ID",
    "SP_LWA_CLIENT_SECRET",
    "SP_REFRESH_TOKEN",
    "SP_AWS_ACCESS_KEY",
    "SP_AWS_SECRET_KEY",
    "SP_ROLE_ARN",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "BITABLE_APP_TOKEN",
    "BITABLE_TABLE_ID",
)

REPORT_WAIT_SECONDS = int(os.environ.get("REPORT_WAIT_SECONDS", "60"))
REPORT_MAX_POLLS = int(os.environ.get("REPORT_MAX_POLLS", "20"))
SALES_DATA_DELAY_DAYS = int(os.environ.get("SALES_DATA_DELAY_DAYS", "1"))
PO_DAYS_BACK = int(os.environ.get("PO_DAYS_BACK", "7"))


@dataclass
class FetchResult:
    ok: bool
    rows: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""
    report_id: str | None = None
    status: str | None = None


def require_env() -> dict[str, str]:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        raise SystemExit(
            "Missing required GitHub Secrets / environment variables: "
            + ", ".join(missing)
        )
    return {name: os.environ[name] for name in REQUIRED_ENV}


def marketplace_config() -> tuple[Any, str]:
    marketplace_name = os.environ.get("SP_MARKETPLACE", "DE").upper()
    marketplace = MARKETPLACE_MAP.get(marketplace_name)
    if marketplace is None:
        supported = ", ".join(sorted(MARKETPLACE_MAP))
        raise SystemExit(f"Unsupported SP_MARKETPLACE={marketplace_name}. Supported: {supported}")
    return marketplace, marketplace_name


def sp_credentials(env: dict[str, str]) -> dict[str, str]:
    return {
        "lwa_app_id": env["SP_LWA_APP_ID"],
        "lwa_client_secret": env["SP_LWA_CLIENT_SECRET"],
        "refresh_token": env["SP_REFRESH_TOKEN"],
        "aws_access_key": env["SP_AWS_ACCESS_KEY"],
        "aws_secret_key": env["SP_AWS_SECRET_KEY"],
        "role_arn": env["SP_ROLE_ARN"],
    }


def github_run_label() -> str:
    run_id = os.environ.get("GITHUB_RUN_ID")
    repo = os.environ.get("GITHUB_REPOSITORY")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    if run_id and repo:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return "local/manual"


def load_asin_names(xlsx_path: str) -> dict[str, str]:
    if not _OPENPYXL_AVAILABLE:
        return {}
    try:
        import openpyxl
        wb = openpyxl.load_workbook(xlsx_path)
        ws = wb.active
        mapping = {}
        for row in ws.iter_rows(min_row=2, values_only=True):
            asin, name = row[1], row[3]
            if asin and name:
                mapping[str(asin).strip()] = str(name).strip()
        logger.info("Loaded %d ASIN names from %s", len(mapping), xlsx_path)
        return mapping
    except Exception as exc:
        logger.warning("Could not load ASIN names from %s: %s", xlsx_path, exc)
        return {}


# ===================== Feishu =====================
_BITABLE_FIELDS = [
    ("日期",        1),  # text
    ("产品名",      1),
    ("ASIN",        1),
    ("已订购量",    2),  # number
    ("ASP(EUR)",    2),
    ("订购金额(EUR)", 2),
    ("退货/取消量", 2),
]


def feishu_token(app_id: str, app_secret: str) -> str:
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=30,
    )
    payload = resp.json()
    if payload.get("code") != 0:
        raise RuntimeError(f"Feishu token request failed: {payload}")
    return payload["tenant_access_token"]


def send_feishu_post(app_id: str, app_secret: str, user_id: str, title: str, content: list[list]):
    token = feishu_token(app_id, app_secret)
    resp = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "receive_id": user_id,
            "msg_type": "post",
            "content": json.dumps(
                {"zh_cn": {"title": title, "content": content}}, ensure_ascii=False
            ),
        },
        timeout=30,
    )
    payload = resp.json()
    if payload.get("code") != 0:
        raise RuntimeError(
            "Feishu message send failed. "
            f"receive_id_type=chat_id, receive_id={user_id}, response={payload}"
        )


# ===================== Feishu Bitable =====================
def _bitable_get(token: str, url: str) -> dict:
    return requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30).json()


def _bitable_post(token: str, url: str, body: dict) -> dict:
    return requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body,
        timeout=30,
    ).json()


def bitable_ensure(token: str) -> tuple[str, str]:
    """Return (app_token, table_id). Creates a new Bitable on first run."""
    app_token = os.environ.get("BITABLE_APP_TOKEN", "")
    table_id = os.environ.get("BITABLE_TABLE_ID", "")
    if app_token and table_id:
        return app_token, table_id

    # Create bitable
    resp = _bitable_post(token, "https://open.feishu.cn/open-apis/bitable/v1/apps",
                         {"name": "Vendor Central 销量日报"})
    if resp.get("code") != 0:
        raise RuntimeError(f"Create bitable failed: {resp}")
    app_token = resp["data"]["app"]["app_token"]

    # Get default table_id
    resp2 = _bitable_get(token, f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables")
    table_id = resp2["data"]["items"][0]["table_id"]

    # Add custom fields (skip any that already exist)
    existing_resp = _bitable_get(
        token,
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
    )
    existing = {f["field_name"] for f in existing_resp.get("data", {}).get("items", [])}
    for fname, ftype in _BITABLE_FIELDS:
        if fname not in existing:
            _bitable_post(
                token,
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
                {"field_name": fname, "type": ftype},
            )

    logger.info("Bitable created: https://www.feishu.cn/base/%s", app_token)
    logger.info(
        "Add to .env and GitHub Secrets:\n  BITABLE_APP_TOKEN=%s\n  BITABLE_TABLE_ID=%s",
        app_token, table_id,
    )
    return app_token, table_id


def write_to_bitable(
    sales: FetchResult,
    report_date: datetime,
    asin_names: dict[str, str],
    feishu_app_id: str,
    feishu_app_secret: str,
) -> None:
    if not sales.ok or not sales.rows:
        logger.info("Bitable write skipped: no sales data")
        return

    token = feishu_token(feishu_app_id, feishu_app_secret)
    app_token, table_id = bitable_ensure(token)
    date_str = report_date.strftime("%Y-%m-%d")
    # Bitable DateTime fields require Unix timestamp in ms (UTC midnight)
    date_ts_ms = int(
        report_date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc).timestamp() * 1000
    )

    records = []
    for row in sales.rows:
        asin = row.get("asin", "")
        units = int(row.get("ordered_units") or 0)
        rev = float(row.get("ordered_revenue") or 0)
        if units > 0:
            ordered, cancelled, asp = units, 0, round(rev / units, 2)
        elif units < 0:
            ordered, cancelled, asp = 0, abs(units), 0.0
        else:
            continue
        records.append({
            "fields": {
                "日期":         date_ts_ms,
                "产品名":       asin_names.get(asin, asin),
                "ASIN":         asin,
                "已订购量":     ordered,
                "ASP(EUR)":     asp,
                "订购金额(EUR)": round(rev if units > 0 else 0, 2),
                "退货/取消量":  cancelled,
            }
        })

    url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}"
           f"/tables/{table_id}/records/batch_create")
    for i in range(0, len(records), 500):
        batch = records[i:i + 500]
        resp = _bitable_post(token, url, {"records": batch})
        if resp.get("code") != 0:
            logger.error("Bitable write failed: %s", resp)
        else:
            logger.info("Bitable: wrote %d records for %s", len(batch), date_str)


# ===================== SP-API reports =====================
def request_report(
    report_type: str,
    start: datetime,
    end: datetime,
    credentials: dict[str, str],
    marketplace: Any,
    report_options: dict[str, str] | None = None,
) -> str | None:
    api = Reports(credentials=credentials, marketplace=marketplace)
    if report_options is None:
        report_options = {
            "reportPeriod": "DAY",
            "distributorView": "MANUFACTURING",
            "sellingProgram": "RETAIL",
        }
    try:
        resp = api.create_report(
            reportType=report_type,
            reportOptions=report_options,
            dataStartTime=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            dataEndTime=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            marketplaceIds=[marketplace.marketplace_id],
        )
        report_id = resp.payload["reportId"]
        logger.info("Requested report [%s], reportId=%s", report_type, report_id)
        return report_id
    except SellingApiException as exc:
        logger.error("Create report failed [%s]: %s", report_type, exc)
        return None


def poll_report(report_id: str, credentials: dict[str, str], marketplace: Any) -> tuple[str | None, str]:
    api = Reports(credentials=credentials, marketplace=marketplace)
    for attempt in range(1, REPORT_MAX_POLLS + 1):
        time.sleep(REPORT_WAIT_SECONDS)
        try:
            resp = api.get_report(report_id)
            status = resp.payload["processingStatus"]
            logger.info("Report [%s] poll %s: %s", report_id, attempt, status)
            if status == "DONE":
                return resp.payload.get("reportDocumentId"), status
            if status in ("FATAL", "CANCELLED"):
                return None, status
        except Exception as exc:
            logger.warning("Polling report failed: %s", exc)
    return None, "TIMEOUT"


def download_report(doc_id: str, credentials: dict[str, str], marketplace: Any) -> dict[str, Any] | None:
    api = Reports(credentials=credentials, marketplace=marketplace)
    try:
        resp = api.get_report_document(doc_id)
        url = resp.payload["url"]
        compression = resp.payload.get("compressionAlgorithm")
        raw = requests.get(url, timeout=60).content
        if compression == "GZIP":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        logger.error("Download or parse report failed: %s", exc)
        return None


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


def normalize_sales_report(payload: Any) -> list[dict[str, Any]]:
    # Real-time report: top-level list or {"reportData": [...]} with hourly records per ASIN.
    # Aggregate hours into daily totals keyed by ASIN.
    if isinstance(payload, list):
        records = payload
    else:
        records = (
            payload.get("reportData")
            or payload.get("salesByAsin")
            or payload.get("sales")
            or []
        )

    by_asin: dict[str, dict[str, Any]] = {}
    for item in records:
        asin = item.get("asin", "") or ""
        if asin not in by_asin:
            by_asin[asin] = {"asin": asin, "ordered_units": 0, "ordered_revenue": 0.0, "currency": ""}
        by_asin[asin]["ordered_units"] += int(item.get("orderedUnits") or 0)
        rev = item.get("orderedRevenue") or {}
        by_asin[asin]["ordered_revenue"] += money_amount(rev)
        if not by_asin[asin]["currency"]:
            by_asin[asin]["currency"] = money_currency(rev)

    return sorted(by_asin.values(), key=lambda r: r["ordered_units"], reverse=True)


REPORT_TYPE = "GET_VENDOR_REAL_TIME_SALES_REPORT"
REPORT_OPTIONS = {
    "distributorView": "MANUFACTURING",
    "sellingProgram": "RETAIL",
}


def fetch_sales_report(
    report_date: datetime,
    credentials: dict[str, str],
    marketplace: Any,
) -> FetchResult:
    start = report_date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    end = report_date.replace(hour=23, minute=59, second=59, microsecond=0, tzinfo=timezone.utc)

    report_id = request_report(REPORT_TYPE, start, end, credentials, marketplace, REPORT_OPTIONS)
    if not report_id:
        return FetchResult(
            ok=False,
            message=(
                f"未能创建 {REPORT_TYPE}。请确认 Amazon SP-API 已授权 "
                "Brand Analytics 角色，并且 LWA/AWS/Role ARN 凭据正确。"
            ),
        )

    doc_id, status = poll_report(report_id, credentials, marketplace)
    if not doc_id:
        if status == "FATAL":
            message = (
                f"{REPORT_TYPE} 返回 FATAL。常见原因：Brand Analytics 权限不足，"
                "或该日期数据尚未生成（实时报告通常当天可用）。"
            )
        else:
            message = f"{REPORT_TYPE} 未生成完成，状态：{status}。"
        return FetchResult(ok=False, message=message, report_id=report_id, status=status)

    payload = download_report(doc_id, credentials, marketplace)
    if payload is None:
        return FetchResult(
            ok=False,
            message="销量报告已生成但下载/解析失败，请查看 Actions 日志。",
            report_id=report_id,
            status=status,
        )

    rows = normalize_sales_report(payload)
    return FetchResult(ok=True, rows=rows, report_id=report_id, status=status)


# ===================== SP-API Vendor Orders =====================
def extract_po_summary(order: dict[str, Any]) -> dict[str, Any]:
    details = order.get("orderDetails", {}) or {}
    items = details.get("items", []) or []
    total_qty = sum(
        int((item.get("orderedQuantity") or {}).get("amount") or 0) for item in items
    )
    total_net = sum(money_amount(item.get("netCost")) for item in items)
    currency = money_currency(items[0].get("netCost")) if items else ""
    return {
        "po_number": order.get("purchaseOrderNumber", ""),
        "status": order.get("purchaseOrderState", ""),
        "po_date": str(details.get("purchaseOrderDate", ""))[:10],
        "ship_window": details.get("shipWindow", ""),
        "sku_count": len(items),
        "total_qty": total_qty,
        "total_net": total_net,
        "currency": currency,
    }


def fetch_po_list(days_back: int, credentials: dict[str, str], marketplace: Any) -> FetchResult:
    api = VendorOrders(credentials=credentials, marketplace=marketplace)
    days_back = max(1, min(days_back, 7))
    now = datetime.now(timezone.utc)
    created_after = (now - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%SZ")
    created_before = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = []
    next_token = None
    while True:
        try:
            kwargs = {
                "createdAfter": created_after,
                "createdBefore": created_before,
                "sortOrder": "DESC",
                "limit": 100,
            }
            if next_token:
                kwargs = {"nextToken": next_token}
            resp = api.get_purchase_orders(**kwargs)
        except SellingApiException as exc:
            logger.error("Fetch PO failed: %s", exc)
            return FetchResult(
                ok=False,
                message=(
                    "获取 PO 失败。Amazon 返回 403/Unauthorized 时，请确认应用已授权 "
                    "Vendor Orders / Retail Procurement Orders 权限，且授权的 vendor group "
                    "包含 DE 站点和对应 ordering vendor code。"
                ),
            )

        for order in resp.payload.get("orders", []) or []:
            rows.append(extract_po_summary(order))

        next_token = getattr(resp, "next_token", None)
        if not next_token:
            break

    return FetchResult(
        ok=True,
        rows=rows,
        message=f"PO window: {created_after} to {created_before}",
    )


# ===================== Report rendering =====================
def build_sales_section(result: FetchResult, asin_names: dict[str, str] | None = None) -> list[list]:
    if not result.ok:
        return [[{"tag": "text", "text": f"  ⚠️ {result.message}"}]]
    if not result.rows:
        return [[{"tag": "text", "text": "  暂无已订购数据（权限正常，但该日期没有 ASIN 数据）。"}]]

    asin_names = asin_names or {}
    total_ordered = sum(int(row.get("ordered_units") or 0) for row in result.rows)
    total_revenue = sum(float(row.get("ordered_revenue") or 0) for row in result.rows)
    currency = next((row.get("currency") for row in result.rows if row.get("currency")), "")
    avg_asp = total_revenue / total_ordered if total_ordered else 0.0

    # Sort by Excel sheet order; ASINs not in the sheet go to the end
    asin_order = {asin: i for i, asin in enumerate(asin_names)}
    rows_sorted = sorted(result.rows, key=lambda r: asin_order.get(r.get("asin", ""), 9999))

    positive_rows = [r for r in rows_sorted if int(r.get("ordered_units") or 0) > 0]
    cancelled_rows = [r for r in rows_sorted if int(r.get("ordered_units") or 0) < 0]
    total_cancelled = sum(abs(int(r.get("ordered_units") or 0)) for r in cancelled_rows)

    cancelled_str = f"  |  退货/取消：{total_cancelled} 件" if total_cancelled else ""
    lines = [
        [
            {
                "tag": "text",
                "text": f"  ✅销量：{total_ordered} 件{cancelled_str}  |  ASP：{currency}{avg_asp:.2f}",
            }
        ]
    ]
    for i, row in enumerate(positive_rows[:30], start=1):
        asin = row.get("asin", "")
        label = asin_names.get(asin, asin)
        units = int(row.get("ordered_units") or 0)
        rev = float(row.get("ordered_revenue") or 0)
        cur = row.get("currency", "")
        asp = rev / units if units else 0.0
        lines.append(
            [{"tag": "text", "text": f" {i:>2}. {label}  * {units} | ASP : {cur}{asp:.2f}"}]
        )
    for row in cancelled_rows:
        asin = row.get("asin", "")
        label = asin_names.get(asin, asin)
        units = abs(int(row.get("ordered_units") or 0))
        lines.append(
            [{"tag": "text", "text": f"  ↩ {label}  退货/取消 {units} 件"}]
        )
    return lines


def build_po_section(result: FetchResult) -> list[list]:
    if not result.ok:
        return [[{"tag": "text", "text": f"  ⚠️ {result.message}"}]]
    if not result.rows:
        return [[{"tag": "text", "text": "  暂无 PO 数据。"}]]

    status_icon = {"New": "🆕", "Acknowledged": "✅", "Closed": "🔒", "NEW": "🆕"}
    by_status: dict[str, list[dict[str, Any]]] = {}
    for po in result.rows:
        by_status.setdefault(po.get("status") or "UNKNOWN", []).append(po)

    lines = []
    for status, group in by_status.items():
        icon = status_icon.get(status, "📋")
        total_qty = sum(int(po.get("total_qty") or 0) for po in group)
        lines.append(
            [{"tag": "text", "text": f"  {icon} {status}  共 {len(group)} 个 PO  合计 {total_qty} 件"}]
        )

    lines.append([{"tag": "text", "text": ""}])
    for po in result.rows[:10]:
        icon = status_icon.get(po.get("status"), "📋")
        net = f"  净额:{po.get('currency', '')}{float(po.get('total_net') or 0):.2f}" if po.get("total_net") else ""
        ship = f"  发货窗口:{po.get('ship_window')}" if po.get("ship_window") else ""
        lines.append(
            [
                {
                    "tag": "text",
                    "text": (
                        f"  {icon} {po.get('po_number')}  {po.get('po_date')}  "
                        f"{po.get('sku_count', 0)}个SKU  {po.get('total_qty', 0)}件{net}{ship}"
                    ),
                }
            ]
        )
    return lines


def build_daily_report(
    sales: FetchResult,
    pos: FetchResult,
    report_date: datetime,
    marketplace_name: str,
    asin_names: dict[str, str] | None = None,
) -> tuple[str, list[list]]:
    now_str = datetime.now(CST).strftime("%Y/%m/%d %H:%M")
    date_str = report_date.strftime("%Y-%m-%d")
    title = f"📊 Vendor Central 日报 [{marketplace_name}] {date_str}"

    sales_status = "✅ 正常" if sales.ok else "⚠️ 需处理"
    po_status = "✅ 正常" if pos.ok else "⚠️ 需处理"
    content: list[list] = [
        [{"tag": "text", "text": f"运行：{github_run_label()}"}],
        [{"tag": "text", "text": f"站点：{marketplace_name}  销量日期：{date_str}  PO范围：近 {min(PO_DAYS_BACK, 7)} 天"}],
        [{"tag": "text", "text": f"状态：销量 {sales_status} / PO {po_status}"}],
    ]
    if sales.report_id:
        content.append([{"tag": "text", "text": f"Amazon sales reportId：{sales.report_id}  状态：{sales.status or '-'}"}])
    content.append([{"tag": "text", "text": ""}])
    content.append([{"tag": "text", "text": f"──（实时销量报告 {date_str}）──"}])
    content.extend(build_sales_section(sales, asin_names))
    content.append([{"tag": "text", "text": ""}])
    content.append([{"tag": "text", "text": "── 采购订单（PO）──"}])
    content.extend(build_po_section(pos))
    content.append([{"tag": "text", "text": ""}])
    content.append([{"tag": "text", "text": f"更新时间：{now_str}"}])
    return title, content


def main():
    env = require_env()
    marketplace, marketplace_name = marketplace_config()
    credentials = sp_credentials(env)
    report_date = datetime.now(CST) - timedelta(days=SALES_DATA_DELAY_DAYS)
    logger.info(
        "Starting Vendor Central report. marketplace=%s sales_date=%s po_days=%s run=%s",
        marketplace_name,
        report_date.strftime("%Y-%m-%d"),
        min(PO_DAYS_BACK, 7),
        github_run_label(),
    )

    xlsx_path = os.environ.get("ASIN_NAMES_XLSX", os.path.join(os.path.dirname(__file__), "Xiaomi电视产品价格表2025-2026.xlsx"))
    asin_names = load_asin_names(xlsx_path)

    sales = fetch_sales_report(report_date, credentials, marketplace)
    logger.info("Sales result: ok=%s rows=%s message=%s", sales.ok, len(sales.rows), sales.message)

    write_to_bitable(sales, report_date, asin_names, env["FEISHU_APP_ID"], env["FEISHU_APP_SECRET"])

    pos = fetch_po_list(PO_DAYS_BACK, credentials, marketplace)
    logger.info("PO result: ok=%s rows=%s message=%s", pos.ok, len(pos.rows), pos.message)

    logger.info("Done.")


if __name__ == "__main__":
    main()
