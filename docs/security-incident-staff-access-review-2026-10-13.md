# Staff Access Security Review

Date: 13 October 2026  
Severity: High  
Status: Persistence indicators confirmed; code fixes prepared; production cleanup required after deployment

## Executive summary

The activity was not a harmless website scan. A remote actor used the public registration endpoint to create accounts, received valid sessions, changed a profile, explored protected APIs, and used the registration flow that led to unauthorized tenant creation.

The supplied Nginx log does **not** show a successful staff-management request. It does, however, show successful public registrations. Production UI evidence now confirms matching unauthorized administrator accounts inside more than one existing tenant. The most likely path is the old public `/core/auth/register/` endpoint: it accepted a client-supplied `role="admin"` while creating an account without the server-issued staff or superuser flags. Permission checks then trusted the role string by itself.

The repeated accounts `lpen02@yopmail.com`, `atkloopenet@yopmail.com`, `dpen02@yopmail.com`, and `atkdantenet@yopmail.com` are strong scripted-persistence indicators. They must be disabled after the deployment audit confirms their exact tenant schemas.

`billing@netily.io` is a separate application defect, not currently attributed to the attacker. Subscription invoice code created this synthetic NET-BILL customer identity with an administrator role. It should be an inactive, non-login customer account and is now normalized that way without deleting its invoices.

A separate staff lifecycle defect was confirmed in the code. Tenant-local staff records normally have empty public `company` and `tenant` foreign keys, but the staff list filtered against the public company foreign key. A valid staff account could therefore disappear from the page while remaining active and able to sign in. Visibility and authorization had drifted apart.

## Confirmed evidence

All Nginx timestamps below are UTC. Kenya time is three hours later.

| UTC | Kenya time | Result |
| --- | --- | --- |
| 12 Oct 22:43:09 | 13 Oct 01:43:09 | Public registration returned `201` and issued an authenticated account response. |
| 12 Oct 22:43:25 | 13 Oct 01:43:25 | The new session opened `/core/users/me/` successfully. |
| 12 Oct 22:43:29-31 | 13 Oct 01:43:29-31 | Attempts to retrieve other user IDs returned `403`. |
| 12 Oct 22:43:33 | 13 Oct 01:43:33 | User listing returned `403`; dashboard access returned `200`. |
| 12 Oct 22:44:03 | 13 Oct 01:44:03 | The actor changed their own profile successfully. |
| 12 Oct 22:47:30 | 13 Oct 01:47:30 | A second public registration returned `201`. |
| 12 Oct 22:47:44 | 13 Oct 01:47:44 | Company and user listings returned `403`. |

The wider incident evidence also identified unauthorized tenants named `kibera-fiber`, `pentest-2`, and `pentest-limited-1`. Those records should remain incident indicators even if they have already been purged.

Production Staff & Access evidence additionally showed:

| Tenant | Account | Assessment |
| --- | --- | --- |
| LoopeNET | `lpen02@yopmail.com` | Unauthorized administrator indicator |
| LoopeNET | `atkloopenet@yopmail.com` | Unauthorized administrator indicator |
| DANTENET | `dpen02@yopmail.com` | Unauthorized administrator indicator |
| DANTENET | `atkdantenet@yopmail.com` | Unauthorized administrator indicator |
| Multiple tenants | `billing@netily.io` | Internal NET-BILL identity incorrectly classified as administrator |

## What is not confirmed

- No supplied line shows a successful `POST /api/v1/core/users/`; the unauthorized administrators were most likely created through the vulnerable public registration endpoint instead of the authenticated staff endpoint.
- Kelvin Ndungu in the supplied audit history is a PPPoE customer login, not a staff login.
- There is no evidence in the supplied window that existing tenant customer data was read successfully.
- Nginx access logs alone cannot identify roles or tenant ownership from a successful registration because they do not record the JSON request body.

## Root causes

1. A public registration API accepted a client-supplied dashboard role even though its frontend link was hidden. Hiding a link is not an access control.
2. Staff creation accepted client-provided company and tenant fields. These fields must always come from the authenticated request context.
3. Staff updates allowed any company staff role, not only the principal tenant administrator.
4. The staff list treated a public company foreign key as the tenant boundary. Tenant-local records intentionally do not use that cross-schema foreign key, so historical staff could become invisible.
5. The application used both `role` and Django's `is_staff` flag without a single reconciliation path. A mismatch could affect display and permission decisions differently.
6. Subscription billing created `billing@netily.io` as an active administrator even though it is only an invoice owner.

## Changes implemented

- Staff create, edit, and delete operations require an authenticated tenant administrator.
- Staff mutations are rate-limited per authenticated administrator.
- A dashboard role is trusted only when the server-issued `is_staff` flag is present or the account is a genuine superuser. A role string alone no longer authenticates or authorizes dashboard access.
- Existing JWTs for untrusted role-only accounts are rejected immediately by the authentication layer.
- The unused compatibility registration function was removed so it cannot be accidentally re-exposed later.
- Only `staff`, `technician`, `accountant`, and `support` can be delegated. The staff API cannot create another principal administrator or a superuser.
- Client-provided `company`, `tenant`, `is_staff`, `is_superuser`, and `is_verified` values are rejected.
- Tenant ownership is derived on the server from the active schema and request context.
- Staff listing now uses the PostgreSQL tenant schema as its isolation boundary and no longer drops records with empty public foreign keys.
- The principal administrator and platform superusers cannot be edited or deleted through the staff endpoint.
- Staff updates normalize `is_staff`, `is_superuser`, and verification flags on the server.
- Technician role checks now agree with the rest of the staff permission model.
- A management command now audits every tenant schema, reports role/flag drift, repairs safe consistency fields, and can deactivate a named suspicious account.
- The audit repair path is revoke-only by default. It cannot accidentally turn an attacker-supplied role into trusted access.
- Legitimate historical staff missing their approval flag can be restored only by exact tenant and email selection.
- `billing@netily.io` is centralized as an inactive customer with an unusable password, no staff flag, and no superuser status.

## Production audit

Run read-only review first:

```bash
docker exec -it netily_backend python manage.py audit_tenant_staff_accounts
```

Focus on accounts created around the incident:

```bash
docker exec -it netily_backend python manage.py audit_tenant_staff_accounts \
  --since 2026-10-12T21:00:00Z
```

Show only records with detected privilege or consistency warnings:

```bash
docker exec -it netily_backend python manage.py audit_tenant_staff_accounts \
  --suspicious-only
```

Inspect one tenant:

```bash
docker exec -it netily_backend python manage.py audit_tenant_staff_accounts \
  --tenant TENANT_SUBDOMAIN
```

After reviewing the output, repair harmless role/visibility drift:

```bash
docker exec -it netily_backend python manage.py audit_tenant_staff_accounts \
  --repair-flags
```

Normalize the internal billing identity in every tenant without deleting invoices:

```bash
docker exec -it netily_backend python manage.py audit_tenant_staff_accounts \
  --normalize-system-accounts
```

Disable the confirmed LoopeNET indicators:

```bash
docker exec -it netily_backend python manage.py audit_tenant_staff_accounts \
  --tenant loopenet \
  --deactivate-email lpen02@yopmail.com \
  --deactivate-email atkloopenet@yopmail.com
```

Disable the confirmed DANTENET indicators:

```bash
docker exec -it netily_backend python manage.py audit_tenant_staff_accounts \
  --tenant dantenet \
  --deactivate-email dpen02@yopmail.com \
  --deactivate-email atkdantenet@yopmail.com
```

If the audit finds a legitimate historical staff member whose staff flag is missing, approve only that exact account:

```bash
docker exec -it netily_backend python manage.py audit_tenant_staff_accounts \
  --tenant TENANT_SUBDOMAIN \
  --approve-staff-email approved.staff@example.com
```

Deactivate a confirmed unauthorized account in a specific tenant:

```bash
docker exec -it netily_backend python manage.py audit_tenant_staff_accounts \
  --tenant TENANT_SUBDOMAIN \
  --deactivate-email suspicious@example.com
```

The deactivation command disables the account and makes its password unusable. Existing JWT access is rejected because authentication reloads the user and requires an active account.

## Deployment checks

```bash
docker exec -it netily_backend python manage.py check
docker exec -it netily_backend python manage.py test apps.core.tests.StaffAccountSecurityTests
```

Then verify with a normal tenant administrator:

1. Open **Staff & access** and confirm historical active and inactive staff are visible.
2. Create one support account and sign in with it.
3. Confirm the support account cannot open staff management or call create, edit, or delete staff APIs.
4. Deactivate the support account and confirm its existing session is rejected.
5. Delete the test account and confirm it can no longer authenticate.

## Remaining operational actions

- Review all accounts created from 12 October 2026 onward, not only names containing `pentest`.
- Search every tenant for additional non-superuser `admin` accounts and repeated Yopmail naming patterns.
- Preserve Nginx, Django, database, and audit logs for the incident window before log rotation.
- Reset passwords for any principal administrator whose credentials may have been exposed.
- Keep the public registration route blocked at both Nginx and Django.
- Add alerts for public registration attempts, repeated `403` enumeration, cross-tenant ownership input, and tenant creation outside the approved superadmin workflow.
