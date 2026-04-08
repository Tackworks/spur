# Security Policy

## Scope

Tackworks projects (Tack, Chock, Spur) are **self-hosted tools designed to run on trusted networks** — your LAN, a VPN, or behind a reverse proxy. They are not cloud services and are not designed to be exposed directly to the public internet.

## By Design, Not by Oversight

The following are **intentional design decisions**, not vulnerabilities:

- **No built-in authentication.** These tools assume a trusted network. Use a reverse proxy (Caddy, nginx, Traefik) or a VPN (Tailscale, WireGuard) to control access.
- **No built-in HTTPS.** Same reason. Terminate TLS at your reverse proxy.
- **No rate limiting.** Handled at the infrastructure layer, not the application layer.

These are deployment concerns. If you expose a Tackworks tool to the open internet without protection, that is a configuration issue on your end.

## What Counts as a Vulnerability

We do want to hear about:

- **SQL/SQLite injection** — unsanitized input reaching database queries
- **Cross-site scripting (XSS)** — unsanitized output rendered in the web UI
- **Path traversal** — file access outside intended directories
- **Remote code execution** — any way to execute arbitrary code via the application
- **Server-side request forgery (SSRF)** — tricking the server into making unintended requests
- **Denial of service via application logic** — crashes or resource exhaustion from crafted input

## Reporting a Vulnerability

Email **tackworks@proton.me** with:

- Description of the issue
- Steps to reproduce
- Affected project and version (or commit hash)

Please do **not** open a public GitHub issue for security vulnerabilities.

## Response Time

This is an open-source project maintained on a best-effort basis. We will acknowledge reports as quickly as we can, but there are no SLAs. If a fix is warranted, we will coordinate disclosure with the reporter before publishing.
