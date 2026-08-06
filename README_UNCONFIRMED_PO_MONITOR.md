# Italy Unconfirmed PO Monitor

This job checks two Italy Vendor Central accounts every day and sends a Feishu
group message only when new unconfirmed POs are found.

Workflow:

- `.github/workflows/vendor_it_unconfirmed_po_daily.yml`
- Schedule: every day at 09:00 China time
- Manual run input: `days_back`, default `1`
- Feishu chat: `oc_c95fca0100bedfff9cabf3c37e45d4cb`

Required GitHub secrets:

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `IT1_SP_REFRESH_TOKEN`
- `IT2_SP_REFRESH_TOKEN`

The script can reuse these shared SP-API secrets if both Italy accounts use the
same app/AWS role:

- `SP_LWA_APP_ID`
- `SP_LWA_CLIENT_SECRET`
- `SP_AWS_ACCESS_KEY`
- `SP_AWS_SECRET_KEY`
- `SP_ROLE_ARN`

If either Italy account needs separate app/AWS credentials, add account-specific
secrets instead:

- `IT1_SP_LWA_APP_ID`
- `IT1_SP_LWA_CLIENT_SECRET`
- `IT1_SP_AWS_ACCESS_KEY`
- `IT1_SP_AWS_SECRET_KEY`
- `IT1_SP_ROLE_ARN`
- `IT2_SP_LWA_APP_ID`
- `IT2_SP_LWA_CLIENT_SECRET`
- `IT2_SP_AWS_ACCESS_KEY`
- `IT2_SP_AWS_SECRET_KEY`
- `IT2_SP_ROLE_ARN`

Unconfirmed states default to:

- `New`
- `NEW`
- `Unconfirmed`
- `UNCONFIRMED`
