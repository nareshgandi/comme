-- Enable the extension (once per database)
CREATE EXTENSION IF NOT EXISTS vector;

-- Confirm version
SELECT extversion FROM pg_extension WHERE extname = 'vector';

# pgvector Session: From Hardcoded Vectors to RAG

## Part 1 — Setup

```sql
-- Enable the extension (once per database)
CREATE EXTENSION IF NOT EXISTS vector;

-- Confirm version
SELECT extversion FROM pg_extension WHERE extname = 'vector';
```

---

## Part 2 — Hardcoded 3-Dimension Embeddings

Start simple so students can *see* the math. 3D vectors are easy to reason
about — think x/y/z coordinates in space, where "close together" = "similar".

```sql
DROP TABLE IF EXISTS items_3d;

CREATE TABLE items_3d (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    embedding   VECTOR(3)
);

INSERT INTO items_3d (name, embedding) VALUES
    ('apple',      '[0.9, 0.1, 0.0]'),
    ('banana',     '[0.8, 0.2, 0.0]'),
    ('mango',      '[0.85, 0.15, 0.05]'),
    ('car',        '[0.0, 0.9, 0.1]'),
    ('truck',      '[0.05, 0.85, 0.1]'),
    ('bicycle',    '[0.1, 0.8, 0.2]'),
    ('laptop',     '[0.0, 0.1, 0.9]'),
    ('smartphone', '[0.05, 0.15, 0.85]');
```

Notice the pattern: fruits cluster near `[high, low, low]`, vehicles near
`[low, high, low]`, electronics near `[low, low, high]`. That clustering
*is* the whole idea behind embeddings — this is just done by hand instead
of by a model.

### Similarity search — the three operators

pgvector gives you three distance operators:

| Operator | Meaning              | Use case                          |
|----------|-----------------------|------------------------------------|
| `<->`    | Euclidean (L2) distance | General purpose, smaller = closer |
| `<#>`    | Negative inner product  | Returns negative numbers; smaller (more negative) = closer |
| `<=>`    | Cosine distance         | Direction matters more than magnitude; most common for text embeddings |

```sql
-- Find items most similar to 'apple' using Euclidean distance
SELECT name, embedding <-> (SELECT embedding FROM items_3d WHERE name = 'apple') AS distance
FROM items_3d
ORDER BY distance
LIMIT 5;

-- Same query using cosine distance
SELECT name, embedding <=> (SELECT embedding FROM items_3d WHERE name = 'apple') AS distance
FROM items_3d
ORDER BY distance
LIMIT 5;

-- Query with a fresh, ad-hoc vector (like a new item a user just typed in)
SELECT name, embedding <-> '[0.88, 0.12, 0.02]' AS distance
FROM items_3d
ORDER BY distance
LIMIT 3;
```

**Expected output** (Euclidean, querying against 'apple'):

```
   name    | distance
-----------+----------
 apple     |     0
 mango     |  0.0707
 banana    |  0.1414
 bicycle   |  1.1225
 ...
```

Fruits come back first, vehicles and electronics trail behind — exactly
what you'd expect from the hand-picked coordinates.

### Production Gotcha ⚠️

`<->`, `<#>`, `<=>` only use an index (IVFFlat / HNSW) if the index was
built with the **matching** distance function (`vector_l2_ops`,
`vector_ip_ops`, `vector_cosine_ops`). Mixing operator and index type
silently falls back to a sequential scan — no error, just a slow query on
a big table. Always match the operator you query with to the `ops` class
you indexed with.

---

## Part 3 — Real Embeddings at Higher Dimensions

3D was for intuition. Real models output much higher dimensions —
`all-MiniLM-L6-v2` gives 384, OpenAI's `text-embedding-3-small` gives
1536. Here's a self-contained Python script using a free local model
(`sentence-transformers`), so no API key is needed for the demo.

```bash
pip install sentence-transformers psycopg2-binary
```

```python
# generate_embeddings.py
from sentence_transformers import SentenceTransformer
import psycopg2

model = SentenceTransformer("all-MiniLM-L6-v2")  # 384 dimensions

documents = [
    "The apple is a sweet, crisp fruit that grows on trees.",
    "Bananas are a curved yellow fruit rich in potassium.",
    "A mango is a tropical stone fruit, sweet and juicy.",
    "Cars are four-wheeled vehicles used for road transport.",
    "Trucks carry heavy cargo over long distances.",
    "A bicycle is a human-powered two-wheeled vehicle.",
    "Laptops are portable computers used for work and study.",
    "A smartphone is a mobile device with internet access.",
]

embeddings = model.encode(documents)  # shape: (8, 384)

conn = psycopg2.connect(
    dbname="postgres", user="postgres", password="postgres",
    host="localhost", port=5432
)
cur = conn.cursor()

cur.execute("""
    DROP TABLE IF EXISTS documents;
    CREATE TABLE documents (
        id        SERIAL PRIMARY KEY,
        content   TEXT NOT NULL,
        embedding VECTOR(384)
    );
""")

for doc, emb in zip(documents, embeddings):
    cur.execute(
        "INSERT INTO documents (content, embedding) VALUES (%s, %s)",
        (doc, emb.tolist())
    )

conn.commit()
cur.close()
conn.close()
print(f"Inserted {len(documents)} documents with {len(embeddings[0])}-dim embeddings.")
```

Once loaded, an index is worth building at this scale:

```sql
-- HNSW: better recall/speed tradeoff than IVFFlat for most workloads
CREATE INDEX ON documents
USING hnsw (embedding vector_cosine_ops);
```

Querying now means embedding the *question* with the same model, then
running the same `<=>` pattern:

```python
# search.py
from sentence_transformers import SentenceTransformer
import psycopg2

model = SentenceTransformer("all-MiniLM-L6-v2")
query = "What fruit is yellow and curved?"
query_embedding = model.encode(query).tolist()

conn = psycopg2.connect(
    dbname="postgres", user="postgres", password="postgres",
    host="localhost", port=5432
)
cur = conn.cursor()
cur.execute("""
    SELECT content, embedding <=> %s::vector AS distance
    FROM documents
    ORDER BY distance
    LIMIT 3
""", (query_embedding,))

for content, distance in cur.fetchall():
    print(f"{distance:.4f}  {content}")
```

### Production Gotcha ⚠️

The dimension in `VECTOR(384)` is fixed at table-creation time. Swap
embedding models later (e.g., MiniLM → OpenAI's 1536-dim model) and every
insert will fail with a dimension mismatch until you `ALTER` the column
type and re-embed the whole table. Pin the model version in your app
config, not just in your head.

---

## Part 4 — RAG (Retrieval-Augmented Generation)

RAG = pgvector finds the relevant chunks, an LLM writes the answer using
only those chunks as context.

```python
# rag.py
from sentence_transformers import SentenceTransformer
import psycopg2
import anthropic

embed_model = SentenceTransformer("all-MiniLM-L6-v2")
llm = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

def retrieve(question, k=3):
    query_embedding = embed_model.encode(question).tolist()
    conn = psycopg2.connect(
        dbname="postgres", user="postgres", password="postgres",
        host="localhost", port=5432
    )
    cur = conn.cursor()
    cur.execute("""
        SELECT content, embedding <=> %s::vector AS distance
        FROM documents
        ORDER BY distance
        LIMIT %s
    """, (query_embedding, k))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [content for content, _ in rows]

def answer(question):
    context_chunks = retrieve(question)
    context = "\n".join(f"- {c}" for c in context_chunks)

    prompt = f"""Answer the question using only the context below.
If the context doesn't contain the answer, say so.

Context:
{context}

Question: {question}"""

    response = llm.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text

if __name__ == "__main__":
    print(answer("What device do I carry to browse the internet on the go?"))
```

That's the full loop: embed → store → retrieve by distance → stuff into
prompt → generate.

### Production Gotcha ⚠️

Naive "top-k by cosine distance" retrieval breaks down once a table has
tens of thousands of rows with near-duplicate content — you'll retrieve
three near-identical chunks instead of three *diverse* ones. At that
scale, pair pgvector with a re-ranking step or add a `WHERE` filter on
metadata (category, date, source) before the `ORDER BY <=>` to narrow the
candidate set first.

---

## Part 5 — Running Ollama as a Backend Service

Ollama serves local LLMs over an HTTP API so your Python script never
needs a cloud API key. Install it, pull a small ("mini") model, and run
it as a persistent background service — not a one-off terminal command.

```bash
# Install (Linux)
curl -fsSL https://ollama.com/install.sh | sh

# Pull a small model — good enough for a RAG demo, runs on CPU
ollama pull llama3.2:1b
```

### Run it as a systemd service (recommended for a session/demo box)

```bash
sudo tee /etc/systemd/system/ollama.service > /dev/null <<'EOF'
[Unit]
Description=Ollama Service
After=network-online.target

[Service]
ExecStart=/usr/local/bin/ollama serve
User=ollama
Group=ollama
Restart=always
RestartSec=3
Environment="OLLAMA_HOST=0.0.0.0:11434"

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ollama
sudo systemctl status ollama
```

If you'd rather not set up a systemd unit for a quick demo, `ollama serve`
in a terminal (or `nohup ollama serve &`) does the same job — it just
won't survive a reboot or terminal close.

### Verify the service is up

```bash
curl http://localhost:11434/api/tags
```

You should see `llama3.2:1b` listed. This is the same endpoint your
Python code will call — no SDK needed, plain HTTP.

### Production Gotcha ⚠️

`ollama pull` and `ollama serve` compete for the same port/model cache by
default. If you containerize this, mount a persistent volume for
`~/.ollama/models` — otherwise every container restart re-downloads the
model, which is a multi-GB surprise on a slow session-day network.

---

## Part 6 — RAG with Ollama: Accepts Any Document

This script takes a document path as input, chunks it, embeds each chunk
with `sentence-transformers`, stores the chunks in pgvector, then answers
a question using Ollama (running as the service above) instead of a
cloud API.

```bash
pip install sentence-transformers psycopg2-binary requests
```

```python
# rag_ollama.py
import sys
import textwrap
import requests
import psycopg2
from sentence_transformers import SentenceTransformer

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3.2:1b"
EMBED_MODEL = SentenceTransformer("all-MiniLM-L6-v2")  # 384 dims
CHUNK_SIZE = 500  # characters per chunk

DB_CONFIG = dict(
    dbname="postgres", user="postgres", password="postgres",
    host="localhost", port=5432
)


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


def setup_table():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS doc_chunks (
            id        SERIAL PRIMARY KEY,
            source    TEXT NOT NULL,
            content   TEXT NOT NULL,
            embedding VECTOR(384)
        );
    """)
    conn.commit()
    cur.close()
    conn.close()


def ingest_document(filepath):
    """Read a document, chunk it, embed each chunk, store in pgvector."""
    with open(filepath, "r", encoding="utf-8") as f:
        text = f.read()

    chunks = textwrap.wrap(text, CHUNK_SIZE)
    embeddings = EMBED_MODEL.encode(chunks)

    conn = get_conn()
    cur = conn.cursor()
    for chunk, emb in zip(chunks, embeddings):
        cur.execute(
            "INSERT INTO doc_chunks (source, content, embedding) VALUES (%s, %s, %s)",
            (filepath, chunk, emb.tolist())
        )
    conn.commit()
    cur.close()
    conn.close()
    print(f"Ingested {len(chunks)} chunks from {filepath}")


def retrieve(question, k=3):
    query_embedding = EMBED_MODEL.encode(question).tolist()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT content, embedding <=> %s::vector AS distance
        FROM doc_chunks
        ORDER BY distance
        LIMIT %s
    """, (query_embedding, k))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [content for content, _ in rows]


def ask_ollama(prompt):
    """Call the locally-running Ollama service (non-streaming)."""
    response = requests.post(OLLAMA_URL, json={
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False
    })
    response.raise_for_status()
    return response.json()["response"]


def answer(question, k=3):
    context_chunks = retrieve(question, k)
    context = "\n---\n".join(context_chunks)

    prompt = f"""Answer the question using only the context below.
If the context doesn't contain the answer, say so.

Context:
{context}

Question: {question}
Answer:"""

    return ask_ollama(prompt)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python rag_ollama.py <path-to-document> [question]")
        sys.exit(1)

    doc_path = sys.argv[1]
    setup_table()
    ingest_document(doc_path)

    question = sys.argv[2] if len(sys.argv) > 2 else input("Ask a question: ")
    print("\n--- Answer ---")
    print(answer(question))
```

Run it against any text file:

```bash
python rag_ollama.py notes.txt "What does the document say about backups?"
```

### Production Gotcha ⚠️

`ollama serve`'s default context window is small (2K–4K tokens depending
on model). Stuff too many retrieved chunks into the prompt and Ollama
silently truncates the context rather than erroring — the model then
"hallucinates" an answer from a partial context. Keep `k` low (3–5
chunks) or check the model's `num_ctx` setting before assuming the whole
context you built made it into the prompt.

---

## Key Takeaways

- `VECTOR(n)` is just a fixed-length array with distance operators built in.
- `<->` Euclidean, `<#>` inner product, `<=>` cosine — pick the one that matches your embedding model's training objective (most text models: cosine).
- Index type (`hnsw`/`ivfflat`) must be built with the same `ops` class as the operator used in queries, or it won't be used.
- Real embeddings are just `VECTOR(3)` scaled up to 384/1536/etc — same SQL, bigger numbers.
- RAG is retrieval (pgvector) + generation (LLM) — pgvector never generates text, it only finds nearby rows.
- Ollama swaps the generation step to a local model over plain HTTP — same RAG pattern, no API key, run it as a systemd service so it survives reboots.
- Chunk size and `k` (chunks retrieved) both need to respect the LLM's context window — pgvector will happily return more context than the model can actually use.
