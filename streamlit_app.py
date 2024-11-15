import streamlit as st
import logging
import os
import tempfile
import shutil
import pdfplumber
import ollama
import chromadb
from chromadb.config import Settings
import time

from langchain_ollama import ChatOllama
from langchain_community.embeddings import OllamaEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import Chroma
from langchain.prompts import ChatPromptTemplate, PromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough
from langchain.retrievers.multi_query import MultiQueryRetriever
from typing import List, Tuple, Dict, Any, Optional
from langchain_core.documents import Document

# Streamlit page configuration
st.set_page_config(
    page_title="Ollama PDF RAG Streamlit UI",
    page_icon="🎈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)

# Global ChromaDB client settings
CHROMA_SETTINGS = Settings(
    anonymized_telemetry=False,
    is_persistent=True,
    allow_reset=True
)

def get_chroma_client():
    """
    Get or create a ChromaDB client with consistent settings.
    """
    return chromadb.PersistentClient(
        path="chroma_db",
        settings=CHROMA_SETTINGS
    )

@st.cache_resource(show_spinner=True)
def extract_model_names(
    models_info: Dict[str, List[Dict[str, Any]]],
) -> Tuple[str, ...]:
    """
    Extract model names from the provided models information.
    """
    logger.info("Extracting model names from models_info")
    model_names = tuple(model["name"] for model in models_info["models"])
    logger.info(f"Extracted model names: {model_names}")
    return model_names

def extract_text_from_pdf(pdf_path: str) -> str:
    """
    Extract text from a PDF file using pdfplumber.
    """
    logger.info(f"Extracting text from PDF: {pdf_path}")
    text = ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text += page.extract_text() or ""
    return text

def create_vector_db(file_upload) -> Chroma:
    """
    Create a vector database from an uploaded PDF file with proper persistence.
    """
    logger.info(f"Creating vector DB from file upload: {file_upload.name}")
    
    # Clean up any existing database
    if os.path.exists("chroma_db"):
        try:
            shutil.rmtree("chroma_db")
            logger.info("Removed existing chroma_db")
            time.sleep(0.5)  # Give the system time to clean up
        except Exception as e:
            logger.warning(f"Could not remove existing directory: {e}")
    
    # Get a fresh client instance
    client = get_chroma_client()
    
    try:
        # Create temporary directory for PDF processing
        temp_dir = tempfile.mkdtemp()
        path = os.path.join(temp_dir, file_upload.name)
        
        with open(path, "wb") as f:
            f.write(file_upload.getvalue())
            logger.info(f"File saved to temporary path: {path}")
        
        # Extract text using pdfplumber
        text = extract_text_from_pdf(path)
        
        # Create a Document object
        doc = Document(page_content=text, metadata={"source": file_upload.name})
        
        # Split the document
        text_splitter = RecursiveCharacterTextSplitter(chunk_size=7500, chunk_overlap=100)
        chunks = text_splitter.split_documents([doc])
        logger.info("Document split into chunks")
        
        # Create embeddings
        embeddings = OllamaEmbeddings(model="nomic-embed-text", show_progress=True)
        
        # Create new vector store
        vector_db = Chroma.from_documents(
            documents=chunks,
            embedding=embeddings,
            collection_name="myRAG",
            persist_directory="chroma_db",
            client=client
        )
        
        # Explicitly persist the database
        vector_db.persist()
        logger.info("Vector DB created and persisted")
        
        return vector_db
        
    finally:
        # Clean up temporary PDF file
        try:
            shutil.rmtree(temp_dir)
            logger.info(f"Temporary directory {temp_dir} removed")
        except Exception as e:
            logger.warning(f"Error removing temp directory: {e}")

def process_question(question: str, vector_db: Chroma, selected_model: str) -> str:
    """
    Process a user question using the vector database and selected language model.
    """
    logger.info(f"Processing question: {question} using model: {selected_model}")
    
    # Use the new ChatOllama import
    llm = ChatOllama(model=selected_model, temperature=0)
    
    # Use the existing vector_db instance instead of creating a new one
    QUERY_PROMPT = PromptTemplate(
        input_variables=["question"],
        template="""You are an AI language model assistant. Your task is to generate 3
        different versions of the given user question to retrieve relevant documents from
        a vector database. By generating multiple perspectives on the user question, your
        goal is to help the user overcome some of the limitations of the distance-based
        similarity search. Provide these alternative questions separated by newlines.
        Original question: {question}""",
    )

    retriever = MultiQueryRetriever.from_llm(
        vector_db.as_retriever(), llm, prompt=QUERY_PROMPT
    )

    template = """Answer the question based ONLY on the following context:
    {context}
    Question: {question}
    If you don't know the answer, just say that you don't know, don't try to make up an answer.
    Only provide the answer from the {context}, nothing else.
    Add snippets of the context you used to answer the question.
    """

    prompt = ChatPromptTemplate.from_template(template)

    chain = (
        {"context": retriever, "question": RunnablePassthrough()}
        | prompt
        | llm
        | StrOutputParser()
    )

    response = chain.invoke(question)
    logger.info("Question processed and response generated")
    return response

@st.cache_data
def extract_all_pages_as_images(file_upload) -> List[Any]:
    """
    Extract all pages from a PDF file as images.
    """
    logger.info(f"Extracting all pages as images from file: {file_upload.name}")
    pdf_pages = []
    with pdfplumber.open(file_upload) as pdf:
        pdf_pages = [page.to_image().original for page in pdf.pages]
    logger.info("PDF pages extracted as images")
    return pdf_pages

def delete_vector_db() -> None:
    """
    Delete the vector database and clear related session state.
    """
    logger.info("Deleting vector DB")
    try:
        # Remove the persistence directory
        if os.path.exists("chroma_db"):
            try:
                # Get a fresh client instance
                client = get_chroma_client()
                
                # Delete the collection if it exists
                if "myRAG" in [col.name for col in client.list_collections()]:
                    client.delete_collection("myRAG")
                    logger.info("Deleted myRAG collection")
                
                # Remove the directory
                shutil.rmtree("chroma_db")
                logger.info("Removed chroma_db directory")
            except Exception as e:
                logger.error(f"Error cleaning up chroma_db: {e}")
                # On Windows, sometimes files are locked, so we need to wait
                time.sleep(2)
                try:
                    shutil.rmtree("chroma_db")
                    logger.info("Removed chroma_db directory after retry")
                except Exception as e2:
                    logger.error(f"Failed to remove chroma_db directory after retry: {e2}")
        
        # Clear session state
        st.session_state.pop("pdf_pages", None)
        st.session_state.pop("file_upload", None)
        st.session_state.pop("vector_db", None)
        
        st.success("Collection and temporary files deleted successfully.")
        logger.info("Vector DB and related session state cleared")
        
        # Rerun the app to reset the state
        time.sleep(1)  # Give a moment for cleanup
        st.rerun()
        
    except Exception as e:
        st.error(f"Error deleting vector DB: {e}")
        logger.error(f"Error deleting vector DB: {e}")

def main() -> None:
    """
    Main function to run the Streamlit application.
    """
    st.subheader("🧠 Ollama PDF RAG playground", divider="gray", anchor=False)

    models_info = ollama.list()
    available_models = extract_model_names(models_info)

    col1, col2 = st.columns([1.5, 2])

    if "messages" not in st.session_state:
        st.session_state["messages"] = []

    if "vector_db" not in st.session_state:
        st.session_state["vector_db"] = None

    if available_models:
        selected_model = col2.selectbox(
            "Pick a model available locally on your system ↓", available_models
        )

    file_upload = col1.file_uploader(
        "Upload a PDF file ↓", type="pdf", accept_multiple_files=False
    )

    if file_upload:
        st.session_state["file_upload"] = file_upload
        if st.session_state["vector_db"] is None:
            with st.spinner("Processing PDF..."):
                st.session_state["vector_db"] = create_vector_db(file_upload)
        
        pdf_pages = extract_all_pages_as_images(file_upload)
        st.session_state["pdf_pages"] = pdf_pages

        zoom_level = col1.slider(
            "Zoom Level", min_value=100, max_value=1000, value=700, step=50
        )

        with col1:
            with st.container(height=410, border=True):
                for page_image in pdf_pages:
                    st.image(page_image, width=zoom_level)

    delete_collection = col1.button("⚠️ Delete collection", type="secondary")

    if delete_collection:
        delete_vector_db()

    with col2:
        message_container = st.container(height=500, border=True)

        for message in st.session_state["messages"]:
            avatar = "🤖" if message["role"] == "assistant" else "😎"
            with message_container.chat_message(message["role"], avatar=avatar):
                st.markdown(message["content"])

        if prompt := st.chat_input("Enter a prompt here..."):
            try:
                st.session_state["messages"].append({"role": "user", "content": prompt})
                message_container.chat_message("user", avatar="😎").markdown(prompt)

                with message_container.chat_message("assistant", avatar="🤖"):
                    with st.spinner(":green[processing...]"):
                        if st.session_state["vector_db"] is not None:
                            response = process_question(
                                prompt, st.session_state["vector_db"], selected_model
                            )
                            st.markdown(response)
                        else:
                            st.warning("Please upload a PDF file first.")

                if st.session_state["vector_db"] is not None:
                    st.session_state["messages"].append(
                        {"role": "assistant", "content": response}
                    )

            except Exception as e:
                st.error(e, icon="⛔️")
                logger.error(f"Error processing prompt: {e}")
        else:
            if st.session_state["vector_db"] is None:
                st.warning("Upload a PDF file to begin chat...")

if __name__ == "__main__":
    main()