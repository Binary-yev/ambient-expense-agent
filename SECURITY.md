# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| `main`  | ✅ Active development |
| Others  | ❌ Not supported |

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

If you discover a security vulnerability in this project, please report it
responsibly via one of the following methods:

1. **GitHub Private Security Advisory** (preferred):
   Go to the **Security** tab of this repository and click
   **"Report a vulnerability"**.

2. **Email**: Send details to the repository maintainer directly. Look for
   contact information in the contributor profile.

Please include as much detail as possible:
- A description of the vulnerability and its potential impact
- Steps to reproduce the issue
- Any suggested mitigations

We aim to acknowledge reports within **48 hours** and provide a fix timeline
within **7 days** for critical issues.

## Scope

This security policy covers:
- The agent source code in `expense_agent/`
- The FastAPI trigger service in `expense_agent/fast_api_app.py`
- The evaluation pipeline in `tests/eval/`

Out of scope:
- Third-party dependencies (report those to the upstream maintainers)
- The ADK framework itself (report to [Google ADK](https://github.com/google/adk-python))

## Security Design

See the [Security Design section](README.md#-security-design) in the README for
information on how PII scrubbing and prompt injection defence are implemented.
