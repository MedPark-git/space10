from urllib.parse import parse_qs, urlparse

from flask import redirect, render_template, request, url_for

from app import app, db
from dashboard_runtime import build_dashboard_data


@app.get("/dashboard/overview")
def visual_dashboard():
    from flask_login import current_user
    if not current_user.is_authenticated:
        return redirect(url_for("login", next=request.full_path.rstrip("?")))
    dashboard2 = build_dashboard_data(app, db)
    return render_template("expiry_dashboard.html", dashboard2=dashboard2)


@app.before_request
def use_visual_dashboard():
    if request.endpoint == "expiry.dashboard":
        args = request.args.to_dict(flat=True)
        return redirect(url_for("visual_dashboard", **args))


@app.after_request
def redirect_stock_commit_to_dashboard(response):
    if request.endpoint == "expiry.commit" and response.status_code in {301, 302, 303, 307, 308}:
        location = response.headers.get("Location", "")
        if location:
            parsed = urlparse(location)
            snapshot = parse_qs(parsed.query).get("snapshot", [""])[0]
            if snapshot:
                response.headers["Location"] = url_for("visual_dashboard", snapshot=snapshot)
    return response
