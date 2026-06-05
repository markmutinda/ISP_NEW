import os
from dotenv import load_dotenv
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import logging
import pandas as pd
from langchain_groq import ChatGroq
from langchain_community.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings
from langchain.text_splitter import CharacterTextSplitter
from langchain.chains import RetrievalQA

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)

# Twilio credentials
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")

# Groq API key
groq_key = os.getenv("API_KEY")

# === LangChain setup ===
def get_text_from_excel(filepath):
    df = pd.read_excel(filepath, engine="openpyxl")
    return "\n".join(df.astype(str).apply(lambda row: " | ".join(row), axis=1).tolist())

excel_path = "products.xlsx"
raw_text = get_text_from_excel(excel_path)

text_splitter = CharacterTextSplitter(chunk_size=500, chunk_overlap=50)
docs = text_splitter.create_documents([raw_text])
embeddings = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")

index_path = "faiss_index"
if os.path.exists(index_path):
    vectorstore = FAISS.load_local(index_path, embeddings, allow_dangerous_deserialization=True)
    logging.info("Loaded existing FAISS index.")
else:
    vectorstore = FAISS.from_documents(docs, embeddings)
    vectorstore.save_local(index_path)
    logging.info("Created new FAISS index.")

llm = ChatGroq(
    api_key=groq_key,
    model="llama3-8b-8192",
    temperature=0,
    max_tokens=2048,
)

qa = RetrievalQA.from_chain_type(
    llm=llm,
    retriever=vectorstore.as_retriever(),
    return_source_documents=True,
)

# === Flask app ===
app = Flask(__name__)

@app.route("/hello")
def hello_world():
    return "Hello, World!"

@app.route("/whatsapp", methods=["POST"])
def whatsapp_reply():
    incoming_msg = request.values.get("Body", "").strip()
    from_number = request.values.get("From", "")

    logging.info(f"Incoming message from {from_number}: {incoming_msg}")

    resp = MessagingResponse()

    if not incoming_msg:
        logging.warning("No message received.")
        resp.message("Sorry, I didn't receive any message. Please send a question.")
        return str(resp)

    try:
        response = qa.invoke(incoming_msg)
        answer = response.get("result", "Sorry, I couldn't find an answer to that.")
        logging.info(f"Answer sent: {answer}")
    except Exception as e:
        logging.error(f"Error during QA processing: {e}")
        answer = "Oops! Something went wrong while fetching your answer. Please try again."

    resp.message(answer)
    return str(resp)

if __name__ == "__main__":
    logging.info("Starting Flask server on port 5000...")
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
