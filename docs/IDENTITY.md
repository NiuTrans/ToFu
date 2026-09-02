# Identity and credential authority

This document is the entry point for authentication subjects, repository
ownership, and bearer credentials. The field-level authority is
[`contracts/identity_v1.yaml`](../contracts/identity_v1.yaml).

## Three identifiers, three jobs

| Identifier | Shape | Used for | Never used for |
|---|---|---|---|
| `account_user_id` | opaque string such as `usr_*` | login, account administration, billing | repository ownership |
| `owner_user_id` | positive integer | every user-owned repository, task, event, cache, and device registry | display or password lookup |
| `tenant_id` | optional opaque string | future enterprise partition boundary | an implicit process global |

Personal mode reserves owner `1`. Each tenant account receives a different
numeric owner from a transactionally locked Sidecar sequence. An account ID is
never parsed into a number and an owner ID is never exposed as the account
subject.

## Request flow

```text
Bearer/cookie/device credential
  -> auth_credentials authenticate + last-used touch (one transaction)
  -> AuthContext(key, owner, account, tenant, scopes)
  -> PrincipalContext(subject=key, owner, tenant, scopes)
  -> owner-scoped service/repository operation
```

`routes/api_v1/auth.py` is the only HTTP authentication boundary. Missing or
invalid ownership is denied there. Lower layers receive `PrincipalContext` or
an explicit positive owner and never consult a current-user global.

The account field may be empty for service credentials. When it is present,
authentication succeeds only while that account exists, is active, and maps
to the credential's owner. Suspending an account therefore invalidates all of
its bound credentials without a cache-invalidation race.

## Credential lifecycle

`auth_credentials` in the Storage Sidecar is the sole authority. Creating a
credential returns plaintext once and persists only its SHA-256 digest.
List/get operations never return the digest. List, get, update, and revoke all
require the exact owner and tenant boundary.

Authentication and `last_used_at` update are one Sidecar command. Disabled,
expired, revoked, unknown, and suspended-account credentials fail closed.
Admin privilege is a credential tier: updating scopes cannot promote a live
credential to admin or demote an admin credential accidentally.

The first personal credential uses an atomic create-if-empty command. The
mode-0600 `.first_run_token` file is only a one-time recovery copy of that
plaintext; deleting or corrupting it cannot alter credential authority.

## Device bridge

Remote browser and desktop agents use an owner-scoped credential with the
literal `agents:bridge` scope. There is no deployment-wide bridge secret,
credential-free LAN mode, or IP-address trust. The packaged desktop app uses a
non-persisted process capability for its in-process agent; that capability is
accepted only by `/api/desktop/poll` and always maps to personal owner `1`.

Extension downloads and agent installers mint a fresh device credential. If
the credential authority is unavailable, download returns 503. Shipping a
package known to be unable to connect is not a valid degraded mode.

## Change map

| Change | First files | Required guard |
|---|---|---|
| Account fields or allocation | `lib/billing/users.py`, `operations_pkg/_tenant.py` | `tests/test_identity_http_contract.py` |
| Request principal | `lib/api_keys/_context.py`, `lib/identity.py`, `routes/api_v1/auth.py` | `tests/test_principal_context.py` |
| Credential lifecycle | `lib/api_keys/`, `operations_pkg/_credentials.py` | `tests/test_api_keys.py`, `tests/test_storage_sidecar_contract.py` |
| Device authentication | `lib/bridge_auth.py`, `routes/_bridge_caller.py` | `tests/test_bridge_auth.py` |

Any new identity field starts in the machine contract, is carried explicitly
through `AuthContext` and `PrincipalContext`, and is tested for wrong-owner and
suspension behavior before a repository consumes it.
