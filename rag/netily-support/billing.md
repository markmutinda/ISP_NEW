# Netily Billing Rules

Use this guide for tenant-facing billing questions.

## Trial and activation

New tenants receive a free trial for the number of days configured on their account. When the trial ends, the tenant account pauses until the tenant pays the activation fee.

The standard activation fee is KES 500. This payment starts the first 30-day billing cycle.

## Usage billing after activation

After activation, recurring invoices are based on usage within each 30-day billing cycle.

- PPPoE usage: KES 20 for every PPPoE client with a footprint in the cycle.
- Hotspot usage: 3% of total hotspot revenue in the cycle.
- Monthly minimum: if PPPoE usage plus hotspot usage is below KES 500, the monthly usage invoice rounds up to KES 500.

The recurring estimate is:

`max(PPPoE footprint charge + 3% hotspot revenue, KES 500 monthly minimum)`

## Invoice timing

Netily generates the usage invoice 5 days before the current 30-day cycle expires. A reminder is sent 1 day before the due date. Payment is due by the cycle end date.
