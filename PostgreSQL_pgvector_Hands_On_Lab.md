
# PostgreSQL pgvector Hands-on Lab

## Objective

In this lab we learn how PostgreSQL performs semantic search using **pgvector**. We begin with manually created vectors, move to AI-generated embeddings, improve performance with **HNSW**, and finally build the **Retrieval** part of a RAG pipeline.

---

# Learning Outcomes

- Understand vectors and embeddings.
- Perform semantic similarity searches.
- Generate embeddings using Sentence Transformers.
- Analyze execution plans.
- Create HNSW indexes.
- Build a simple knowledge base.
- Understand Retrieval, Augmentation and Generation.

---

# Step 1 – Understanding Vectors

### Why?

PostgreSQL does not understand words such as *Apple* or *Tesla*. It only compares numbers. We therefore start with a simple 3-dimensional vector so the mathematics is easy to visualize.

## Install pgvector

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

`pgvector` adds:
- vector datatype
- similarity operators
- HNSW and IVFFlat indexes

## Create Demo Table

```sql
DROP TABLE IF EXISTS knowledge;

CREATE TABLE knowledge
(
    id SERIAL PRIMARY KEY,
    text TEXT,
    embedding vector(3)
);
```

## Insert Hardcoded Embeddings

```sql
INSERT INTO knowledge(text, embedding)
VALUES
('Apple is a fruit','[0.95,0.10,0.10]'),
('Mango is a fruit','[0.96,0.12,0.08]'),
('Banana is a fruit','[0.94,0.09,0.11]'),
('Tesla is a car','[0.10,0.95,0.10]'),
('BMW is a car','[0.08,0.96,0.12]'),
('Audi is a car','[0.12,0.94,0.09]'),
('Dog is an animal','[0.10,0.10,0.95]'),
('Cat is an animal','[0.12,0.08,0.96]'),
('Tiger is an animal','[0.09,0.11,0.94]');
```

These vectors are manually created only for teaching.

## Similarity Search

### Fruits

```sql
SELECT text,
       embedding <=> '[0.97,0.11,0.09]' AS cosine_distance
FROM knowledge
ORDER BY embedding <=> '[0.97,0.11,0.09]';
```

Expected: Apple, Mango, Banana

### Cars

```sql
SELECT text,
       embedding <=> '[0.09,0.96,0.11]' AS cosine_distance
FROM knowledge
ORDER BY embedding <=> '[0.09,0.96,0.11]';
```

### Animals

```sql
SELECT text,
       embedding <=> '[0.11,0.09,0.97]' AS cosine_distance
FROM knowledge
ORDER BY embedding <=> '[0.11,0.09,0.97]';
```

**Takeaway:** Smaller cosine distance means more similar vectors.

---

# Step 2 – Real Embeddings

Manual vectors are impossible in production. An embedding model converts text into 384-dimensional vectors automatically.

```sql
DROP TABLE IF EXISTS knowledge;

CREATE TABLE knowledge
(
    id SERIAL PRIMARY KEY,
    text TEXT,
    embedding vector(384)
);
```

Load embeddings:

```bash
python3.9 importdata.py
```

Verify:

```sql
SELECT id,text,LEFT(embedding::text,80)
FROM knowledge;
```

Search:

```bash
python3.9 search.py
```

**Takeaway:** PostgreSQL stores vectors; the embedding model understands language.

---

# Step 3 – Large Dataset

Generate a larger dataset for performance testing.

```bash
python3.9 bulk_load_embeddings.py
```

Generate a query vector:

```bash
python3.9 generate_embedding.py
```

Execution plan:

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT
    text,
    embedding <=> '[PASTE_VECTOR]'::vector AS distance
FROM knowledge
ORDER BY embedding <=> '[PASTE_VECTOR]'::vector
LIMIT 5;
```

Observe Sequential Scan and execution time.

---

# Step 4 – HNSW Index

```sql
CREATE INDEX idx_hnsw
ON knowledge
USING hnsw (embedding vector_cosine_ops);
```

Run the same search again.

Observe improved execution time and index usage on large datasets.

**HNSW** = Hierarchical Navigable Small World, an Approximate Nearest Neighbor (ANN) index for fast vector search.

---

# Step 5 – Building a Knowledge Base (Retrieval)

```sql
DROP TABLE IF EXISTS knowledge_base;

CREATE TABLE knowledge_base
(
    id SERIAL PRIMARY KEY,
    title TEXT,
    content TEXT,
    embedding vector(384)
);
```

Load documents:

```bash
python3.9 load_documents.py
```

Retrieve relevant documents:

```bash
python3.9 retrieve.py
```

Ask:

```
How do I speed up vector search?
```

PostgreSQL returns the most relevant documents. This is **Retrieval**.

---

# Understanding RAG

```
Documents
    ↓
Embedding Model
    ↓
PostgreSQL + pgvector
    ↓
Top Matching Documents (Retrieval)
    ↓
Prompt Construction (Augmentation)
    ↓
LLM (Generation)
    ↓
Answer
```

- **Retrieval**: Find relevant documents.
- **Augmentation**: Add retrieved context to the user's prompt.
- **Generation**: An LLM creates the final answer.

---

# Terminology

| Term | Meaning |
|------|---------|
| Vector | Numerical representation of text |
| Embedding | AI-generated vector |
| Semantic Search | Search by meaning |
| Cosine Distance | Lower means more similar |
| HNSW | Approximate nearest-neighbor index |
| Retrieval | Finding relevant documents |
| Augmentation | Attaching retrieved context |
| Generation | LLM produces the answer |
| RAG | Retrieval + Augmentation + Generation |

---

# Final Takeaways

- pgvector extends PostgreSQL with vector search.
- Embeddings represent meaning numerically.
- HNSW accelerates similarity searches.
- PostgreSQL provides the Retrieval layer of a RAG architecture.
