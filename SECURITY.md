# Security

HyperOptions is a loopback-only local workstation. Do not expose the API or Vite dev server on a public interface.

## Reporting a vulnerability

Please use [GitHub Security Advisories](https://github.com/hypertrial/hyperoptions/security/advisories/new) for this repository. Do not open a public issue for an unreleased vulnerability.

## Scope notes

- The FastAPI process admits localhost / `127.0.0.1` / `::1` Host headers only.
- The browser talks only to the local API. It must never call `api.nasdaq.com`.
- This repository should not contain credentials, Pad exports, or brokerage/account data.
