"""In-memory usage metrics.

Beyond ops visibility, error-type counts turn the simulator into a
developer-experience instrument: which mistakes do integrators actually
make against this API? (`/_lab/metrics` to read, POST `/_lab/metrics/reset`
to zero.) Not persisted across restarts.
"""
import time
from collections import Counter

from .state import now_iso


class Metrics:
    def __init__(self):
        self.reset()

    def reset(self):
        self.started_at = now_iso()
        self._t0 = time.time()
        self.requests = Counter()   # (method, route, status) -> count
        self.errors = Counter()     # (status, route, message) -> count

    def record_request(self, method: str, route: str, status: int):
        self.requests[(method, route, status)] += 1

    def record_error(self, status: int, route: str, message: str):
        self.errors[(status, route, message[:120])] += 1

    def snapshot(self) -> dict:
        total, ok, errored = 0, 0, 0
        by_route, by_status = Counter(), Counter()
        for (method, route, status), n in self.requests.items():
            total += n
            by_route[f"{method} {route}"] += n
            by_status[str(status)] += n
            if status < 400:
                ok += n
            else:
                errored += n
        return {
            "since": self.started_at,
            "uptimeSeconds": round(time.time() - self._t0, 1),
            "totalRequests": total,
            "successful": ok,
            "errored": errored,
            "byStatus": dict(sorted(by_status.items())),
            "byRoute": [{"route": r, "count": n} for r, n in by_route.most_common()],
            "topErrors": [
                {"status": s, "route": r, "error": msg, "count": n}
                for (s, r, msg), n in self.errors.most_common(15)
            ],
        }


metrics = Metrics()
