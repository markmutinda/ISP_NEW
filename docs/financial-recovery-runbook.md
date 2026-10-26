# Financial reporting backup and recovery

The Financial Reports page has a **Recovery backup** button. It downloads one versioned JSON file containing account balance checkpoints, manual cash events, account transfers, and receipt-to-account mappings. Subscription and SMS payments remain protected in their original tables.

## Recommended routine

Download a recovery backup after every balance check or mapping session. Keep one copy in encrypted cloud storage and one restricted server copy. Never commit a backup to Git because references and internal notes may be sensitive.

```bash
mkdir -p /root/netily_backups/financial
chmod 700 /root/netily_backups/financial
docker exec -i netily_backend python manage.py export_platform_financial_backup > "/root/netily_backups/financial/financial-$(date +%F-%H%M%S).json"
chmod 600 /root/netily_backups/financial/*.json
```

This does not pause payments.

## Validate and recover

Restore is a dry run unless `--apply` is supplied:

```bash
cat /root/netily_backups/financial/financial-YYYY-MM-DD-HHMMSS.json | docker exec -i netily_backend python manage.py restore_platform_financial_backup
```

After reviewing the counts, create records that are missing from the database:

```bash
cat /root/netily_backups/financial/financial-YYYY-MM-DD-HHMMSS.json | docker exec -i netily_backend python manage.py restore_platform_financial_backup --apply
```

Existing records are skipped. Recovery is atomic, so any failed write rolls back the entire restore.

## Reading balances

- **Confirmed anchor** means the checkpoint was checked against the bank or wallet.
- **Estimated** means the checkpoint was explicitly marked approximate.
- Live system balance means the checkpoint plus later recorded receipts, costs and transfers.
- Figures refresh each minute but cannot include an unrecorded bank fee, withdrawal or payment.

The current Account 2 working figure is approximately **KES 53,100**. It stays labelled as an estimate until its actual wallet or bank balance is checked.
