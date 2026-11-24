# Security

Each account's data is only returned to that account by the UI. There is no admin data
API and no health model in Django admin: the admin holds accounts and API-token metadata
only. A superuser can reset a password, read the database file and read its backups, so the
operator is trusted. There is no encryption at rest and no client-side encryption. The API
(`/api/v1`) does not exist yet in this build; when it ships, the same rule applies — another
account's id answers `404`, never `403`.

Sessions are `__Host-health_info_session` (`Secure`, `HttpOnly`, `SameSite=Lax`). Sign-in
locks out after 5 failures in 15 minutes per username and client IP (`429`), an in-process
dict that a restart clears. The proxy rate zones are the second, independent limit.

Report a vulnerability privately to the operator rather than in a public issue.
