"""Lumen NaaS simulator — FastAPI app assembly.

Error responses follow the Lumen shape ({"code": ..., "message": ...}) and
request-validation failures return 400 (not FastAPI's default 422), matching
the real platform.
"""
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .auth import router as auth_router
from .ethernet_on_demand import router as eod_router
from .internet_on_demand import router as iod_router
from .lab import router as lab_router

app = FastAPI(
    title="Lumen NaaS Simulator",
    version="0.1.0",
    description=(
        "Lab simulator for Lumen's NaaS APIs: Ethernet On-Demand v5 "
        "(/Network/v5/DynamicConnection) and Internet On-Demand "
        "(qualify → quote → order). Get a token from POST /oauth/v2/token "
        "with grant_type=client_credentials (default creds: "
        "naas-lab-client / naas-lab-secret)."
    ),
)


@app.exception_handler(StarletteHTTPException)
async def lumen_style_http_error(request: Request, exc: StarletteHTTPException):
    return JSONResponse(status_code=exc.status_code,
                        content={"code": exc.status_code, "message": str(exc.detail)})


@app.exception_handler(RequestValidationError)
async def lumen_style_validation_error(request: Request, exc: RequestValidationError):
    errors = [{"field": ".".join(str(part) for part in e["loc"]), "message": e["msg"]}
              for e in exc.errors()]
    return JSONResponse(status_code=400,
                        content={"code": 400, "message": "Bad Request", "errors": errors})


@app.get("/", tags=["Info"])
async def root():
    return {
        "name": "Lumen NaaS Simulator",
        "docs": "/docs",
        "token": "POST /oauth/v2/token (grant_type=client_credentials)",
        "ethernetOnDemand": "/Network/v5/DynamicConnection",
        "internetOnDemand": ["/Product/v1/price", "/Product/v1/priceRequest",
                             "/Customer/v3/Ordering/orderRequest"],
        "lab": ["/_lab/state", "/_lab/events", "/_lab/reset"],
    }


app.include_router(auth_router)
app.include_router(eod_router)
app.include_router(iod_router)
app.include_router(lab_router)
