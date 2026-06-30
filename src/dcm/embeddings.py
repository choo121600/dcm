from __future__ import annotations

import hashlib
import logging
import math
from typing import Protocol

log = logging.getLogger(__name__)


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class LocalEmbedder:
    """Deterministic, NON-semantic embeddings for offline dev/testing only.

    Lets the whole memory pipeline run without an embedding key. Retrieval quality is
    meaningless (it's a crude hashed bag-of-words) — use a real provider for production.
    """

    def __init__(self, dim: int = 256) -> None:
        self._dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def _vec(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for token in text.lower().split():
            h = int(hashlib.sha1(token.encode("utf-8")).hexdigest(), 16)
            vec[h % self._dim] += 1.0
        return vec


class _HttpEmbedder:
    """Shared REST caller for OpenAI-compatible embedding endpoints."""

    def __init__(self, api_key: str, model: str, url: str) -> None:
        if not api_key:
            raise ValueError("embedding API key is required for this provider")
        self._api_key = api_key
        self._model = model
        self._url = url

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx  # lazy: only needed when a remote provider is selected

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                self._url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self._model, "input": texts},
            )
            resp.raise_for_status()
            return [item["embedding"] for item in resp.json()["data"]]


class VoyageEmbedder(_HttpEmbedder):
    def __init__(self, api_key: str, model: str = "voyage-3") -> None:
        super().__init__(api_key, model, "https://api.voyageai.com/v1/embeddings")


class OpenAIEmbedder(_HttpEmbedder):
    def __init__(self, api_key: str, model: str = "text-embedding-3-small") -> None:
        super().__init__(api_key, model, "https://api.openai.com/v1/embeddings")


def build_embedder(provider: str, api_key: str, model: str) -> Embedder:
    provider = (provider or "local").lower()
    if provider == "local":
        log.warning(
            "using LocalEmbedder (non-semantic) — set EMBEDDING_PROVIDER=voyage|openai "
            "with EMBEDDING_API_KEY for real memory"
        )
        return LocalEmbedder()
    if provider == "voyage":
        return VoyageEmbedder(api_key, model or "voyage-3")
    if provider == "openai":
        return OpenAIEmbedder(api_key, model or "text-embedding-3-small")
    raise ValueError(f"unknown embedding provider: {provider!r}")
