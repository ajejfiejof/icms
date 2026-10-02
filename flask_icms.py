"""Flask-iCMS: Decentralized, Constant-Memory Rate Limiting for Flask.

Provides zero-dependency, peer-to-peer distributed rate limiting for Flask
applications without requiring centralized Redis or database infrastructure.

Based on the Idempotent Count-Min Lattice (iCMS).
Copyright (c) 2026. Licensed under AGPLv3.
"""

from __future__ import annotations

import time
from functools import wraps
from typing import Callable, Optional
from flask import Flask, jsonify, make_response, request, Response

from icms import ICMS, EpochICMS


class FlaskICMS:
    """Flask extension for decentralized, idempotent rate limiting."""

    def __init__(
        self,
        app: Optional[Flask] = None,
        default_limit: int = 10,
        window_seconds: int = 60,
        worker_id: int = 0,
        w: int = 128,
        d: int = 4,
        p: int = 6,
        seed: int = 42,
    ):
        self.default_limit = default_limit
        self.window_seconds = window_seconds
        self.worker_id = worker_id
        self.w = w
        self.d = d
        self.p = p
        self.seed = seed
        self._seq = 0
        self.incarnation_id = int(time.time_ns()) & 0xFFFFFFFFFFFFFFFF

        self.epoch_sketch = EpochICMS(
            window_seconds=float(window_seconds),
            w=w,
            d=d,
            p=p,
            seed=seed,
        )

        if app is not None:
            self.init_app(app)

    def init_app(self, app: Flask) -> None:
        """Initialize Flask application."""
        app.extensions = getattr(app, "extensions", {})
        app.extensions["icms_limiter"] = self

    def _get_client_key(self) -> str:
        """Extract client identifier: API key, Idempotency-Key, or IP address."""
        if "X-API-Key" in request.headers:
            return f"apikey:{request.headers['X-API-Key']}"
        remote = request.remote_addr or "127.0.0.1"
        return f"ip:{remote}"

    def limit(self, limit: Optional[int] = None, key_func: Optional[Callable[[], str]] = None):
        """Decorator to rate-limit a Flask view function."""
        eff_limit = limit if limit is not None else self.default_limit

        def decorator(f):
            @wraps(f)
            def decorated_function(*args, **kwargs):
                key = key_func() if key_func is not None else self._get_client_key()
                client_idempotency_token = request.headers.get("Idempotency-Key")

                endpoint_key = f"{request.endpoint or 'endpoint'}:{key.lower()}"

                if client_idempotency_token:
                    # Client-identified event: globally deterministic token across all workers.
                    # Hashed completely via Blake2b (no 8-byte prefix truncation).
                    self.epoch_sketch.add(
                        endpoint_key,
                        idempotency_key=client_idempotency_token,
                    )
                else:
                    # Server-sequenced event: monotonic counter scoped to worker and incarnation
                    self._seq += 1
                    self.epoch_sketch.add(
                        endpoint_key,
                        host_id=self.worker_id,
                        seq=self._seq,
                        incarnation_id=self.incarnation_id,
                    )

                current_count = self.epoch_sketch.query(endpoint_key, method="min")

                if int(round(current_count)) > eff_limit:
                    resp = jsonify({
                        "error": "Too Many Requests",
                        "message": f"Rate limit of {eff_limit} requests per {self.window_seconds}s exceeded.",
                        "current_estimate": round(current_count, 1),
                        "limit": eff_limit,
                    })
                    resp.status_code = 429
                    resp.headers["Retry-After"] = str(self.window_seconds)
                    resp.headers["X-RateLimit-Limit"] = str(eff_limit)
                    resp.headers["X-RateLimit-Remaining"] = "0"
                    return resp

                response = make_response(f(*args, **kwargs))
                remaining = max(0, eff_limit - int(round(current_count)))
                response.headers["X-RateLimit-Limit"] = str(eff_limit)
                response.headers["X-RateLimit-Remaining"] = str(remaining)
                return response

            return decorated_function

        return decorator

    def check_rate_limit(
        self,
        key: str,
        limit: Optional[int] = None,
        idempotency_key: Optional[str] = None,
    ) -> tuple[bool, float, int]:
        """Programmatically check rate limit for a key outside an HTTP request context.

        Returns:
            (is_blocked, current_estimate, remaining)
        """
        eff_limit = limit if limit is not None else self.default_limit
        if idempotency_key is not None:
            self.epoch_sketch.add(
                key,
                idempotency_key=idempotency_key,
            )
        else:
            self._seq += 1
            self.epoch_sketch.add(
                key,
                host_id=self.worker_id,
                seq=self._seq,
                incarnation_id=self.incarnation_id,
            )
        current_count = self.epoch_sketch.query(key, method="min")
        is_blocked = int(round(current_count)) > eff_limit
        remaining = max(0, eff_limit - int(round(current_count)))
        return is_blocked, current_count, remaining


    def sync_gossip_from(self, other: FlaskICMS) -> bool:
        """Simulate P2P gossip sync between Flask workers."""
        return self.epoch_sketch.merge_gossip(
            other.epoch_sketch.curr_sketch,
            other.epoch_sketch.current_epoch,
        )

    def size_bytes(self) -> int:
        """Memory footprint in bytes."""
        return self.epoch_sketch.curr_sketch.size_bytes() * 2

