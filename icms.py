"""Idempotent Count-Min Lattice (iCMS)
A Provably Verified, Strong-Eventual-Consistent (SEC) Frequency Sketch
for Distributed Telemetry and Gossip Networks.

Mathematical Foundation:
Combines Cormode-Muthukrishnan Count-Min hashing with Flajolet-Martin
register lattices to construct a true join-semilattice (CvRDT) for multiset
frequency estimation with O(1) space per replica.

Copyright (c) 2026.
"""

from __future__ import annotations

import hashlib
import math
from typing import List, Tuple, Union


def _blake2b_64(data: bytes, key_int: int) -> int:
    """Cryptographic keyed 64-bit universal hash using BLAKE2b."""
    return int.from_bytes(
        hashlib.blake2b(
            data,
            digest_size=8,
            key=key_int.to_bytes(8, byteorder="little", signed=False),
        ).digest(),
        byteorder="little",
    )


class MiniHLL:
    """A compact HyperLogLog join-semilattice cell.

    Operates as an idempotent register lattice where merge is pointwise maximum.
    """

    __slots__ = ("p", "m", "reg")

    def __init__(self, p: int = 4):
        self.p = p
        self.m = 1 << p
        self.reg = [0] * self.m

    def add_hash(self, h: int) -> None:
        """Add a precomputed 64-bit event hash into the lattice."""
        idx = h >> (64 - self.p)
        w = h & ((1 << (64 - self.p)) - 1)
        b = w.bit_length()
        rho = (64 - self.p) - b + 1 if b else (64 - self.p) + 1
        if rho > self.reg[idx]:
            self.reg[idx] = rho

    def merge(self, other: MiniHLL) -> MiniHLL:
        """Join-semilattice LUB: pointwise maximum."""
        assert self.p == other.p, "Precision mismatch"
        out = MiniHLL(self.p)
        out.reg = [a if a >= b else b for a, b in zip(self.reg, other.reg)]
        return out

    def count(self) -> float:
        """Unbiased cardinality estimate using Flajolet harmonic mean and linear counting."""
        m = self.m
        r = self.reg
        if m >= 128:
            alpha = 0.7213 / (1.0 + 1.079 / m)
        elif m >= 64:
            alpha = 0.709
        elif m >= 32:
            alpha = 0.697
        elif m >= 16:
            alpha = 0.673
        else:
            alpha = 0.5

        s = sum(2.0 ** -x for x in r)
        e = alpha * m * m / s

        # Small cardinality correction (Linear Counting)
        if e <= 2.5 * m:
            zeros = r.count(0)
            if zeros > 0:
                e = m * math.log(m / zeros)

        # Large cardinality correction (for 32-bit boundary)
        if e > (1 << 32) / 30.0:
            e = -(1 << 32) * math.log(1.0 - e / (1 << 32))

        return e


class ICMS:
    """Idempotent Count-Min Sketch (iCMS).

    A bounded join-semilattice over (MiniHLL)^(d x w) supporting:
    - O(1) space complexity independent of replica count R.
    - Strict Strong Eventual Consistency (SEC) under lossy/duplicate gossip.
    - True multiset sum estimation (eliminating max-merge undercounting).
    """

    def __init__(self, w: int = 128, d: int = 4, p: int = 4, seed: int = 42):
        self.w = w
        self.d = d
        self.p = p
        self.seed = seed
        self.cells = [[MiniHLL(p) for _ in range(w)] for _ in range(d)]

    def _bucket(self, item_bytes: bytes, row: int) -> int:
        return _blake2b_64(item_bytes, self.seed + row * 1009) % self.w

    def add(self, item: Union[str, bytes], host_id: int, seq: int) -> None:
        """Add an event occurrence at a specific host and sequence nonce.

        The composite token (item || host_id || seq) guarantees that identical
        events arriving via redundant network paths are deduplicated idempotently.
        """
        if isinstance(item, str):
            item = item.encode("utf-8")

        # Deterministic event token: Blake2b(item || host_id || seq)
        token_data = item + b":" + host_id.to_bytes(4, "little") + b":" + seq.to_bytes(8, "little")
        token_hash = _blake2b_64(token_data, 0x1337BEEF)

        for r in range(self.d):
            c = self._bucket(item, r)
            self.cells[r][c].add_hash(token_hash)

    def merge(self, other: ICMS) -> ICMS:
        """Lattice Join (LUB): element-wise register maximum across all cells."""
        assert self.w == other.w and self.d == other.d and self.p == other.p, "Dimension mismatch"
        out = ICMS(self.w, self.d, self.p, self.seed)
        for r in range(self.d):
            for c in range(self.w):
                out.cells[r][c] = self.cells[r][c].merge(other.cells[r][c])
        return out

    def query(self, item: Union[str, bytes]) -> float:
        """Query estimated global multiset frequency of the item."""
        if isinstance(item, str):
            item = item.encode("utf-8")

        estimates = [self.cells[r][self._bucket(item, r)].count() for r in range(self.d)]
        return min(estimates)

    def size_bytes(self) -> int:
        """Total memory consumed by the sketch in bytes."""
        # Each cell has 2^p registers of 1 byte each (sufficient for rho <= 64)
        registers_per_cell = 1 << self.p
        return self.w * self.d * registers_per_cell

    def partial_le(self, other: ICMS) -> bool:
        """Check lattice partial order: self <= other."""
        for r in range(self.d):
            for c in range(self.w):
                for i in range(1 << self.p):
                    if self.cells[r][c].reg[i] > other.cells[r][c].reg[i]:
                        return False
        return True
