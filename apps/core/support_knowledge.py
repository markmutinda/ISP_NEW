"""
Tenant-facing support knowledge retrieval for the Netily assistant demo.

This intentionally avoids architecture-level answers. The first demo uses a
small local Markdown knowledge base and deterministic keyword retrieval so it
cannot invent internal implementation details.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings


ARCHITECTURE_BLOCKLIST = {
    "architecture",
    "database",
    "schema",
    "postgres",
    "docker",
    "container",
    "server",
    "source code",
    "secret",
    "credential",
    "password hash",
    "private key",
    "wireguard",
    "openvpn",
    "vpn config",
    "deployment",
    "ssh",
    "env",
}

STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "how",
    "i",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "the",
    "to",
    "what",
    "when",
    "where",
    "with",
}


@dataclass(frozen=True)
class SupportDocument:
    title: str
    source: str
    text: str
    tokens: set[str]


def _knowledge_dir() -> Path:
    return Path(settings.BASE_DIR) / "rag" / "netily-support"


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {word for word in words if len(word) > 2 and word not in STOPWORDS}


def _title_from_text(text: str, fallback: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip() or fallback
    return fallback


def load_support_documents() -> list[SupportDocument]:
    docs: list[SupportDocument] = []
    directory = _knowledge_dir()
    if not directory.exists():
        return docs

    for path in sorted(directory.glob("*.md")):
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            continue
        docs.append(
            SupportDocument(
                title=_title_from_text(text, path.stem.replace("-", " ").title()),
                source=f"rag/netily-support/{path.name}",
                text=text,
                tokens=_tokens(text),
            )
        )
    return docs


def is_architecture_question(question: str) -> bool:
    normalized = question.lower()
    return any(term in normalized for term in ARCHITECTURE_BLOCKLIST)


def answer_support_question(question: str) -> dict:
    cleaned = (question or "").strip()
    if not cleaned:
        return {
            "answer": "Ask me a Netily onboarding, billing, router, hotspot, leads, dispatch, or inventory question.",
            "sources": [],
            "confidence": 0,
            "blocked": False,
        }

    if is_architecture_question(cleaned):
        return {
            "answer": (
                "I can help with Netily workflows, onboarding, billing, routers, hotspot, leads, "
                "dispatch, and inventory. I cannot share internal architecture, server, database, "
                "deployment, or credential details."
            ),
            "sources": [],
            "confidence": 0,
            "blocked": True,
        }

    question_tokens = _tokens(cleaned)
    if not question_tokens:
        return {
            "answer": "Please ask a more specific Netily support question.",
            "sources": [],
            "confidence": 0,
            "blocked": False,
        }

    ranked = []
    for doc in load_support_documents():
        overlap = question_tokens & doc.tokens
        if overlap:
            score = len(overlap) / max(len(question_tokens), 1)
            ranked.append((score, doc))

    ranked.sort(key=lambda item: item[0], reverse=True)
    if not ranked or ranked[0][0] < 0.12:
        return {
            "answer": (
                "I do not have an approved support answer for that yet. Add the topic to "
                "`rag/netily-support` or contact Netily support for help."
            ),
            "sources": [],
            "confidence": 0,
            "blocked": False,
        }

    best = ranked[:2]
    snippets = []
    sources = []
    for score, doc in best:
        body = re.sub(r"^#+\s*", "", doc.text, flags=re.MULTILINE)
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        snippet = " ".join(lines[:8])
        snippets.append(snippet)
        sources.append({"title": doc.title, "source": doc.source, "score": round(score, 2)})

    answer = " ".join(snippets)
    return {
        "answer": answer[:1200],
        "sources": sources,
        "confidence": round(best[0][0], 2),
        "blocked": False,
    }
