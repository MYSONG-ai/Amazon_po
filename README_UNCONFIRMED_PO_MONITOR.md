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

The script can reuse these shared SP-API secrets if this Italy account uses the
same app/AWS role as the existing job:

- `SP_LWA_APP_ID`
- `SP_LWA_CLIENT_SECRET`
- `SP_AWS_ACCESS_KEY`
- `SP_AWS_SECRET_KEY`
- `SP_ROLE_ARN`

If this Italy account needs separate app/AWS credentials, add account-specific
secrets instead:

- `IT_SP_LWA_APP_ID`
- `IT_SP_LWA_CLIENT_SECRET`
- `IT_SP_AWS_ACCESS_KEY`
- `IT_SP_AWS_SECRET_KEY`
- `IT_SP_ROLE_ARN`

Unconfirmed states default to:

- `New`
- `NEW`
- `Unconfirmed`
- `UNCONFIRMED`
