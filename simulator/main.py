"""Lumen NaaS simulator — FastAPI app assembly.

Error responses follow the Lumen shape ({"code": ..., "message": ...}) and
request-validation failures return 400 (not FastAPI's default 422), matching
the real platform.
"""
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import config
from .auth import router as auth_router
from .ethernet_on_demand import router as eod_router
from .internet_on_demand import router as iod_router
from .lab import router as lab_router
from .metrics import metrics
from .state import store

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


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", request.url.path)


@app.middleware("http")
async def observe_and_persist(request: Request, call_next):
    response = await call_next(request)
    path = _route_template(request)
    if not path.startswith("/_lab"):
        metrics.record_request(request.method, path, response.status_code)
    # Persist after successful mutations (opt-in via NAAS_SIM_STATE_FILE)
    if config.STATE_FILE and request.method in ("POST", "PATCH", "DELETE") and response.status_code < 400:
        store.save()
    return response


@app.exception_handler(StarletteHTTPException)
async def lumen_style_http_error(request: Request, exc: StarletteHTTPException):
    metrics.record_error(exc.status_code, _route_template(request), str(exc.detail))
    return JSONResponse(status_code=exc.status_code,
                        content={"code": exc.status_code, "message": str(exc.detail)})


@app.exception_handler(RequestValidationError)
async def lumen_style_validation_error(request: Request, exc: RequestValidationError):
    errors = [{"field": ".".join(str(part) for part in e["loc"]), "message": e["msg"]}
              for e in exc.errors()]
    summary = "; ".join(f"{e['field']}: {e['message']}" for e in errors)
    metrics.record_error(400, _route_template(request), f"validation — {summary}")
    return JSONResponse(status_code=400,
                        content={"code": 400, "message": "Bad Request", "errors": errors})


@app.on_event("startup")
async def load_seed_profile():
    """NAAS_SIM_SEED_FILE: catalog profile applied at startup — unless a
    persisted snapshot was restored (running state wins over seeds)."""
    if config.SEED_FILE and not store.loaded_from_snapshot:
        import json
        with open(config.SEED_FILE) as f:
            store.apply_seed(json.load(f))


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

# The Lumen-style error envelope actually returned by the exception handlers
_API_ERROR_SCHEMA = {
    "title": "APIError",
    "type": "object",
    "properties": {
        "code": {"type": "integer", "example": 400},
        "message": {"type": "string", "example": "Bad Request"},
        "errors": {
            "type": "array",
            "description": "Present on request-validation failures",
            "items": {
                "type": "object",
                "properties": {"field": {"type": "string"}, "message": {"type": "string"}},
            },
        },
    },
}


def _lumen_openapi():
    """Replace FastAPI's advertised 422 with the 400 the API actually returns.

    The RequestValidationError handler above converts every validation failure
    to a Lumen-style 400 at runtime; schema generation doesn't know about
    exception handlers, so without this the docs would promise a 422 shape
    that no request can ever receive.
    """
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version,
                         description=app.description, routes=app.routes)
    schema.setdefault("components", {}).setdefault("schemas", {})["APIError"] = _API_ERROR_SCHEMA
    error_response = {
        "description": "Bad Request",
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/APIError"}}},
    }
    for path_item in schema["paths"].values():
        for operation in path_item.values():
            responses = operation.get("responses", {})
            if "422" in responses:
                del responses["422"]
                responses["400"] = error_response
    schema["components"]["schemas"].pop("HTTPValidationError", None)
    schema["components"]["schemas"].pop("ValidationError", None)
    app.openapi_schema = schema
    return schema


app.openapi = _lumen_openapi
