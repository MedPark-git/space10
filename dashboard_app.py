from app import app, db
from dashboard_feature import register_dashboard_context

register_dashboard_context(app, db)
