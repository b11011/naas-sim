"""Pytest bootstrap: speed up async transitions and shrink the daily order
quota so tests can exercise it. Must run before simulator.config is imported."""
import os

os.environ.setdefault("NAAS_SIM_DELAY_SECONDS", "0.2")
os.environ.setdefault("NAAS_SIM_DAILY_ORDER_LIMIT", "5")
