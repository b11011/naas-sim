"""Run the simulator: python -m simulator"""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "simulator.main:app",
        host=os.getenv("NAAS_SIM_HOST", "127.0.0.1"),
        port=int(os.getenv("NAAS_SIM_PORT", "8080")),
    )
