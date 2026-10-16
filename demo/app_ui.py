"""Streamlit demo UI for the DocMind API.

Two modes, selected purely by configuration:

* **Local development** (default): the full UI — upload a document, poll its
  processing status, then chat with it. Requires an API key only when the API
  enforces one.
* **Public demo** (``DOCMIND_DEMO_MODE=1``): read-only. Upload and delete are
  never reachable from the page — the corpus is chosen from the documents
  already seeded into the isolated demo stack (see ``docker-compose.demo.yml``)
  and questions are length-capped. This exists so a public deployment cannot be
  used to push arbitrary content into the index or to burn the provider budget
  without bound.
"""

from __future__ import annotations

import json
import os

import requests
import streamlit as st

# Internal base URL the Streamlit *container* uses to reach the API inside Docker.
# The hostname is a Compose service name ("api") that resolves only on the Docker
# network -- it is NOT reachable from the user's browser.
API_BASE_URL = os.getenv("DOCMIND_API_URL", "http://localhost:8000/api/v1").rstrip("/")

# Browser-reachable server root, used only for the sidebar link. The API's
# browsable pages live at the server root (``/docs``, ``/health``); the bare
# ``/api/v1`` prefix is a router mount with no route of its own (404).
#
# ``DOCMIND_PUBLIC_API_URL`` should be the host-reachable origin (e.g.
# ``http://localhost:8000``, the port published by docker-compose.yml). When it
# is unset, fall back to the internal URL with the Compose service host swapped
# for ``localhost`` and the ``/api/v1`` prefix stripped.
_PUBLIC_API_ROOT = os.getenv("DOCMIND_PUBLIC_API_URL", "").rstrip("/")
if not _PUBLIC_API_ROOT:
    _PUBLIC_API_ROOT = API_BASE_URL.replace("//api:", "//localhost:").removesuffix("/api/v1")
SWAGGER_URL = f"{_PUBLIC_API_ROOT}/docs"

REQUEST_TIMEOUT = 30

# Optional shared secret: when the API runs with ``API_KEY`` set (always the case
# in production), every ``/api/v1/*`` call must present it in this header.
API_KEY = os.getenv("DOCMIND_API_KEY", "")


def _env_flag(name: str, *, default: bool = False) -> bool:
    """Read a boolean environment flag tolerantly."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, *, default: int, minimum: int = 1) -> int:
    """Read an integer environment setting, falling back on junk values."""
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


# Public demo posture. Off by default so local development keeps the full UI.
DEMO_MODE = _env_flag("DOCMIND_DEMO_MODE")
DEMO_MAX_QUESTION_CHARS = _env_int("DOCMIND_DEMO_MAX_QUESTION_CHARS", default=500)
DEMO_TOP_K = _env_int("DOCMIND_DEMO_TOP_K", default=4)


def _auth_headers() -> dict[str, str]:
    """Return the auth header when a key is configured, else an empty mapping."""
    return {"X-API-Key": API_KEY} if API_KEY else {}


def _fetch_documents() -> list[dict]:
    """Return the indexed documents available for chatting."""
    response = requests.get(
        f"{API_BASE_URL}/documents",
        headers=_auth_headers(),
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def _chat(document_id: str, prompt: str) -> None:
    """Stream a grounded answer for ``prompt`` scoped to ``document_id``."""
    if "messages" not in st.session_state:
        st.session_state.messages = []

    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        placeholder = st.empty()
        full_response = ""
        sources: list[dict] = []
        try:
            response = requests.post(
                f"{API_BASE_URL}/chat/completions",
                json={
                    "query": prompt,
                    "top_k": DEMO_TOP_K if DEMO_MODE else 4,
                    "document_ids": [document_id],
                },
                headers=_auth_headers(),
                stream=True,
                timeout=300,
            )
            response.raise_for_status()

            for line in response.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                payload = json.loads(line[len("data: ") :])
                event_type = payload.get("type")
                if event_type == "token":
                    full_response += payload.get("content", "")
                    placeholder.markdown(full_response + "▌")
                elif event_type == "sources":
                    sources = payload.get("content", [])
                elif event_type == "error":
                    st.error(payload.get("message", "Unknown error"))
                    break
                elif event_type == "done":
                    break

            placeholder.markdown(full_response or "_No answer returned._")

            if sources:
                with st.expander("Sources"):
                    for index, source in enumerate(sources, start=1):
                        st.markdown(
                            f"**{index}. {source.get('filename')}** "
                            f"(chunk {source.get('chunk_index')}, "
                            f"score {source.get('score')})\n\n"
                            f"> {source.get('excerpt', '')}"
                        )
        except requests.RequestException as exc:
            st.error(f"Error during chat: {exc}")

        st.session_state.messages.append({"role": "assistant", "content": full_response})


st.set_page_config(page_title="DocMind AI - Demo", page_icon="🧠", layout="wide")

st.title("🧠 DocMind AI")
st.markdown("Enterprise RAG System for Document Intelligence")

if DEMO_MODE:
    st.info(
        "**Public demonstration.** Questions are answered only from a small, "
        "curated document set. Uploads are disabled and no data you enter is "
        "stored beyond your session — please don't paste anything confidential."
    )

if DEMO_MODE:
    tab_docs, tab_chat = st.tabs(["📚 Demo corpus", "💬 Chat with the corpus"])
else:
    tab_docs, tab_chat = st.tabs(["📄 Document Management", "💬 Chat with Documents"])

# ---------------------------------------------------------------------------
# Documents: read-only picker in demo mode, uploader elsewhere
# ---------------------------------------------------------------------------
with tab_docs:
    if DEMO_MODE:
        st.header("Available documents")
        st.caption(
            "This deployment indexes a fixed set of documents. Pick one and ask "
            "questions about it in the chat tab."
        )
        try:
            documents = [
                document
                for document in _fetch_documents()
                if document.get("chunk_count", 0) > 0
            ]
        except requests.RequestException as exc:
            documents = []
            st.error(f"Could not load the document list: {exc}")

        if not documents:
            st.warning(
                "The demo corpus is still empty. An operator has to load it with "
                "the seed step before this page can answer questions."
            )
        else:
            labels = {
                document["id"]: f"{document['filename']} — {document['chunk_count']} chunks"
                for document in documents
            }
            selected = st.radio(
                "Choose a document",
                options=list(labels),
                format_func=lambda document_id: labels[document_id],
            )
            st.session_state["current_doc_id"] = selected
    else:
        st.header("Upload & Process Documents")
        uploaded_file = st.file_uploader(
            "Choose a file (PDF, TXT, MD, CSV, JSON)",
            type=["pdf", "txt", "md", "csv", "json"],
        )

        if uploaded_file is not None and st.button("Upload Document"):
            files = {"file": (uploaded_file.name, uploaded_file.getvalue())}
            try:
                response = requests.post(
                    f"{API_BASE_URL}/documents/upload",
                    files=files,
                    headers=_auth_headers(),
                    timeout=REQUEST_TIMEOUT,
                )
                if response.status_code == 202:
                    data = response.json()
                    st.session_state["current_doc_id"] = data["id"]
                    st.success(f"Document uploaded successfully! ID: {data['id']}")
                    st.info("Processing started in the background.")
                else:
                    detail = response.json().get("detail", response.text)
                    st.error(f"Upload failed ({response.status_code}): {detail}")
            except requests.RequestException as exc:
                st.error(f"Error connecting to API: {exc}")

        doc_id = st.session_state.get("current_doc_id")
        if doc_id:
            st.divider()
            st.subheader("Processing Status")
            if st.button("Check Status"):
                try:
                    response = requests.get(
                        f"{API_BASE_URL}/documents/{doc_id}/status",
                        headers=_auth_headers(),
                        timeout=REQUEST_TIMEOUT,
                    )
                    if response.status_code == 200:
                        data = response.json()
                        st.write(f"**Status:** {data['status']}")
                        st.write(f"**Filename:** {data['filename']}")
                        st.write(f"**Chunks:** {data.get('chunk_count', 0)}")
                        if data.get("error"):
                            st.error(data["error"])
                    else:
                        st.error(f"Could not fetch status ({response.status_code}).")
                except requests.RequestException as exc:
                    st.error(f"Error: {exc}")

# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
with tab_chat:
    st.header("Chat with the corpus" if DEMO_MODE else "Chat with your Documents")

    doc_id = st.session_state.get("current_doc_id")
    if not doc_id:
        if DEMO_MODE:
            st.warning("Pick a document in the 'Demo corpus' tab first.")
        else:
            st.warning(
                "Please upload and process a document first in the 'Document Management' tab."
            )
    else:
        prompt = st.chat_input("Ask anything about this document...")
        if prompt:
            # Enforced here rather than via a widget argument so the check holds
            # regardless of the installed Streamlit version.
            if DEMO_MODE and len(prompt) > DEMO_MAX_QUESTION_CHARS:
                st.warning(
                    f"Please keep questions under {DEMO_MAX_QUESTION_CHARS} characters "
                    "in the public demo."
                )
            else:
                _chat(doc_id, prompt)

st.sidebar.markdown("---")
st.sidebar.markdown("### System Info")
st.sidebar.info("Backend: FastAPI\nDatabase: pgvector\nWorker: Arq")
if DEMO_MODE:
    st.sidebar.caption(
        "Public demo on an isolated stack: documents are pre-seeded and uploads "
        "are disabled."
    )
elif SWAGGER_URL:
    st.sidebar.caption(f"API docs: [{SWAGGER_URL}]({SWAGGER_URL})")
