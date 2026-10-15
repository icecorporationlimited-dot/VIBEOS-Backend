"""Optional OIDC settings, separate from Meta publishing credentials."""


def load_web_auth_settings(settings_module=None):
    if settings_module is None:
        import settings as settings_module
    issuer = str(getattr(settings_module, "OIDC_ISSUER", "") or "").strip()
    client_id = str(getattr(settings_module, "OIDC_CLIENT_ID", "") or "").strip()
    client_secret = str(getattr(settings_module, "OIDC_CLIENT_SECRET", "") or "").strip()
    values = (issuer, client_id, client_secret)
    if not any(values):
        return None
    if not all(values):
        raise ValueError("Configure OIDC_ISSUER, OIDC_CLIENT_ID and OIDC_CLIENT_SECRET together.")
    if not issuer.startswith("https://"):
        raise ValueError("OIDC_ISSUER must use HTTPS.")
    return {"issuer": issuer, "client_id": client_id, "client_secret": client_secret}
