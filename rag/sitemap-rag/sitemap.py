import os
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, session
from uuid import uuid4

from langchain_groq import ChatGroq
from langchain_community.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings
from langchain.text_splitter import CharacterTextSplitter
from langchain.prompts import PromptTemplate
from langchain.chains import ConversationalRetrievalChain
from langchain.memory import ConversationBufferMemory

# Load environment variables
load_dotenv()
groq_key = os.getenv("API_KEY")
flask_secret = os.getenv("FLASK_SECRET_KEY")

app = Flask(__name__)
app.secret_key = flask_secret

# === Step 1: Get content from sitemap ===

def fetch_urls_from_sitemap(sitemap_url):
    response = requests.get(sitemap_url)
    soup = BeautifulSoup(response.content, 'xml')
    return [loc.text for loc in soup.find_all('loc')]

def extract_text_from_url(url):
    try:
        res = requests.get(url, timeout=10)
        soup = BeautifulSoup(res.text, 'html.parser')
        return soup.get_text(separator=" ", strip=True)
    except Exception as e:
        print(f"Failed to fetch {url}: {e}")
        return ""

sitemap_url = "https://www.imbankgroup.com/ke/page-sitemap.xml"
urls = fetch_urls_from_sitemap(sitemap_url)

pages_text = []
for url in urls:
    text = extract_text_from_url(url)
    if text:
        pages_text.append(text)

raw_text = "\n\n".join(pages_text)

# === Step 2: Vector Store ===

text_splitter = CharacterTextSplitter(chunk_size=300, chunk_overlap=30)
docs = text_splitter.create_documents([raw_text])

embeddings = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")

index_path = "im_bank"
if os.path.exists(index_path):
    vectorstore = FAISS.load_local(index_path, embeddings, allow_dangerous_deserialization=True)
    print("Loaded existing FAISS index")
else:
    vectorstore = FAISS.from_documents(docs, embeddings)
    vectorstore.save_local(index_path)
    print("Created new FAISS index")

# === Step 3: Prompt & LLM ===

prompt_template = PromptTemplate(
    input_variables=["context", "question"],
    template="""
You are Sallie, an AI assistant who responds with a casual and very welcoming tone. 
Avoid repeating your name or introduction in every response. Just focus on giving helpful, friendly answers that feel human and natural.

Use the following context to answer the user's question conversationally.

Context:
{context}

User's Question:
{question}

Sallie's Response:"""
)

llm = ChatGroq(
    api_key=groq_key,
    model="meta-llama/llama-4-scout-17b-16e-instruct",
    temperature=0.7,
    max_tokens=2048,
)

# === Step 4: Memory Sessions ===

memories = {}

def get_memory_for_session():
    session_id = session.get("session_id")
    if not session_id:
        session_id = str(uuid4())
        session["session_id"] = session_id
    if session_id not in memories:
        memories[session_id] = ConversationBufferMemory(memory_key="chat_history", return_messages=True)
    return memories[session_id]

# === Step 5: Flask Routes ===

@app.route("/", methods=["GET", "POST"])
def home():
    memory = get_memory_for_session()
    qa = ConversationalRetrievalChain.from_llm(
        llm=llm,
        retriever=vectorstore.as_retriever(search_kwargs={"k": 4}),
        memory=memory,
        combine_docs_chain_kwargs={"prompt": prompt_template}
    )

    if request.method == "POST":
        question = request.form.get("question", "")
        if question:
            result = qa({"question": question})
            answer = result["answer"]
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"answer": answer})
            return render_template("index.html", answer=answer, question=question)
        else:
            return render_template("index.html", answer=None, question="")
    else:
        return render_template("index.html", answer=None, question="")

if __name__ == "__main__":
    app.run(debug=True)
