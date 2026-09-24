"""Streamlit demo UI for the DocMind API."""

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
    _PUBLIC_API_ROOT = (
        API_BASE_URL.replace("//api:", "//localhost:").removesuffix("/api/v1")
    )
SWAGGER_URL = f"{_PUBLIC_API_ROOT}/docs"

REQUEST_TIMEOUT = 30

st.set_page_config(page_title="DocMind AI - Demo", page_icon="🧠", layout="wide")

st.title("🧠 DocMind AI")
st.markdown("Enterprise RAG System for Document Intelligence")

tab_docs, tab_chat = st.tabs(["📄 Document Management", "💬 Chat with Documents"])

# ---------------------------------------------------------------------------
# Document management
# ---------------------------------------------------------------------------
with tab_docs:
    st.header("Upload & Process Documents")
    uploaded_file = st.file_uploader(
        "Choose a file (PDF, TXT, MD, CSV, JSON)",
        type=["pdf", "txt", "md", "csv", "json"],
    )

    if uploaded_file is not None and st.button("Upload Document"):
        files = {"file": (uploaded_file.name, uploaded_file.getvalue())}
        try:
            response = requests.post(
                f"{API_BASE_URL}/documents/upload", files=files, timeout=REQUEST_TIMEOUT
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
                    f"{API_BASE_URL}/documents/{doc_id}/status", timeout=REQUEST_TIMEOUT
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
    st.header("Chat with your Documents")

    doc_id = st.session_state.get("current_doc_id")
    if not doc_id:
        st.warning(
            "Please upload and process a document first in the "
            "'Document Management' tab."
        )
    else:
        if "messages" not in st.session_state:
            st.session_state.messages = []

        for message in st.session_state.messages:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])

        if prompt := st.chat_input("Ask anything about your document..."):
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
                        json={"query": prompt, "top_k": 4, "document_ids": [doc_id]},
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

                st.session_state.messages.append(
                    {"role": "assistant", "content": full_response}
                )

st.sidebar.markdown("---")
st.sidebar.markdown("### System Info")
st.sidebar.info("Backend: FastAPI\nDatabase: pgvector\nWorker: Arq")
st.sidebar.caption(f"API docs: [{SWAGGER_URL}]({SWAGGER_URL})")
