"""Optional OIDC settings, separate from Meta publishing credentials.

Deployment environment variables take precedence over settings.py so secrets can
be injected by the hosting platform instead of committed to source control.
"""
import os


OIDC_KEYS = ("OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET")


def load_web_auth_settings(settings_module=None, environ=None):
    if settings_module is None:
        import settings as settings_module
    if environ is None:
        environ = os.environ

    values = {}
    for key in OIDC_KEYS:
        if key in environ:
            raw = environ[key]
        else:
            raw = getattr(settings_module, key, "")
        values[key] = str(raw or "").strip()

    issuer = values["OIDC_ISSUER"]
    client_id = values["OIDC_CLIENT_ID"]
    client_secret = values["OIDC_CLIENT_SECRET"]
    configured = (issuer, client_id, client_secret)
    if not any(configured):
        return None
    if not all(configured):
        raise ValueError(
            "Configure OIDC_ISSUER, OIDC_CLIENT_ID and OIDC_CLIENT_SECRET together."
        )
    if not issuer.startswith("https://"):
        raise ValueError("OIDC_ISSUER must use HTTPS.")
    return {"issuer": issuer, "client_id": client_id, "client_secret": client_secret}
