"""
Amazon Vendor Central 每日报告
- 销量：SP-API Reports API (GET_VENDOR_SALES_REPORT)
- PO：SP-API Vendor Orders API
- 发送飞书群消息
"""

import io
import os
import csv
import gzip
import json
import time
import logging
import requests
from datetime import datetime, timedelta, timezone

from sp_api.api import Reports, VendorOrders
from sp_api.base import Marketplaces, SellingApiException

CST = timezone(timedelta(hours=8))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ===================== 配置 =====================
_MARKETPLACE_MAP = {
    "DE": Marketplaces.DE,
    "UK": Marketplaces.UK,
    "FR": Marketplaces.FR,
    "IT": Marketplaces.IT,
    "ES": Marketplaces.ES,
}
MARKETPLACE = _MARKETPLACE_MAP.get(os.environ.get("SP_MARKETPLACE", "DE"), Marketplaces.DE)
MARKETPLACE_NAME = os.environ.get("SP_MARKETPLACE", "DE")

SP_CREDENTIALS = {
    "lwa_app_id": os.environ["SP_LWA_APP_ID"],
    "lwa_client_secret": os.environ["SP_LWA_CLIENT_SECRET"],
    "refresh_token": os.environ["SP_REFRESH_TOKEN"],
    "aws_access_key": os.environ["SP_AWS_ACCESS_KEY"],
    "aws_secret_key": os.environ["SP_AWS_SECRET_KEY"],
    "role_arn": os.environ["SP_ROLE_ARN"],
}

FEISHU_APP_ID = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET"]
FEISHU_CHAT_ID = os.environ["FEISHU_VENDOR_CHAT_ID"]

REPORT_WAIT_SECONDS = 60   # 每次轮询间隔（秒）
REPORT_MAX_POLLS = 20      # 最多轮询次数（共等待约 20 分钟）


# ===================== 飞书 =====================
def _feishu_token() -> str:
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
    )
    return resp.json()["tenant_access_token"]


def send_feishu_text(text: str):
    token = _feishu_token()
    resp = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "receive_id": FEISHU_CHAT_ID,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
    )
    if resp.json().get("code") != 0:
        logger.error(f"飞书文本发送失败: {resp.json()}")


def send_feishu_post(title: str, content: list):
    token = _feishu_token()
    resp = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "receive_id": FEISHU_CHAT_ID,
            "msg_type": "post",
            "content": json.dumps(
                {"zh_cn": {"title": title, "content": content}}, ensure_ascii=False
            ),
        },
    )
    if resp.json().get("code") != 0:
        logger.error(f"飞书富文本发送失败: {resp.json()}")


# ===================== SP-API 报告 =====================
def _request_report(report_type: str, start: datetime, end: datetime) -> str | None:
    """提交报告请求，返回 reportId；失败返回 None"""
    api = Reports(credentials=SP_CREDENTIALS, marketplace=MARKETPLACE)
    try:
        resp = api.create_report(
            reportType=report_type,
            dataStartTime=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            dataEndTime=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            marketplaceIds=[MARKETPLACE.marketplace_id],
        )
        report_id = resp.payload["reportId"]
        logger.info(f"报告已请求 [{report_type}] reportId={report_id}")
        return report_id
    except SellingApiException as e:
        logger.error(f"创建报告失败 [{report_type}]: {e}")
        return None


def _poll_report(report_id: str) -> str | None:
    """轮询报告状态，返回 reportDocumentId；超时或失败返回 None"""
    api = Reports(credentials=SP_CREDENTIALS, marketplace=MARKETPLACE)
    for attempt in range(1, REPORT_MAX_POLLS + 1):
        time.sleep(REPORT_WAIT_SECONDS)
        try:
            resp = api.get_report(report_id)
            status = resp.payload["processingStatus"]
            logger.info(f"报告状态 [{report_id}] 第{attempt}次: {status}")
            if status == "DONE":
                return resp.payload.get("reportDocumentId")
            if status in ("FATAL", "CANCELLED"):
                logger.error(f"报告生成失败: {status}")
                return None
        except Exception as e:
            logger.warning(f"轮询报告出错: {e}")
    logger.error(f"等待报告超时 [{report_id}]")
    return None


def _download_report(doc_id: str) -> str | None:
    """下载报告文档，返回文本内容"""
    api = Reports(credentials=SP_CREDENTIALS, marketplace=MARKETPLACE)
    try:
        resp = api.get_report_document(doc_id)
        url = resp.payload["url"]
        compression = resp.payload.get("compressionAlgorithm")
        raw = requests.get(url, timeout=60).content
        if compression == "GZIP":
            raw = gzip.decompress(raw)
        return raw.decode("utf-8")
    except Exception as e:
        logger.error(f"下载报告失败: {e}")
        return None


def fetch_sales_report(report_date: datetime) -> list[dict]:
    """
    拉取指定日期的销量报告（GET_VENDOR_SALES_REPORT）。
    Vendor 数据通常有 1-2 天延迟，此处拉取前一天数据。
    返回解析后的行列表（字段名保持原始英文）。
    """
    start = report_date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    end = report_date.replace(hour=23, minute=59, second=59, microsecond=0, tzinfo=timezone.utc)

    report_id = _request_report("GET_VENDOR_SALES_REPORT", start, end)
    if not report_id:
        return []

    doc_id = _poll_report(report_id)
    if not doc_id:
        return []

    content = _download_report(doc_id)
    if not content:
        return []

    reader = csv.DictReader(io.StringIO(content), delimiter="\t")
    return list(reader)


# ===================== SP-API Vendor Orders =====================
def fetch_po_list(days_back: int = 14) -> list[dict]:
    """
    拉取最近 days_back 天内的 PO 订单列表。
    返回简化后的 PO 汇总列表。
    """
    api = VendorOrders(credentials=SP_CREDENTIALS, marketplace=MARKETPLACE)
    created_after = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    po_list = []
    next_token = None

    while True:
        try:
            kwargs = {"createdAfter": created_after, "limit": 100}
            if next_token:
                kwargs = {"nextToken": next_token}
            resp = api.get_purchase_orders(**kwargs)
        except SellingApiException as e:
            logger.error(f"获取 PO 失败: {e}")
            break

        orders = resp.payload.get("orders", [])
        for order in orders:
            details = order.get("orderDetails", {})
            items = details.get("items", [])
            total_qty = sum(
                item.get("orderedQuantity", {}).get("amount", 0) for item in items
            )
            total_net = sum(
                float(item.get("netCost", {}).get("amount", 0)) for item in items
            )
            currency = (
                items[0].get("netCost", {}).get("currencyCode", "") if items else ""
            )
            po_list.append(
                {
                    "po_number": order.get("purchaseOrderNumber", ""),
                    "status": order.get("purchaseOrderState", ""),
                    "po_date": details.get("purchaseOrderDate", "")[:10],
                    "ship_window": details.get("shipWindow", ""),
                    "sku_count": len(items),
                    "total_qty": total_qty,
                    "total_net": total_net,
                    "currency": currency,
                }
            )

        next_token = resp.next_token
        if not next_token:
            break

    return po_list


# ===================== 报告构建 =====================
def _safe_int(v) -> int:
    try:
        return int(str(v).replace(",", "").strip())
    except Exception:
        return 0


def _safe_float(v) -> float:
    try:
        return float(str(v).replace(",", "").replace("€", "").replace("£", "").strip())
    except Exception:
        return 0.0


def build_sales_section(rows: list[dict]) -> list[list]:
    """构建飞书富文本的销量部分"""
    if not rows:
        return [[{"tag": "text", "text": "  暂无数据（报告可能未就绪或无权限）"}]]

    # 尝试识别字段名（不同站点列名可能略有差异）
    sample = rows[0]
    asin_key = next((k for k in sample if "ASIN" in k.upper()), None)
    title_key = next((k for k in sample if "Title" in k or "TITLE" in k), None)
    units_key = next((k for k in sample if "Shipped Units" in k or "ShippedUnits" in k), None)
    rev_key = next((k for k in sample if "Shipped Revenue" in k or "ShippedRevenue" in k), None)
    ordered_units_key = next((k for k in sample if "Ordered Units" in k or "OrderedUnits" in k), None)

    total_shipped = 0
    total_ordered = 0
    total_revenue = 0.0
    lines = []

    for row in rows[:30]:
        asin = row.get(asin_key, "") if asin_key else ""
        title = (row.get(title_key, "") or "")[:30] if title_key else ""
        shipped = _safe_int(row.get(units_key, 0)) if units_key else 0
        ordered = _safe_int(row.get(ordered_units_key, 0)) if ordered_units_key else 0
        revenue = _safe_float(row.get(rev_key, 0)) if rev_key else 0.0

        total_shipped += shipped
        total_ordered += ordered
        total_revenue += revenue

        if asin:
            parts = f"  {asin}"
            if title:
                parts += f"  {title}"
            parts += f"  已发货:{shipped}件"
            if ordered:
                parts += f"  已订购:{ordered}件"
            if revenue:
                parts += f"  收入:{revenue:.2f}"
            lines.append([{"tag": "text", "text": parts}])

    # 汇总行
    summary = f"  ✦ 合计  已发货:{total_shipped}件  已订购:{total_ordered}件  总收入:{total_revenue:.2f}"
    lines.append([{"tag": "text", "text": summary}])
    return lines


def build_po_section(pos: list[dict]) -> list[list]:
    """构建飞书富文本的 PO 部分"""
    if not pos:
        return [[{"tag": "text", "text": "  暂无 PO 数据"}]]

    status_icon = {"NEW": "🆕", "ACKNOWLEDGED": "✅", "CLOSED": "🔒", "RECEIVED": "📥"}
    lines = []

    # 按状态分组统计
    by_status: dict[str, list] = {}
    for po in pos:
        by_status.setdefault(po["status"], []).append(po)

    for status, group in by_status.items():
        icon = status_icon.get(status, "📋")
        total_qty = sum(p["total_qty"] for p in group)
        lines.append([
            {"tag": "text", "text": f"  {icon} {status}  共{len(group)}个PO  合计{total_qty}件"}
        ])

    lines.append([{"tag": "text", "text": ""}])

    # 详情（最新 10 条）
    for po in pos[:10]:
        icon = status_icon.get(po["status"], "📋")
        currency = po["currency"]
        net_str = f"  净额:{currency}{po['total_net']:.2f}" if po["total_net"] else ""
        ship_str = f"  发货窗口:{po['ship_window']}" if po["ship_window"] else ""
        lines.append([{
            "tag": "text",
            "text": (
                f"  {icon} {po['po_number']}  {po['po_date']}"
                f"  {po['sku_count']}个SKU  {po['total_qty']}件{net_str}{ship_str}"
            ),
        }])

    return lines


def send_daily_report(sales_rows: list[dict], po_list: list[dict], report_date: datetime):
    now_str = datetime.now(CST).strftime("%Y/%m/%d %H:%M")
    date_str = report_date.strftime("%Y-%m-%d")
    title = f"📊 Vendor Central 日报 [{MARKETPLACE_NAME}] {date_str}"

    content: list[list] = []

    # 销量
    content.append([{"tag": "text", "text": f"── 销量报告（{date_str}）──"}])
    content.extend(build_sales_section(sales_rows))
    content.append([{"tag": "text", "text": ""}])

    # PO
    content.append([{"tag": "text", "text": "── 采购订单（近14天）──"}])
    content.extend(build_po_section(po_list))
    content.append([{"tag": "text", "text": ""}])

    content.append([{"tag": "text", "text": f"更新时间：{now_str}"}])

    send_feishu_post(title, content)
    logger.info("飞书报告已发送")


# ===================== 主入口 =====================
def main():
    cst_now = datetime.now(CST)
    # Vendor 数据通常有 1-2 天延迟；使用 2 天前确保数据完整
    report_date = cst_now - timedelta(days=2)
    logger.info(f"开始拉取 Vendor Central 数据，销量日期：{report_date.strftime('%Y-%m-%d')}，站点：{MARKETPLACE_NAME}")

    # 并行拉取（PO 是实时的，销量需等待报告生成）
    logger.info("正在请求销量报告（预计等待 5-20 分钟）...")
    sales_rows = fetch_sales_report(report_date)
    logger.info(f"销量数据：{len(sales_rows)} 条 ASIN")

    logger.info("正在拉取 PO 列表...")
    po_list = fetch_po_list(days_back=14)
    logger.info(f"PO 数据：{len(po_list)} 条")

    send_daily_report(sales_rows, po_list, report_date)


if __name__ == "__main__":
    main()
