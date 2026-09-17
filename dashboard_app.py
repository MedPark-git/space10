from app import app, db
from dashboard_feature_v2 import register_dashboard_context

register_dashboard_context(app, db)
