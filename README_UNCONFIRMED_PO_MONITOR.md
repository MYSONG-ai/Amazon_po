# Italy Unconfirmed PO Monitor

This job checks one Italy Vendor Central account and sends a Feishu group
message only when unconfirmed POs are found.

Workflow:

- `.github/workflows/vendor_it_unconfirmed_po_daily.yml`
- Trigger: external scheduler or manual `workflow_dispatch`
- Feishu chat: `oc_c95fca0100bedfff9cabf3c37e45d4cb`
- RMB conversion: GitHub variable `EUR_TO_RMB_RATE`, default `7.8`

Required GitHub secrets:

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `IT_SP_REFRESH_TOKEN`

The script uses direct Vendor Orders requests with LWA credentials only. Add
the account-specific app credentials:

- `IT_SP_LWA_APP_ID`
- `IT_SP_LWA_CLIENT_SECRET`

Unconfirmed states default to:

- `New`
- `NEW`
- `Unconfirmed`
- `UNCONFIRMED`
