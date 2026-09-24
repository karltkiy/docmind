import streamlit as st
import requests
import time
import uuid
from typing import List

# Configuration
API_BASE_URL = "http://localhost:8000/api/v1"

st.set_page_config(
    page_title="DocMind AI - Demo",
    page_icon="🧠",
    layout="wide"
)

st.title("🧠 DocMind AI")
st.markdown("Enterprise RAG System for Document Intelligence")

tab1, tab2 = st.tabs(["📄 Document Management", "💬 Chat with Documents"])

with tab1:
    st.header("Upload & Process Documents")
    uploaded_file = st.file_uploader("Choose a file (PDF or TXT)", type=["pdf", "txt"])
    
    if uploaded_file is not None:
        if st.button("Upload Document"):
            files = {"file": (uploaded_file.name, uploaded_file.getvalue())}
            try:
                response = requests.post(f"{API_BASE_URL}/documents/upload", files=files)
                if response.status_code == 202:
                    doc_id = response.json().get("id")
                    st.success(f"Document uploaded successfully! ID: {doc_id}")
                    st.info("Processing started in the background.")
                    st.session_state["current_doc_id"] = doc_id
                else:
                    st.error(f"Failed to upload: {response.text}")
            except Exception as e:
                st.error(f"Error connecting to API: {e}")

    if "current_doc_id" in st.session_state:
        doc_id = st.session_state["current_doc_id"]
        st.divider()
        st.subheader("Processing Status")
        
        if st.button("Check Status"):
            try:
                response = requests.get(f"{API_BASE_URL}/documents/{doc_id}/status")
                if response.status_code == 200:
                    data = response.json()
                    st.write(f"**Status:** {data['status']}")
                    st.write(f"**Filename:** {data['filename']}")
                    st.write(f"**Chunks:** {data.get('chunk_count', 0)}")
                else:
                    st.error("Could not fetch status.")
            except Exception as e:
                st.error(f"Error: {e}")

with tab2:
    st.header("Chat with your Documents")
    
    if "current_doc_id" not in st.session_state or st.session_state["current_doc_id"] is None:
        st.warning("Please upload and process a document first in the 'Document Management' tab.")
    else:
        doc_id = st.session_state["current_doc_id"]
        
        # Chat interface
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
                # Use SSE for streaming
                try:
                    # Note: In a real production app, we'd use a streaming library for SSE
                    # but for a simple demo, we'll iterate over the response.
                    response = requests.post(
                        f"{API_BASE_URL}/chat/completions",
                        json={
                            "query": prompt,
                            "top_k": 4,
                            "document_ids": [doc_id]
                        },
                        stream=True
                    )
                    
                    full_response = ""
                    for line in response.iter_lines():
                        if line:
                            line_text = line.decode('utf-8')
                            if line_text.startswith("data: "):
                                content = line_text[6:]
                                if content == "[DONE]":
                                    break
                                full_response += content
                                st.markdown(full_response + " ", type="markdown")
                    
                    st.session_state.messages.append({"role": "assistant", "content": full_response})
                except Exception as e:
                    st.error(f"Error during chat: {e}")

# Footer
st.sidebar.markdown("---")
st.sidebar.markdown("### System Info")
st.sidebar.info("Backend: FastAPI\nDatabase: pgvector\nWorker: Arq")
