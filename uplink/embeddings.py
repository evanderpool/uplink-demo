"""Pluggable text embedders for the phase-2 hybrid retriever.

The embedding backend follows the trust boundary, not a single technology:

  local   an on-device model (fastembed) — nothing leaves the machine, the
          right choice for confidential corpora and the private edition.
  voyage  Voyage AI's API — Anthropic's recommended embeddings provider.
  openai  OpenAI's embeddings API.
  hash    a deterministic, dependency-free bag-of-hashed-tokens vector. Not
          semantic — it exists so tests and CI can exercise the whole hybrid
          path without a model download or an API key, and so the system
          degrades to *something* rather than crashing when nothing is
          configured.

All backends satisfy the same tiny contract (`name`, `dim`, `embed`), so the
vector store, the fusion step, and the eval harness never know or care which
one produced the numbers.
"""

from __future__ import annotations

import hashlib
import math
import os
from typing import Protocol, runtime_checkable


@runtime_checkable
class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one unit-normalised vector per input text."""
        ...


def _normalise(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


class HashEmbedder:
    """Deterministic, dependency-free embeddings via feature hashing.

    Each token is hashed into one of `dim` buckets (signed), giving a sparse
    lexical vector. It captures word overlap, NOT meaning — so it must never
    be presented as semantic retrieval. Its job is to make the hybrid
    plumbing runnable and testable everywhere, offline, with no model.
    """

    name = "hash-256"
    dim = 256

    def __init__(self, dim: int = 256):
        self.dim = dim
        self.name = f"hash-{dim}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        import re
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            for tok in re.findall(r"\w+", text.lower()):
                h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "big")
                bucket = h % self.dim
                sign = 1.0 if (h >> 63) & 1 else -1.0
                vec[bucket] += sign
            out.append(_normalise(vec))
        return out


class LocalEmbedder:
    """On-device embeddings with fastembed (bge-small by default).

    Downloads the model once and runs it locally — no text leaves the
    machine. Raises EmbedderUnavailable if fastembed is not installed.
    """

    def __init__(self, model: str = "BAAI/bge-small-en-v1.5"):
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise EmbedderUnavailable(
                "fastembed not installed (pip install fastembed)"
            ) from exc
        self._model = TextEmbedding(model_name=model)
        self.name = f"local:{model}"
        # bge-small is 384-dimensional; discover it from a probe so a different
        # model just works.
        self.dim = len(next(iter(self._model.embed(["probe"]))))

    def embed(self, texts: list[str]) -> list[list[float]]:
        # fastembed returns numpy arrays, already L2-normalised for bge.
        return [_normalise([float(x) for x in v]) for v in self._model.embed(list(texts))]


class _HTTPEmbedder:
    """Shared base for API embedders (Voyage, OpenAI)."""

    endpoint = ""
    key_env = ""
    name = ""
    dim = 0

    def __init__(self, model: str, api_key: str | None = None):
        self.model = model
        self.api_key = api_key or os.environ.get(self.key_env, "")
        if not self.api_key:
            raise EmbedderUnavailable(f"{self.key_env} is not set")

    def _post(self, payload: dict) -> dict:
        import json
        import urllib.request
        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())


class VoyageEmbedder(_HTTPEmbedder):
    endpoint = "https://api.voyageai.com/v1/embeddings"
    key_env = "VOYAGE_API_KEY"

    def __init__(self, model: str = "voyage-3.5-lite", api_key: str | None = None):
        super().__init__(model, api_key)
        self.name = f"voyage:{model}"
        self.dim = 0  # discovered on first embed

    def embed(self, texts: list[str]) -> list[list[float]]:
        data = self._post({"model": self.model, "input": list(texts),
                           "input_type": "document"})
        vecs = [_normalise([float(x) for x in row["embedding"]])
                for row in data["data"]]
        if vecs:
            self.dim = len(vecs[0])
        return vecs


class OpenAIEmbedder(_HTTPEmbedder):
    endpoint = "https://api.openai.com/v1/embeddings"
    key_env = "OPENAI_API_KEY"

    def __init__(self, model: str = "text-embedding-3-small", api_key: str | None = None):
        super().__init__(model, api_key)
        self.name = f"openai:{model}"
        self.dim = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        data = self._post({"model": self.model, "input": list(texts)})
        vecs = [_normalise([float(x) for x in row["embedding"]])
                for row in data["data"]]
        if vecs:
            self.dim = len(vecs[0])
        return vecs


class EmbedderUnavailable(RuntimeError):
    """The configured embedder needs a library or key that isn't present."""


def get_embedder(name: str | None = None) -> Embedder:
    """Build the embedder named by `name` or the UPLINK_EMBEDDER env var.

    Values: local | voyage | openai | hash (default). Unknown names fall
    back to the hash embedder rather than crashing, so a misconfiguration
    degrades to non-semantic retrieval instead of an outage.
    """
    choice = (name or os.environ.get("UPLINK_EMBEDDER") or "hash").strip().lower()
    if choice == "local":
        return LocalEmbedder()
    if choice == "voyage":
        return VoyageEmbedder()
    if choice == "openai":
        return OpenAIEmbedder()
    return HashEmbedder()
