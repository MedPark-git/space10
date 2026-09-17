from urllib.parse import parse_qs, urlparse

from flask import request, url_for

from app import app, db
from dashboard_feature_v2 import register_dashboard_context

register_dashboard_context(app, db)


@app.after_request
def redirect_stock_commit_to_dashboard(response):
    """After an ERP stock snapshot is committed, land on the visual dashboard.

    Other reference-data commits keep their existing redirect behavior.
    The stock commit redirect is identifiable by the snapshot query parameter.
    """
    if request.endpoint == "expiry.commit" and response.status_code in {301, 302, 303, 307, 308}:
        location = response.headers.get("Location", "")
        if location:
            parsed = urlparse(location)
            snapshot = parse_qs(parsed.query).get("snapshot", [""])[0]
            if snapshot:
                response.headers["Location"] = url_for("expiry.dashboard", snapshot=snapshot)
    return response
