"""HTTP client for the risk and agent services: timeouts, retries with backoff, error mapping."""

import asyncio
import random
from typing import Any

import httpx

from gateway.errors import ApiError
from ledgerlens_common.logging import REQUEST_ID_HEADER, request_id_var


class Upstream:
    def __init__(
        self, name: str, client: httpx.AsyncClient, retries: int = 3, backoff_s: float = 0.2
    ) -> None:
        self.name, self.client, self.retries, self.backoff_s = name, client, retries, backoff_s

    async def request(
        self, method: str, path: str, *, json: Any = None, params: dict[str, Any] | None = None
    ) -> Any:
        # GETs are idempotent, so any transient failure may be retried. Other methods are
        # retried only when the request provably never reached the server (connect failure);
        # a timed-out POST may already have run, so retrying it could e.g. double-write the audit log.
        idempotent = method == "GET"
        rid = request_id_var.get()
        headers = {REQUEST_ID_HEADER: rid} if rid else {}
        for attempt in range(self.retries + 1):
            retryable = True
            try:
                resp = await self.client.request(method, path, json=json, params=params, headers=headers)
            except (httpx.ConnectError, httpx.ConnectTimeout):  # before TimeoutException: it's a subclass
                err = ApiError(503, "upstream_unavailable", f"{self.name} service is unavailable")
            except httpx.TimeoutException:
                err = ApiError(504, "upstream_timeout", f"{self.name} service timed out")
                retryable = idempotent
            except httpx.TransportError:
                err = ApiError(502, "upstream_error", f"{self.name} service connection failed")
                retryable = idempotent
            else:
                if resp.status_code < 400:
                    return resp.json()
                if resp.status_code < 500:
                    # Client errors are not retried; pass the upstream's own error code through.
                    body = _json_or_empty(resp)
                    raise ApiError(
                        resp.status_code,
                        body.get("code", "upstream_rejected"),
                        body.get("message", f"{self.name} rejected the request"),
                    )
                err = ApiError(502, "upstream_error", f"{self.name} returned {resp.status_code}")
                retryable = idempotent
            if not retryable or attempt == self.retries:
                raise err
            # Exponential backoff with jitter, so retrying replicas don't stampede in lockstep.
            await asyncio.sleep(self.backoff_s * 2**attempt * random.uniform(0.5, 1.5))  # noqa: S311
        raise AssertionError("unreachable")


def _json_or_empty(resp: httpx.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
