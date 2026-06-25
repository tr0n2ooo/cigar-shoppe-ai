"""
research_rag.py
---------------
RAG (Retrieval-Augmented Generation) layer over the Cigar_Research.xlsx database.

Architecture
------------
1. On first call, loads all researched cigars into a persistent ChromaDB
   collection stored at data/chroma_research/.
2. search_similar(query, k) retrieves semantically similar cigars in two stages:

     a. Broad candidate retrieval (3×k results by cosine similarity)
     b. MMR (Maximal Marginal Relevance) re-ranking for diversity
        — balances relevance against redundancy so you don't get five nearly-
          identical Connecticut-wrapper cigars when one would do
     c. Optional BGE cross-encoder re-ranking for precision
        — uses sentence-transformers CrossEncoder (ms-marco-MiniLM-L-6-v2)
          to score each (query, document) pair; falls back silently if the
          library is not installed

3. The collection is persisted to disk and reused across runs; call
   rebuild_index() to force a refresh after Cigar_Research.xlsx changes.

ReAct loop context
------------------
This module is the semantic "Retrieve" action in the ordering ReAct loop.
The agent can ask "what cigars in our research database are similar to X?"
before deciding what to stock — grounding the recommendation in verified
inventory knowledge rather than model memory.

Usage
-----
    from research_rag import search_similar, rebuild_index, index_status

    results = search_similar("medium-bodied Connecticut wrapper under $15", k=5)
    for r in results:
        print(r["Description"], r["Brand"], r.get("_similarity"))

    # Force rebuild after updating Cigar_Research.xlsx:
    rebuild_index()
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"
RESEARCH_FILE = DATA_DIR / "Cigar_Research.xlsx"
CHROMA_DIR = DATA_DIR / "chroma_research"

_COLLECTION_NAME = "cigar_research"


# ── public API ────────────────────────────────────────────────────────────────

def search_similar(
    query: str,
    k: int = 5,
    mmr_lambda: float = 0.7,
) -> list[dict]:
    """
    Find cigars in the research database semantically similar to `query`.

    Parameters
    ----------
    query       : Natural-language query, e.g. "bold maduro under $20 with
                  chocolate and earth notes"
    k           : Number of results to return.
    mmr_lambda  : MMR diversity/relevance balance.
                  1.0 = pure relevance, 0.0 = pure diversity.
                  Default 0.7 biases toward relevance while still promoting
                  variety — avoids returning five nearly-identical cigars.

    Returns
    -------
    List of metadata dicts (one per cigar) with an added ``_similarity``
    field (0–1, higher = more similar) and optionally ``_rerank_score``.
    """
    try:
        collection = _get_collection()
    except Exception as exc:
        log.warning("RAG unavailable (ChromaDB error): %s", exc)
        return []

    count = collection.count()
    if count == 0:
        log.warning("Chroma collection is empty — call rebuild_index().")
        return []

    fetch_n = min(count, k * 3)  # over-fetch so MMR has candidates to prune
    log.info("[RAG] query=%r  requesting k=%d  index_size=%d  fetching %d candidates",
             query[:80], k, count, fetch_n)

    raw = collection.query(
        query_texts=[query],
        n_results=fetch_n,
        include=["documents", "metadatas", "embeddings", "distances"],
    )

    docs = raw["documents"][0]
    metas = raw["metadatas"][0]
    embeddings = raw.get("embeddings") or [None]
    embeddings = embeddings[0]
    distances = raw["distances"][0]   # cosine distances (lower = more similar)

    similarities = [max(0.0, 1.0 - d) for d in distances]

    top_names = [m.get("Description", m.get("Brand", "?")) for m in metas[:3]]
    log.info("[RAG] broad retrieval: %d candidates  top-3 by cosine: %s",
             len(docs), " | ".join(top_names))

    # ── MMR re-ranking ────────────────────────────────────────────────────────
    if embeddings is not None and len(docs) > k:
        log.info("[RAG] MMR re-ranking: %d candidates → %d results  lambda=%.2f"
                 "  (relevance=%.0f%%  diversity=%.0f%%)",
                 len(docs), k, mmr_lambda, mmr_lambda * 100, (1 - mmr_lambda) * 100)
        selected = _mmr(embeddings, similarities, k, mmr_lambda)
    else:
        log.info("[RAG] MMR skipped (embeddings unavailable or candidates ≤ k) — using top-%d", k)
        selected = list(range(min(k, len(docs))))

    results = []
    for i in selected:
        meta = dict(metas[i])
        meta["_similarity"] = round(similarities[i], 4)
        meta["_document"] = docs[i]
        results.append(meta)

    mmr_names = [r.get("Description", r.get("Brand", "?")) for r in results]
    log.info("[RAG] MMR selected: %s", " | ".join(mmr_names))

    # ── BGE cross-encoder re-ranking (optional) ───────────────────────────────
    results = _rerank(query, results)

    final_names = [r.get("Description", r.get("Brand", "?")) for r in results[:k]]
    log.info("[RAG] final results (%d): %s", len(results[:k]), " | ".join(final_names))

    return results[:k]


def rebuild_index() -> int:
    """
    Drop and rebuild the ChromaDB collection from Cigar_Research.xlsx.
    Returns the number of documents indexed.
    """
    import chromadb

    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    try:
        client.delete_collection(_COLLECTION_NAME)
        log.info("Dropped existing collection '%s'.", _COLLECTION_NAME)
    except Exception:
        pass
    collection = _create_collection(client)
    n = _populate(collection)
    log.info("Rebuilt index: %d documents.", n)
    return n


def index_status() -> dict:
    """Return basic stats about the vector index."""
    try:
        collection = _get_collection()
        return {
            "indexed_cigars": collection.count(),
            "index_path": str(CHROMA_DIR),
            "research_file": str(RESEARCH_FILE),
        }
    except Exception as exc:
        return {
            "error": str(exc),
            "index_path": str(CHROMA_DIR),
            "research_file": str(RESEARCH_FILE),
        }


# ── internals ─────────────────────────────────────────────────────────────────

def _get_collection():
    import chromadb

    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))

    try:
        collection = client.get_collection(
            _COLLECTION_NAME,
            embedding_function=_embedding_fn(),
        )
        if collection.count() == 0:
            _populate(collection)
        return collection
    except Exception:
        collection = _create_collection(client)
        _populate(collection)
        return collection


def _create_collection(client):
    return client.create_collection(
        name=_COLLECTION_NAME,
        embedding_function=_embedding_fn(),
        metadata={"hnsw:space": "cosine"},
    )


def _embedding_fn():
    from chromadb.utils import embedding_functions
    return embedding_functions.DefaultEmbeddingFunction()


def _populate(collection) -> int:
    import pandas as pd

    if not RESEARCH_FILE.exists():
        log.warning("Research file not found: %s", RESEARCH_FILE)
        return 0

    df = pd.read_excel(RESEARCH_FILE, engine="openpyxl")
    df.columns = [c.strip() for c in df.columns]

    documents: list[str] = []
    ids: list[str] = []
    metadatas: list[dict] = []
    seen_ids: set[str] = set()

    for i, row in df.iterrows():
        row_dict = {k: v for k, v in row.items() if _is_valid(v)}
        doc = _row_to_text(row_dict)
        if not doc:
            continue

        raw_id = str(row_dict.get("Item Number", i))
        doc_id = raw_id if raw_id not in seen_ids else f"{raw_id}_{i}"
        seen_ids.add(doc_id)

        ids.append(doc_id)
        documents.append(doc)
        metadatas.append({k: str(v)[:500] for k, v in row_dict.items()})

    if documents:
        # ChromaDB recommends batches ≤ 5,000; our dataset is tiny so one shot is fine
        collection.add(documents=documents, ids=ids, metadatas=metadatas)
        log.info("Indexed %d cigars into ChromaDB at %s.", len(documents), CHROMA_DIR)

    return len(documents)


def _is_valid(v) -> bool:
    try:
        import pandas as pd
        if isinstance(v, float) and pd.isna(v):
            return False
    except Exception:
        pass
    return v is not None and str(v).strip() != ""


def _row_to_text(row: dict) -> str:
    """
    Build a dense natural-language document from a research row for embedding.
    Combines all semantically meaningful fields into one searchable string.
    """
    parts = []
    if row.get("Brand"):
        parts.append(f"Brand: {row['Brand']}")
    if row.get("Description"):
        parts.append(f"Name: {row['Description']}")
    if row.get("Parent Company"):
        parts.append(f"Maker: {row['Parent Company']}")
    if row.get("Wrapper"):
        parts.append(f"Wrapper: {row['Wrapper']}")
    if row.get("Binder"):
        parts.append(f"Binder: {row['Binder']}")
    if row.get("Filler"):
        parts.append(f"Filler: {row['Filler']}")
    if row.get("Country of Origin"):
        parts.append(f"Origin: {row['Country of Origin']}")
    if row.get("Strength"):
        parts.append(f"Strength: {row['Strength']}")
    if row.get("Shape"):
        parts.append(f"Shape: {row['Shape']}")
    if row.get("Flavor Notes"):
        parts.append(f"Flavor: {row['Flavor Notes']}")
    if row.get("MSRP"):
        parts.append(f"MSRP: ${row['MSRP']}")
    if row.get("Top Rating"):
        src = row.get("Rating Source", "")
        parts.append(f"Rating: {row['Top Rating']}" + (f" ({src})" if src else ""))
    return ". ".join(str(p) for p in parts if p)


def _mmr(
    embeddings: list[list[float]],
    similarities: list[float],
    k: int,
    lambda_: float,
) -> list[int]:
    """
    Maximal Marginal Relevance selection.

    Iteratively selects the index that maximises:
        λ × sim_to_query  −  (1−λ) × max_cosine_sim_to_already_selected

    A result that is highly relevant but nearly identical to an already-
    selected result scores lower than a slightly less relevant but distinct
    one — promoting diversity across wrapper styles, strengths, etc.

    Parameters
    ----------
    embeddings  : List of embedding vectors (one per candidate).
    similarities: Cosine similarities to the query (same order).
    k           : Number of items to select.
    lambda_     : Relevance/diversity trade-off (see search_similar).
    """
    import numpy as np

    emb = np.array(embeddings, dtype=float)
    sims = np.array(similarities, dtype=float)

    selected: list[int] = []
    remaining = list(range(len(embeddings)))

    for _ in range(min(k, len(embeddings))):
        if not remaining:
            break

        if not selected:
            best = max(remaining, key=lambda i: sims[i])
        else:
            sel_embs = emb[selected]

            def mmr_score(i: int) -> float:
                ci = emb[i]
                norm_i = np.linalg.norm(ci)
                if norm_i == 0:
                    redundancy = 0.0
                else:
                    dots = sel_embs @ ci
                    sel_norms = np.linalg.norm(sel_embs, axis=1)
                    cosines = dots / (sel_norms * norm_i + 1e-9)
                    redundancy = float(cosines.max())
                return lambda_ * float(sims[i]) - (1.0 - lambda_) * redundancy

            best = max(remaining, key=mmr_score)

        selected.append(best)
        remaining.remove(best)

    return selected


def _rerank(query: str, results: list[dict]) -> list[dict]:
    """
    BGE cross-encoder re-ranking via sentence-transformers.

    Uses the ms-marco-MiniLM-L-6-v2 cross-encoder to score each
    (query, document) pair jointly — this captures query-document
    interaction that the bi-encoder embedding misses.

    Falls back gracefully if sentence-transformers is not installed.
    """
    if len(results) <= 1:
        return results
    try:
        from sentence_transformers import CrossEncoder

        model = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2", max_length=512)
        pairs = [(query, r["_document"]) for r in results]
        scores = model.predict(pairs)
        for result, score in zip(results, scores):
            result["_rerank_score"] = float(score)
        results.sort(key=lambda r: r.get("_rerank_score", 0.0), reverse=True)
        log.info("BGE cross-encoder reranker applied to %d results.", len(results))
    except ImportError:
        log.debug("sentence-transformers not installed — skipping BGE reranking.")
    except Exception as exc:
        log.warning("Reranker error (continuing without reranking): %s", exc)
    return results


# ── CLI helper ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import json

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Cigar Research RAG")
    sub = parser.add_subparsers(dest="cmd")

    s = sub.add_parser("search", help="Semantic search over cigar research")
    s.add_argument("query", help="Natural-language query")
    s.add_argument("--k", type=int, default=5, help="Number of results")
    s.add_argument("--lambda", dest="mmr_lambda", type=float, default=0.7)

    sub.add_parser("rebuild", help="Rebuild the vector index")
    sub.add_parser("status", help="Show index stats")

    args = parser.parse_args()

    if args.cmd == "search":
        results = search_similar(args.query, k=args.k, mmr_lambda=args.mmr_lambda)
        print(json.dumps(results, indent=2, default=str))
    elif args.cmd == "rebuild":
        n = rebuild_index()
        print(f"Indexed {n} cigars.")
    elif args.cmd == "status":
        print(json.dumps(index_status(), indent=2))
    else:
        parser.print_help()
