"""Golden-question evaluation harness.

Fixtures are JSONL: one object per line with
    q       the question to search
    expect  list of substrings; a result whose PATH contains any of them
            counts as a hit (paths only — matching on section titles proved
            too lenient: a decoy document sharing a section-title substring
            would count, inflating the numbers)

Reports hit@1, hit@k, and MRR. These numbers are the project's honesty
mechanism: retrieval changes (chunking tweaks, hybrid vectors in phase 2)
must show their before/after here before they count as improvements.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .search import search


@dataclass
class EvalResult:
    total: int = 0
    hit_at_1: int = 0
    hit_at_k: int = 0
    mrr_sum: float = 0.0
    k: int = 5
    misses: list[str] = field(default_factory=list)
    # One row per fixture: {"q", "rank" (0 = miss), "top_path"} — consumed by
    # the quality report so it never has to re-run the searches.
    per_question: list[dict] = field(default_factory=list)

    @property
    def mrr(self) -> float:
        return self.mrr_sum / self.total if self.total else 0.0

    def summary(self) -> str:
        if not self.total:
            return "no fixtures"
        lines = [
            f"questions: {self.total}",
            f"hit@1:     {self.hit_at_1}/{self.total} ({self.hit_at_1 / self.total:.0%})",
            f"hit@{self.k}:     {self.hit_at_k}/{self.total} ({self.hit_at_k / self.total:.0%})",
            f"MRR:       {self.mrr:.3f}",
        ]
        for m in self.misses:
            lines.append(f"MISS:      {m}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "questions": self.total,
            "hit_at_1": self.hit_at_1,
            "hit_at_k": self.hit_at_k,
            "k": self.k,
            "mrr": round(self.mrr, 3),
            "misses": self.misses,
        }


def load_fixtures(path: str | Path) -> list[dict]:
    fixtures = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        obj = json.loads(line)
        if "q" not in obj or "expect" not in obj:
            raise ValueError(f"fixture line {n}: needs 'q' and 'expect'")
        if isinstance(obj["expect"], str):
            obj["expect"] = [obj["expect"]]
        fixtures.append(obj)
    return fixtures


def _matches(hit, expects: list[str]) -> bool:
    path = hit.path.lower()
    return any(e.lower() in path for e in expects)


def run_eval(
    db_path: str | Path,
    fixtures_path: str | Path,
    k: int = 5,
    collection: str | None = None,
    retrieve_fn=None,
) -> EvalResult:
    """Score the golden fixtures. `retrieve_fn(db_path, q, k, collection)`
    defaults to BM25 search; pass the hybrid retriever to score it on the
    exact same questions — that head-to-head is what gates phase 2."""
    if retrieve_fn is None:
        def retrieve_fn(db_path, q, k, collection):
            return search(db_path, q, k=k, collection=collection)
    result = EvalResult(k=k)
    for fx in load_fixtures(fixtures_path):
        result.total += 1
        hits = retrieve_fn(db_path, fx["q"], k, collection)
        rank = next(
            (i for i, h in enumerate(hits, start=1) if _matches(h, fx["expect"])), 0
        )
        result.per_question.append(
            {"q": fx["q"], "rank": rank, "top_path": hits[0].path if hits else ""}
        )
        if rank == 1:
            result.hit_at_1 += 1
        if rank:
            result.hit_at_k += 1
            result.mrr_sum += 1.0 / rank
        else:
            result.misses.append(fx["q"])
    return result
