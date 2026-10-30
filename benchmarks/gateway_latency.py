"""Gateway benchmark: /v1 offset pagination vs /v2 cursor pagination at the same depth.

Starts its own gateway process (DEV_MODE, bench port) against the live Mongo, then for each
scenario fires `--requests` GETs at fixed `--concurrency` after a warm-up, and reports
p50/p95/p99 latency and req/s. Page size 50 for both versions; depth = rows skipped.
  v1 page N  -> find().skip((N-1)*50).limit(50) + count_documents()  (cost grows with N)
  v2 cursor  -> keyset {$or: [ts < t, (ts == t, _id < id)]} on the (timestamp, _id) index

Usage:  MONGO_URI=... JWT_SECRET=... uv run python -u benchmarks/gateway_latency.py
Writes: benchmarks/results/gateway_latency.{json,md}
"""

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

RESULTS = Path(__file__).parent / "results"
PAGE = 50


def pct(values: list[float], p: int) -> float:
    return round(statistics.quantiles(values, n=100)[p - 1], 2)


async def load(
    client: httpx.AsyncClient, url: str, params: dict[str, Any], n: int, conc: int
) -> dict[str, Any]:
    latencies: list[float] = []
    sem = asyncio.Semaphore(conc)

    async def one() -> None:
        async with sem:
            t0 = time.perf_counter()
            r = await client.get(url, params=params)
            r.raise_for_status()
            latencies.append((time.perf_counter() - t0) * 1000)

    await asyncio.gather(*(one() for _ in range(max(20, n // 10))))  # warm-up, discarded
    latencies.clear()
    t0 = time.perf_counter()
    await asyncio.gather(*(one() for _ in range(n)))
    wall = time.perf_counter() - t0
    return {
        "p50_ms": pct(latencies, 50),
        "p95_ms": pct(latencies, 95),
        "p99_ms": pct(latencies, 99),
        "req_per_s": round(n / wall, 1),
    }


async def cursor_at(client: httpx.AsyncClient, pages: int) -> str | None:
    """Walk v2 pages once to get the cursor that starts at row pages*PAGE."""
    cursor = None
    for _ in range(pages):
        params = {"limit": PAGE} | ({"cursor": cursor} if cursor else {})
        cursor = (await client.get("/v2/transactions", params=params)).json()["next_cursor"]
    return cursor


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8102)
    ap.add_argument("--requests", type=int, default=400)
    ap.add_argument("--concurrency", type=int, default=16)
    args = ap.parse_args()

    env = os.environ | {
        "PORT": str(args.port),
        "DEV_MODE": "true",
        "LOG_LEVEL": "WARNING",
        "KAFKA_BOOTSTRAP": os.environ.get("KAFKA_BOOTSTRAP", "localhost:9092"),
    }
    server = subprocess.Popen(["uv", "run", "gateway"], env=env)  # noqa: ASYNC220 (start-up only)
    base = f"http://localhost:{args.port}"
    try:
        async with httpx.AsyncClient(base_url=base, timeout=30) as client:
            for _ in range(60):
                try:
                    if (await client.get("/health/ready")).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(1)
            token = (await client.post("/dev/token", json={"sub": "bench", "role": "analyst"})).json()
            client.headers["authorization"] = f"Bearer {token['access_token']}"
            total = (await client.get("/v1/transactions", params={"page_size": 1})).json()["total"]
            print(f"live transactions: {total}")

            results = []
            for page in (1, 50, 200):
                depth = (page - 1) * PAGE
                if depth + PAGE > total:
                    continue
                v1 = await load(
                    client,
                    "/v1/transactions",
                    {"page": page, "page_size": PAGE},
                    args.requests,
                    args.concurrency,
                )
                cursor = await cursor_at(client, page - 1)
                v2 = await load(
                    client,
                    "/v2/transactions",
                    {"limit": PAGE} | ({"cursor": cursor} if cursor else {}),
                    args.requests,
                    args.concurrency,
                )
                for version, r in (("v1 offset", v1), ("v2 cursor", v2)):
                    row = {"version": version, "depth_rows": depth} | r
                    results.append(row)
                    print(json.dumps(row))
    finally:
        server.terminate()
        server.wait(timeout=15)

    out = {
        "run_at": datetime.now(UTC).isoformat(),
        "live_transactions": total,
        "page_size": PAGE,
        "requests_per_scenario": args.requests,
        "concurrency": args.concurrency,
        "results": results,
        "setup": "one local gateway process (uvicorn, 1 worker) + compose MongoDB 8.0.34 in Docker Desktop "
        "(8 GB), load generator on the same Apple Silicon 10 CPU / 16 GB laptop",
    }
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "gateway_latency.json").write_text(json.dumps(out, indent=2))
    lines = [
        f"# Gateway pagination latency — {out['run_at']}",
        "",
        f"Command: `make bench-gateway`. {total} live transactions, page size {PAGE}, "
        f"{args.requests} requests per row at concurrency {args.concurrency}. Setup: {out['setup']}.",
        "",
        "| version | depth (rows skipped) | p50 ms | p95 ms | p99 ms | req/s |",
        "|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {r['version']} | {r['depth_rows']} | {r['p50_ms']} | {r['p95_ms']} | {r['p99_ms']} | "
        f"{r['req_per_s']} |"
        for r in results
    ]
    (RESULTS / "gateway_latency.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
