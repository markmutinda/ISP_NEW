"""
Tenant-facing RAG support assistant for Netily.

The assistant is intentionally scoped to curated Markdown files under
``rag/netily-support``. LangChain is used for document/chunk handling and for
the optional LLM prompt path, while deterministic retrieval remains available
so the demo keeps working even before an API key is configured.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from django.conf import settings

try:  # LangChain is optional at import time so deployments do not hard-crash.
    from langchain_core.documents import Document as LangChainDocument
    from langchain_core.prompts import PromptTemplate
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    LANGCHAIN_AVAILABLE = True
except Exception:  # pragma: no cover - exercised only when deps are missing.
    LangChainDocument = None
    PromptTemplate = None
    RecursiveCharacterTextSplitter = None
    LANGCHAIN_AVAILABLE = False

try:
    from langchain_openai import ChatOpenAI
except Exception:  # pragma: no cover - exercised only when deps are missing.
    ChatOpenAI = None


logger = logging.getLogger(__name__)


ARCHITECTURE_BLOCKLIST = {
    "api key",
    "architecture",
    "backend",
    "container",
    "credential",
    "database",
    "deployment",
    "docker",
    "env",
    "openvpn",
    "password hash",
    "postgres",
    "private key",
    "schema",
    "secret",
    "server",
    "source code",
    "ssh",
    "vpn config",
    "wireguard",
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

SUPPORT_PROMPT_TEMPLATE = """
You are Netily Support, a friendly tenant-facing assistant inside the Netily admin dashboard.

Rules:
- Answer only from the approved CONTEXT below.
- If the context is not enough, say that the support docs do not cover it yet.
- Do not reveal architecture, source code, database, server, deployment, VPN, credential, or secret details.
- Keep the tone warm, practical, and concise.
- Prefer clear steps or bullets when the user asks how to do something.
- Mention the relevant screen/route when it appears in the context.

CONTEXT:
{context}

QUESTION:
{question}

ANSWER:
""".strip()


@dataclass(frozen=True)
class SupportDocument:
    title: str
    source: str
    text: str
    tokens: set[str]


@dataclass(frozen=True)
class SupportChunk:
    title: str
    source: str
    text: str
    tokens: set[str]


def _settings_bool(name: str, default: bool) -> bool:
    raw = getattr(settings, name, os.getenv(name, str(default)))
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _settings_int(name: str, default: int) -> int:
    try:
        return int(getattr(settings, name, os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


def _knowledge_dir() -> Path:
    configured = getattr(settings, "NETILY_SUPPORT_CHAT_KNOWLEDGE_DIR", "rag/netily-support")
    path = Path(configured)
    if path.is_absolute():
        return path
    return Path(settings.BASE_DIR) / path


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {word for word in words if len(word) > 2 and word not in STOPWORDS}


def _title_from_text(text: str, fallback: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip() or fallback
    return fallback


@lru_cache(maxsize=1)
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


@lru_cache(maxsize=1)
def load_support_chunks() -> list[SupportChunk]:
    docs = load_support_documents()
    if not docs:
        return []

    if LANGCHAIN_AVAILABLE and LangChainDocument and RecursiveCharacterTextSplitter:
        langchain_docs = [
            LangChainDocument(
                page_content=doc.text,
                metadata={"title": doc.title, "source": doc.source},
            )
            for doc in docs
        ]
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=_settings_int("NETILY_SUPPORT_CHAT_CHUNK_SIZE", 900),
            chunk_overlap=_settings_int("NETILY_SUPPORT_CHAT_CHUNK_OVERLAP", 120),
        )
        split_docs = splitter.split_documents(langchain_docs)
        return [
            SupportChunk(
                title=str(chunk.metadata.get("title") or "Netily Support"),
                source=str(chunk.metadata.get("source") or "rag/netily-support"),
                text=chunk.page_content.strip(),
                tokens=_tokens(chunk.page_content),
            )
            for chunk in split_docs
            if chunk.page_content.strip()
        ]

    return [
        SupportChunk(title=doc.title, source=doc.source, text=doc.text, tokens=doc.tokens)
        for doc in docs
    ]


def _clean_doc_lines(text: str) -> list[str]:
    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^#+\s*", "", line)
        line = re.sub(r"^[-*]\s*", "", line)
        line = re.sub(r"^\d+\.\s*", "", line)
        line = line.strip()
        if line:
            lines.append(line)
    return lines


def _retrieve_chunks(question: str, limit: int = 4) -> list[tuple[float, SupportChunk]]:
    question_tokens = _tokens(question)
    if not question_tokens:
        return []

    ranked: list[tuple[float, SupportChunk]] = []
    for chunk in load_support_chunks():
        overlap = question_tokens & chunk.tokens
        if not overlap:
            continue
        score = len(overlap) / max(len(question_tokens), 1)
        title_tokens = _tokens(chunk.title)
        if question_tokens & title_tokens:
            score += 0.08
        ranked.append((score, chunk))

    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[:limit]


def _format_context(chunks: list[SupportChunk]) -> str:
    max_chars = _settings_int("NETILY_SUPPORT_CHAT_MAX_CONTEXT_CHARS", 5000)
    parts: list[str] = []
    used = 0
    for index, chunk in enumerate(chunks, start=1):
        part = f"[{index}] {chunk.title} ({chunk.source})\n{chunk.text}"
        if used + len(part) > max_chars:
            remaining = max(max_chars - used, 0)
            if remaining <= 100:
                break
            part = part[:remaining]
        parts.append(part)
        used += len(part)
    return "\n\n".join(parts)


def _organic_answer(question: str, chunks: list[SupportChunk]) -> str:
    topic = chunks[0].title if chunks else "that Netily workflow"
    collected: list[str] = []

    question_terms = _tokens(question)
    for chunk in chunks:
        for line in _clean_doc_lines(chunk.text):
            if line.lower() == chunk.title.lower():
                continue
            line_terms = _tokens(line)
            if question_terms and line_terms and not (question_terms & line_terms):
                continue
            if line not in collected:
                collected.append(line)
            if len(collected) >= 5:
                break
        if len(collected) >= 5:
            break

    if len(collected) < 3:
        for chunk in chunks:
            for line in _clean_doc_lines(chunk.text):
                if line.lower() == chunk.title.lower() or line in collected:
                    continue
                collected.append(line)
                if len(collected) >= 5:
                    break
            if len(collected) >= 5:
                break

    if not collected:
        return (
            f"I found approved guidance for {topic}, but it needs more detail in the "
            "support docs before I can give a useful answer."
        )

    intro = f"Here is the Netily-approved guidance for {topic}:"
    bullets = "\n".join(f"- {line}" for line in collected[:5])
    follow_up = (
        "If you want, ask me a more specific follow-up like where to click, what to check first, "
        "or what a tenant should expect."
    )
    return f"{intro}\n{bullets}\n\n{follow_up}"


def _llm_enabled() -> bool:
    return (
        _settings_bool("NETILY_SUPPORT_CHAT_USE_LLM", True)
        and LANGCHAIN_AVAILABLE
        and ChatOpenAI is not None
        and bool(getattr(settings, "OPENAI_API_KEY", os.getenv("OPENAI_API_KEY", "")))
    )


def _llm_answer(question: str, chunks: list[SupportChunk]) -> str | None:
    if not _llm_enabled():
        return None

    context = _format_context(chunks)
    try:
        if PromptTemplate:
            prompt = PromptTemplate.from_template(SUPPORT_PROMPT_TEMPLATE).format(
                context=context,
                question=question,
            )
        else:
            prompt = SUPPORT_PROMPT_TEMPLATE.format(context=context, question=question)

        llm = ChatOpenAI(
            model=getattr(settings, "NETILY_SUPPORT_CHAT_MODEL", "gpt-4o-mini"),
            temperature=float(getattr(settings, "NETILY_SUPPORT_CHAT_TEMPERATURE", 0.2)),
            timeout=_settings_int("NETILY_SUPPORT_CHAT_TIMEOUT", 20),
            max_retries=_settings_int("NETILY_SUPPORT_CHAT_MAX_RETRIES", 1),
        )
        response: Any = llm.invoke(prompt)
        answer = getattr(response, "content", "") or str(response)
        return answer.strip() or None
    except Exception:
        logger.warning("Netily support LLM answer failed; falling back to extractive answer", exc_info=True)
        return None


def is_architecture_question(question: str) -> bool:
    normalized = question.lower()
    return any(term in normalized for term in ARCHITECTURE_BLOCKLIST)


def support_chat_status() -> dict:
    docs = load_support_documents()
    chunks = load_support_chunks()
    return {
        "status": "ready" if docs else "missing_docs",
        "documents": len(docs),
        "chunks": len(chunks),
        "knowledge_dir": str(_knowledge_dir()),
        "langchain_available": LANGCHAIN_AVAILABLE,
        "llm_available": _llm_enabled(),
        "model": getattr(settings, "NETILY_SUPPORT_CHAT_MODEL", "gpt-4o-mini"),
    }


def answer_support_question(question: str) -> dict:
    cleaned = (question or "").strip()
    if not cleaned:
        return {
            "answer": "Ask me a Netily onboarding, billing, router, hotspot, leads, dispatch, or inventory question.",
            "sources": [],
            "confidence": 0,
            "blocked": False,
            "mode": "empty",
        }

    if is_architecture_question(cleaned):
        return {
            "answer": (
                "I can help with Netily workflows, onboarding, billing, routers, hotspot, leads, "
                "dispatch, and inventory. I cannot share internal architecture, server, database, "
                "deployment, VPN, source code, or credential details."
            ),
            "sources": [],
            "confidence": 0,
            "blocked": True,
            "mode": "blocked",
        }

    chunks_with_scores = _retrieve_chunks(cleaned)
    if not chunks_with_scores or chunks_with_scores[0][0] < 0.12:
        return {
            "answer": (
                "I do not have an approved support answer for that yet. Add the topic to "
                "`rag/netily-support` or contact Netily support for help."
            ),
            "sources": [],
            "confidence": 0,
            "blocked": False,
            "mode": "no_match",
        }

    chunks = [chunk for _, chunk in chunks_with_scores]
    llm_answer = _llm_answer(cleaned, chunks)
    answer = llm_answer or _organic_answer(cleaned, chunks)

    sources = [
        {"title": chunk.title, "source": chunk.source, "score": round(score, 2)}
        for score, chunk in chunks_with_scores
    ]

    return {
        "answer": answer[:1800],
        "sources": sources,
        "confidence": round(chunks_with_scores[0][0], 2),
        "blocked": False,
        "mode": "langchain_llm" if llm_answer else "langchain_extract",
    }
