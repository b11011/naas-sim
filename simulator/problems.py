"""RFC 7807 problem+json errors — the error model of the new-generation APIs.

Multi-Cloud Gateway and Ethernet Fabric Connect return `application/problem+json`
(the spec's LpdpProblem schema) instead of the legacy `{code, message}` envelope
used by Ethernet/Internet On-Demand. Raise ProblemException inside /mcgw and
/fabric routes; the handler in main.py renders it faithfully.
"""
from fastapi.responses import JSONResponse

PROBLEM_CONTENT_TYPE = "application/problem+json"

# Paths served by new-generation APIs (problem+json error model)
NEW_API_PREFIXES = ("/mcgw/", "/fabric/")


class ProblemException(Exception):
    def __init__(self, status: int, title: str, detail: str = "", type_: str = "about:blank"):
        self.status, self.title, self.detail, self.type = status, title, detail, type_


def problem_response(status: int, title: str, detail: str = "", type_: str = "about:blank") -> JSONResponse:
    body = {"type": type_, "title": title, "status": status}
    if detail:
        body["detail"] = detail
    return JSONResponse(status_code=status, content=body, media_type=PROBLEM_CONTENT_TYPE)


def is_new_api(path: str) -> bool:
    return path.startswith(NEW_API_PREFIXES)


def paginate(items: list, limit: int = 25, offset: int = 0) -> dict:
    """The new APIs' list envelope: {pagination, data}."""
    return {
        "pagination": {"limit": limit, "offset": offset, "total": len(items)},
        "data": items[offset:offset + limit],
    }
