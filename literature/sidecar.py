"""Frozen sidecar entry point used by the packaged Tauri application."""

import sys


def check_tls() -> None:
    """Offline check of the actual frozen TLS runtime, before releasing a bundle."""
    import json
    import ssl
    import httpx

    from literature.providers import normalize_proxy_environment, normalize_tls_environment

    normalize_proxy_environment()
    normalize_tls_environment()
    context = ssl.create_default_context()
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise RuntimeError("TLS 证书校验不可用")
    with httpx.Client():
        pass
    print(json.dumps({"tls": "ok", "certificate_verification": True}))


if __name__ == "__main__":
    if sys.argv[1:] == ["--check-tls"]:
        check_tls()
    else:
        from literature.api import main
        main()
