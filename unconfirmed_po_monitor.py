"""
Monitor for all unconfirmed Amazon Vendor Central Italy POs.

The job checks one Italy VC account and sends a Feishu group message only when
unconfirmed purchase orders are found.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests

from vendor_report import (
    CST,
    load_asin_names,
    money_amount,
    money_currency,
    send_feishu_text,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CHAT_ID = os.environ.get(
    "UNCONFIRMED_PO_CHAT_ID", "oc_c95fca0100bedfff9cabf3c37e45d4cb"
)
ACCOUNT_SLOTS = tuple(
    slot.strip().upper()
    for slot in os.environ.get("UNCONFIRMED_PO_ACCOUNT_SLOTS", "IT").split(",")
    if slot.strip()
)
UNCONFIRMED_STATES = {
    state.strip().upper()
    for state in os.environ.get(
        "UNCONFIRMED_PO_STATES", "New,NEW,Unconfirmed,UNCONFIRMED"
    ).split(",")
    if state.strip()
}
EUR_TO_RMB_RATE = float(os.environ.get("EUR_TO_RMB_RATE", "7.8"))

COMMON_SP_ENV = (
    "SP_LWA_APP_ID",
    "SP_LWA_CLIENT_SECRET",
)

MARKETPLACE_IDS = {
    "DE": "A1PA6795UKAE89",
    "IT": "APJ6JRA9NG5V4",
    "FR": "A13V1IB3VIYZZH",
    "ES": "A1RKKUPIHCS9HS",
    "UK": "A1F83G8C2ARO7P",
}
SP_API_BASE = "https://sellingpartnerapi-eu.amazon.com"
LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"


@dataclass
class AccountConfig:
    slot: str
    label: str
    marketplace_code: str
    credentials: dict[str, str]


def require_feishu_env() -> tuple[str, str]:
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    missing = [
        name
        for name, value in (
            ("FEISHU_APP_ID", app_id),
            ("FEISHU_APP_SECRET", app_secret),
        )
        if not value
    ]
    if missing:
        raise SystemExit("Missing Feishu environment variables: " + ", ".join(missing))
    return app_id, app_secret


def _env_value(slot: str, name: str) -> str:
    return os.environ.get(f"{slot}_{name}", "") or os.environ.get(name, "")


def load_account(slot: str) -> AccountConfig | None:
    refresh_token = os.environ.get(f"{slot}_SP_REFRESH_TOKEN", "")
    if not refresh_token:
        logger.warning("%s skipped: missing %s_SP_REFRESH_TOKEN", slot, slot)
        return None

    missing = [name for name in COMMON_SP_ENV if not _env_value(slot, name)]
    if missing:
        logger.warning(
            "%s skipped: missing %s",
            slot,
            ", ".join(f"{slot}_{name} or {name}" for name in missing),
        )
        return None

    marketplace_code = os.environ.get(f"{slot}_SP_MARKETPLACE", "IT").upper()
    if marketplace_code not in MARKETPLACE_IDS:
        logger.warning("%s skipped: unsupported marketplace %s", slot, marketplace_code)
        return None

    return AccountConfig(
        slot=slot,
        label=os.environ.get(f"{slot}_ACCOUNT_LABEL", slot),
        marketplace_code=marketplace_code,
        credentials={
            "lwa_app_id": _env_value(slot, "SP_LWA_APP_ID"),
            "lwa_client_secret": _env_value(slot, "SP_LWA_CLIENT_SECRET"),
            "refresh_token": refresh_token,
        },
    )


def _po_amount(item: dict[str, Any]) -> float:
    quantity = _quantity_amount(item)
    return quantity * money_amount(item.get("netCost"))


def _quantity_amount(item: dict[str, Any]) -> int:
    value = item.get("orderedQuantity") or {}
    if isinstance(value, dict) and isinstance(value.get("orderedQuantity"), dict):
        value = value.get("orderedQuantity") or {}
    if isinstance(value, dict):
        value = value.get("amount")
    try:
        return int(float(str(value).replace(",", "")))
    except Exception:
        return 0


def _format_number(value: float) -> str:
    text = f"{value:,.2f}".rstrip("0").rstrip(".")
    return text


def _format_money(currency: str, value: float) -> str:
    return f"{currency or 'EUR'} {_format_number(value)}"


def _format_rmb(value: float) -> str:
    return f"RMB {_format_number(value)}"


def _format_w(value: float) -> str:
    text = f"{value / 10000:.1f}".rstrip("0").rstrip(".")
    return f"{text}W"


def _format_rmb_w(value: float) -> str:
    return f"RMB {_format_w(value)}"


def _parse_date(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d")
        except Exception:
            return None


def _ship_window_start(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, dict):
        start = value.get("start") or value.get("startDate") or value.get("startDateTime") or ""
        return _parse_date(str(start)) if start else None
    text = str(value)
    start = text.split("--", 1)[0] if "--" in text else text
    return _parse_date(start)


def _starts_current_month(value: Any, now: datetime) -> bool:
    start = _ship_window_start(value)
    return bool(start and start.year == now.year and start.month == now.month)


def _month_day(value: str) -> str:
    parsed = _parse_date(value)
    if parsed:
        return f"{parsed.month}/{parsed.day}"
    return value[:10] if value else ""


def _format_ship_window(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, dict):
        start = value.get("start") or value.get("startDate") or value.get("startDateTime") or ""
        end = value.get("end") or value.get("endDate") or value.get("endDateTime") or ""
        if start and end:
            return f"SW {_month_day(str(start))} -{_month_day(str(end))}"
        if start:
            return f"SW {_month_day(str(start))}"
    text = str(value)
    if "--" in text:
        start, end = text.split("--", 1)
        return f"SW {_month_day(start)} -{_month_day(end)}"
    return f"SW {text}"


def _ship_window(order: dict[str, Any], details: dict[str, Any]) -> Any:
    return (
        details.get("shipWindow")
        or details.get("deliveryWindow")
        or order.get("shipWindow")
        or order.get("deliveryWindow")
    )


def _item_product_id(item: dict[str, Any]) -> str:
    return str(
        item.get("amazonProductIdentifier")
        or item.get("buyerProductIdentifier")
        or item.get("asin")
        or item.get("ASIN")
        or ""
    ).strip()


def _summarize_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for item in items:
        product_id = _item_product_id(item)
        rows.append(
            {
                "product_id": product_id,
                "vendor_product_id": str(item.get("vendorProductIdentifier") or "").strip(),
                "fallback_label": str(
                    item.get("productName")
                    or item.get("title")
                    or item.get("vendorProductIdentifier")
                    or product_id
                    or item.get("itemSequenceNumber")
                    or "Unknown item"
                ).strip(),
                "quantity": _quantity_amount(item),
            }
        )
    return rows


def summarize_po(order: dict[str, Any], account: AccountConfig) -> dict[str, Any]:
    details = order.get("orderDetails", {}) or {}
    items = details.get("items", []) or []
    total_qty = sum(_quantity_amount(item) for item in items)
    total_net = sum(_po_amount(item) for item in items)
    currency = money_currency(items[0].get("netCost")) if items else ""
    return {
        "account": account.label,
        "marketplace": account.marketplace_code,
        "po_number": order.get("purchaseOrderNumber", ""),
        "status": order.get("purchaseOrderState", ""),
        "po_date": str(details.get("purchaseOrderDate", ""))[:10],
        "ship_window": _ship_window(order, details),
        "sku_count": len(items),
        "total_qty": total_qty,
        "total_net": round(total_net, 2),
        "currency": currency,
        "items": _summarize_items(items),
    }


def fetch_unconfirmed_pos(account: AccountConfig) -> list[dict[str, Any]]:
    logger.info(
        "Checking %s (%s): all unconfirmed purchase orders",
        account.label,
        account.marketplace_code,
    )

    token_response = requests.post(
        LWA_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": account.credentials["refresh_token"],
            "client_id": account.credentials["lwa_app_id"],
            "client_secret": account.credentials["lwa_client_secret"],
        },
        timeout=30,
    )
    if not token_response.ok:
        raise RuntimeError(
            f"{account.label} LWA token request failed: "
            f"HTTP {token_response.status_code} {token_response.text[:500]}"
        )
    token_payload = token_response.json()
    access_token = token_payload.get("access_token")
    if not access_token:
        raise RuntimeError(f"{account.label} LWA response did not contain access_token")

    marketplace_id = MARKETPLACE_IDS[account.marketplace_code]
    rows: list[dict[str, Any]] = []
    next_token = None
    while True:
        params: dict[str, Any] = {
            "MarketplaceId": marketplace_id,
            "purchaseOrderState": "New",
            "sortOrder": "DESC",
            "limit": 100,
        }
        if next_token:
            params = {"nextToken": next_token}

        response = requests.get(
            f"{SP_API_BASE}/vendor/orders/v1/purchaseOrders",
            params=params,
            headers={
                "Accept": "application/json",
                "User-Agent": "Amazon-po-monitor/1.0",
                "x-amz-access-token": access_token,
            },
            timeout=30,
        )
        if not response.ok:
            raise RuntimeError(
                f"{account.label} PO fetch failed: "
                f"HTTP {response.status_code} {response.text[:500]}"
            )

        body = response.json()
        payload = body.get("payload") or body

        for order in payload.get("orders", []) or []:
            state = str(order.get("purchaseOrderState", "")).upper()
            if state in UNCONFIRMED_STATES:
                rows.append(summarize_po(order, account))

        pagination = payload.get("pagination") or body.get("pagination") or {}
        next_token = pagination.get("nextToken") or body.get("nextToken")
        if not next_token:
            break

    return rows


def build_message(rows: list[dict[str, Any]], asin_names: dict[str, str] | None = None) -> str:
    asin_names = asin_names or {}
    now = datetime.now(CST)
    now_str = now.strftime("%Y-%m-%d %H:%M")
    currency = next((row.get("currency") for row in rows if row.get("currency")), "EUR")
    total_amount = sum(float(row.get("total_net") or 0) for row in rows)
    current_month_amount = sum(
        float(row.get("total_net") or 0)
        for row in rows
        if _starts_current_month(row.get("ship_window"), now)
    )
    lines = [
        "📌 VC 后台有 unconfirmed PO，请及时查看",
        f"更新时间：{now_str}",
        f"数量：{len(rows)} 个",
        "",
    ]

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in sorted(rows, key=lambda item: (item.get("po_date", ""), item.get("po_number", "")), reverse=True):
        grouped.setdefault(row.get("po_date") or "日期未知", []).append(row)

    shown = 0
    for date, group in grouped.items():
        lines.append(f"- {date}：")
        for index, row in enumerate(group, start=1):
            if shown >= 30:
                continue
            amount = _format_money(row.get("currency") or currency, float(row.get("total_net") or 0))
            ship = _format_ship_window(row.get("ship_window"))
            lines.append(
                f"{index}. {row['po_number']} | "
                f"{ship or '-'} | 金额 {amount}"
            )
            for item in row.get("items", []) or []:
                label = (
                    asin_names.get(item.get("product_id", ""))
                    or item.get("fallback_label")
                    or item.get("vendor_product_id")
                    or item.get("product_id")
                    or "Unknown item"
                )
                lines.append(f"    {label} * {item.get('quantity', 0)}")
            shown += 1
        lines.append("")

    if shown < len(rows):
        lines.append(f"其余 {len(rows) - shown} 个 PO 未展开")
        lines.append("")
    lines.append(
        f"总金额 {currency or 'EUR'} {_format_w(total_amount)}，"
        f"{_format_rmb_w(total_amount * EUR_TO_RMB_RATE)}"
    )
    lines.append(f"本月挂单：{_format_rmb_w(current_month_amount * EUR_TO_RMB_RATE)}")
    return "\n".join(lines)


def build_no_po_message() -> str:
    now_str = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    return "\n".join(
        [
            "📌 VC 后台当前没有 unconfirmed PO",
            f"更新时间：{now_str}",
            "数量：0 个",
        ]
    )


def main() -> None:
    app_id, app_secret = require_feishu_env()
    xlsx_path = os.environ.get(
        "ASIN_NAMES_XLSX",
        os.path.join(os.path.dirname(__file__), "Xiaomi电视产品价格表2025-2026.xlsx"),
    )
    asin_names = load_asin_names(xlsx_path)
    accounts = [account for slot in ACCOUNT_SLOTS if (account := load_account(slot))]
    if not accounts:
        raise SystemExit("No valid Italy VC accounts configured.")

    all_rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for account in accounts:
        try:
            rows = fetch_unconfirmed_pos(account)
            logger.info("%s: found %d unconfirmed PO(s)", account.label, len(rows))
            all_rows.extend(rows)
        except Exception as exc:
            logger.exception("%s failed", account.label)
            errors.append(str(exc))

    if errors:
        send_feishu_text(
            app_id,
            app_secret,
            CHAT_ID,
            "⚠️ VC Italy unconfirmed PO 检查失败：\n" + "\n".join(f"- {e}" for e in errors),
        )
        raise SystemExit(1)

    if not all_rows:
        logger.info("No unconfirmed PO found. Sending Feishu status message.")
        send_feishu_text(app_id, app_secret, CHAT_ID, build_no_po_message())
        return

    send_feishu_text(app_id, app_secret, CHAT_ID, build_message(all_rows, asin_names))


if __name__ == "__main__":
    main()
