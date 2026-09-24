"""Streamlit demo UI for the DocMind API."""

from __future__ import annotations

import json
import os

import requests
import streamlit as st

API_BASE_URL = os.getenv("DOCMIND_API_URL", "http://localhost:8000/api/v1").rstrip("/")
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
st.sidebar.caption(f"API: {API_BASE_URL}")
