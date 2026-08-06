# Amazon Vendor Central CLI

This CLI reuses the existing SP-API helpers in this folder and reads Amazon
credentials from environment variables or `.env`.

## Required `.env`

```env
SP_LWA_APP_ID=
SP_LWA_CLIENT_SECRET=
SP_REFRESH_TOKEN=
SP_MARKETPLACE=DE
```

`Application ID` / solution id from the Amazon developer console is useful for
identification, but this CLI does not need it for normal SP-API calls.

The CLI calls SP-API directly with the LWA access token in
`x-amz-access-token`. It does not require AWS access key, AWS secret key, or
role ARN.

## Commands

Show implemented first-batch commands:

```bash
python vendor_cli.py capabilities
```

Check whether local credentials are complete:

```bash
python vendor_cli.py check-env
```

List recent retail purchase orders:

```bash
python vendor_cli.py po list --days 7
```

Fetch standard Vendor Analytics reports:

```bash
python vendor_cli.py report sales --date 2026-07-12
python vendor_cli.py report realtime-sales --date 2026-07-13
python vendor_cli.py report inventory --date 2026-07-12
python vendor_cli.py report forecast --start 2026-07-01 --end 2026-07-31
python vendor_cli.py report traffic --start 2026-07-01 --end 2026-07-13
python vendor_cli.py report margin --start 2026-07-01 --end 2026-07-31
```

Report commands submit an async Amazon report request and return immediately
with a `reportId`. Check the result later:

```bash
python vendor_cli.py report-status 119357020647 --kind sales
```

To keep the old blocking behavior, add `--wait`:

```bash
python vendor_cli.py report sales --date 2026-07-12 --wait
```

Output JSON:

```bash
python vendor_cli.py report-status 119357020647 --kind sales --format json
```

Export CSV:

```bash
python vendor_cli.py report-status 119357020647 --kind inventory --output output/inventory.csv
```

## Supported First-Batch APIs

- Vendor Orders API v1
- Reports API with:
  - `GET_VENDOR_SALES_REPORT`
  - `GET_VENDOR_REAL_TIME_SALES_REPORT`
  - `GET_VENDOR_INVENTORY_REPORT`
  - `GET_VENDOR_FORECASTING_REPORT`
  - `GET_VENDOR_TRAFFIC_REPORT`
  - `GET_VENDOR_REAL_TIME_INVENTORY_REPORT`
  - `GET_VENDOR_REAL_TIME_TRAFFIC_REPORT`
  - `GET_VENDOR_NET_PURE_PRODUCT_MARGIN_REPORT`
