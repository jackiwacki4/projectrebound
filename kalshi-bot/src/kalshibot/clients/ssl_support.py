"""Shared TLS context.

macOS Python builds from python.org ship without wiring the CA root
certificates into OpenSSL, so every HTTPS request fails with
"CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate" until the
user runs Python's own "Install Certificates.command".

Rather than depend on that having been done, we prefer `certifi`'s bundled CA
roots when it is installed (it ships as a dependency of `cryptography`, which
we already require). If certifi is missing we fall back to the system default
context -- which is still a *verifying* context. Verification is never disabled.
"""
from __future__ import annotations

import ssl
from functools import lru_cache


@lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()
