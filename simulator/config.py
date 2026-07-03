"""Simulator configuration, all overridable via environment variables."""
import os

CLIENT_ID = os.getenv("NAAS_SIM_CLIENT_ID", "naas-lab-client")
CLIENT_SECRET = os.getenv("NAAS_SIM_CLIENT_SECRET", "naas-lab-secret")
TOKEN_TTL_SECONDS = int(os.getenv("NAAS_SIM_TOKEN_TTL_SECONDS", "3600"))

# How long async operations (create/modify/delete) stay in their transitional
# state before completing. The real platform takes ~minutes; default 10s.
TRANSITION_DELAY_SECONDS = float(os.getenv("NAAS_SIM_DELAY_SECONDS", "10"))

# Internet On-Demand quotes are valid for 15 minutes on the real platform.
QUOTE_TTL_SECONDS = float(os.getenv("NAAS_SIM_QUOTE_TTL_SECONDS", str(15 * 60)))

# Real platform quota: max 24 change requests per day (resets at GMT midnight).
DAILY_ORDER_LIMIT = int(os.getenv("NAAS_SIM_DAILY_ORDER_LIMIT", "24"))
