"""
publishing/graph.py — thin Instagram Graph client.

* The HTTP layer is injected (`http.get/post(url, params|data, timeout)`), so
  everything is testable offline.
* Every failure becomes a GraphError that says whether retrying makes sense.
"""
import logging

logger = logging.getLogger(__name__)

API_HOST = "https://graph.instagram.com"

# Meta error codes (documented): 190 token invalid/expired, 102 session,
# 4/17/32/613 rate limits, 1/2 temporary server errors, 9007 media not ready.
AUTH_CODES = {190, 102, 10, 200}
RATE_CODES = {4, 17, 32, 613}
TRANSIENT_CODES = {1, 2, 9007} | RATE_CODES


class GraphError(Exception):
    def __init__(self, message, code=None, subcode=None, http_status=None,
                 transient=False, auth=False):
        super().__init__(message)
        self.code = code
        self.subcode = subcode
        self.http_status = http_status
        self.transient = transient
        self.auth = auth

    def summary(self):
        return {"message": str(self), "code": self.code,
                "subcode": self.subcode, "http_status": self.http_status,
                "transient": self.transient, "auth": self.auth}


def _classify(status, body):
    err = (body or {}).get("error") if isinstance(body, dict) else None
    if not err:
        return GraphError(f"HTTP {status} from Instagram", http_status=status,
                          transient=status >= 500 or status == 429)
    code, sub = err.get("code"), err.get("error_subcode")
    auth = code in AUTH_CODES or status == 401
    transient = (not auth) and (code in TRANSIENT_CODES or status >= 500
                                or status == 429 or bool(err.get("is_transient")))
    return GraphError(err.get("message", "Instagram API error"), code=code,
                      subcode=sub, http_status=status, transient=transient,
                      auth=auth)


class GraphClient:
    def __init__(self, http, version="v26.0", host=API_HOST, timeout=30):
        self.http, self.version, self.host, self.timeout = http, version, host, timeout

    def _url(self, path):
        return f"{self.host}/{self.version}/{path.lstrip('/')}"

    def _call(self, method, url, params):
        try:
            if method == "GET":
                resp = self.http.get(url, params=params, timeout=self.timeout)
            else:
                resp = self.http.post(url, data=params, timeout=self.timeout)
        except Exception as exc:  # network down, timeout, DNS ...
            raise GraphError(f"Network error: {exc.__class__.__name__}",
                             transient=True) from None
        try:
            body = resp.json()
        except Exception:
            body = None
        if resp.status_code >= 400 or (isinstance(body, dict) and "error" in body):
            raise _classify(resp.status_code, body)
        return body or {}

    # ---- account ---------------------------------------------------------
    def me(self, token):
        return self._call("GET", self._url("me"), {
            "fields": "user_id,username,account_type", "access_token": token})

    def publishing_limit(self, ig_id, token):
        body = self._call("GET", self._url(f"{ig_id}/content_publishing_limit"), {
            "fields": "quota_usage,config", "access_token": token})
        row = (body.get("data") or [{}])[0]
        total = (row.get("config") or {}).get("quota_total")
        return {"usage": row.get("quota_usage", 0), "total": total}

    # ---- comments and owned media (requires Instagram manage-comments scope) ----
    def list_media(self, ig_id, token, limit=10):
        body = self._call("GET", self._url(f"{ig_id}/media"), {
            "fields": "id,caption,permalink,timestamp",
            "limit": max(1, min(int(limit), 25)), "access_token": token})
        return body.get("data") or []

    def list_comments(self, media_id, token, limit=50):
        body = self._call("GET", self._url(f"{media_id}/comments"), {
            "fields": "id,text,timestamp,username",
            "limit": max(1, min(int(limit), 100)), "access_token": token})
        return body.get("data") or []

    def create_comment(self, media_id, message, token):
        text = str(message or "").strip()
        if not text or len(text) > 2000:
            raise ValueError("Comment must contain 1–2000 characters.")
        return self._call("POST", self._url(f"{media_id}/comments"), {
            "message": text, "access_token": token})

    def reply_to_comment(self, comment_id, message, token):
        text = str(message or "").strip()
        if not text or len(text) > 2000:
            raise ValueError("Reply must contain 1–2000 characters.")
        return self._call("POST", self._url(f"{comment_id}/replies"), {
            "message": text, "access_token": token})

    # ---- containers ------------------------------------------------------
    def create_container(self, ig_id, token, *, image_url=None, caption=None,
                         media_type=None, children=None, carousel_item=False,
                         video_url=None, share_to_feed=None):
        params = {"access_token": token}
        if image_url:
            params["image_url"] = image_url
        if video_url:
            params["video_url"] = video_url
        if share_to_feed is not None:
            params["share_to_feed"] = "true" if share_to_feed else "false"
        if caption:
            params["caption"] = caption
        if media_type:
            params["media_type"] = media_type
        if children:
            params["children"] = ",".join(children)
        if carousel_item:
            params["is_carousel_item"] = "true"
        return self._call("POST", self._url(f"{ig_id}/media"), params)["id"]

    def container_status(self, container_id, token):
        body = self._call("GET", self._url(container_id), {
            "fields": "status_code,status", "access_token": token})
        return body.get("status_code"), body.get("status")

    def publish_container(self, ig_id, token, container_id):
        return self._call("POST", self._url(f"{ig_id}/media_publish"), {
            "creation_id": container_id, "access_token": token})["id"]

    def media_info(self, media_id, token):
        return self._call("GET", self._url(media_id), {
            "fields": "permalink,media_type,timestamp", "access_token": token})
