# Security

Each account's data is only returned to that account by the UI and by `/api/v1`. There is
no admin data API and no health model in Django admin: the admin holds accounts and
API-token metadata only. A superuser can reset a password, read the database file and read
its backups, so the operator is trusted. There is no encryption at rest and no client-side
encryption.

API tokens are digest hashes (`sha256`, prefix lookup then `compare_digest`); the secret is
shown once and never stored or logged. A token carries no scopes: it can write every
resource the API exposes for its account. Another account's id answers `404`, never `403`,
and error messages never echo a value, dose, substance or note. `/api/v1/docs` and
`/api/v1/openapi.json` need a session or the same bearer token. Requests over 64 KiB on
`/api/v1` are rejected; there is no per-token rate limit, so `429` there comes from the
proxy.

Sessions are `__Host-health_info_session` (`Secure`, `HttpOnly`, `SameSite=Lax`). Sign-in
locks out after 5 failures in 15 minutes per username and client IP (`429`), an in-process
dict that a restart clears. The proxy rate zones are the second, independent limit.

Report a vulnerability privately to the operator rather than in a public issue.
