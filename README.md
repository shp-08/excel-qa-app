# Ask your Excel files

Upload one or more Excel workbooks, ask analytical questions in plain English, and get answers
that are **computed from the data**, including questions that need several sheets or files combined.

**Live app:** _add the Streamlit link here after deploying_

## How it works

```
Excel files ──► clean + load every sheet into DuckDB ──► detect links between sheets
                                                              │
question ──► (pick relevant sheets, only if there are many) ──┤
                                                              ▼
                      open-weight LLM writes ONE SQL query from schema + sample rows
                                                              │
                 checks: SELECT only · columns exist · every join matches real rows
                                                              │  error? send it back, retry (max 2)
                                                              ▼
                              DuckDB runs the query (timeout, row cap)
                                                              │
                 LLM words the answer from the RESULT ROWS · numbers are cross-checked
                                                              ▼
                  answer + chart/table + the SQL and sheets it came from
```

The model never does arithmetic and never sees the full data. It writes a query; the database
computes the answer. If the data cannot answer a question, the app says so instead of guessing.

## What is engineered on top of the model

| Problem | What the app does |
|---|---|
| Messy spreadsheets | Finds the real header row, drops empty rows/columns, fixes duplicate headers, converts `$1,200.50` and text dates to real types |
| Cross-file questions | Detects foreign key → primary key links from the data itself (unique target, value containment, matching names for integer ids) and gives them to the model as join hints |
| Invented columns | Query is planned with `EXPLAIN` before running; the precise error goes back to the model for a retry |
| Invented joins | Every join condition is tested against the data; a join that matches no rows is rejected, so the user gets a refusal rather than a table of zeros |
| Unsafe SQL | DuckDB's own parser must report exactly one `SELECT`; file and network access are disabled on the connection |
| Runaway queries | 10 second timeout, 5,000 row cap |
| Empty results | One extra attempt with a hint to check filter values |
| Unanswerable questions | The model can refuse with a reason; greetings and "what can I ask?" get a conversational reply |
| Invented numbers in the summary | Every number in the sentence is checked against the result; mismatches are flagged |
| Many sheets | If the schema exceeds a token budget, a cheaper call first picks the relevant sheets (follow-up aware) |
| Free-tier rate limits | Tight token reservations, a second model for sheet picking, and automatic wait-and-continue shown in the UI |
| Trust | Every answer shows its SQL, the sheets used, retries and timing |

## Models

Open-weight models only, served through any OpenAI-compatible endpoint:

- `qwen/qwen3.8-27b` (Apache 2.0) writes SQL and words answers
- `openai/gpt-oss-20b` (Apache 2.0) picks relevant sheets and suggests questions

Defaults point at Groq. To run fully local, set `LLM_BASE_URL=http://localhost:11434/v1` and an
Ollama model name in `.env`. No LangChain or agent framework: three small prompts and a plain client.

## Run locally

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env            # then put your Groq key in .env  (console.groq.com, free)
streamlit run app.py
```

Press **Try with sample data** in the sidebar to load the three workbooks in `samples/`.

## Tests

```bash
python scripts/check_ingest.py    # what ingestion makes of the messy sample files
python scripts/check_query.py     # guards and retry loop, with a scripted fake model (no API key needed)
python scripts/ask.py             # real model, real questions, from the terminal
```

## Layout

```
app.py            Streamlit UI only
core/ingest.py    Excel → DuckDB, type cleaning, link detection
core/schema.py    turns loaded tables into prompt text
core/query.py     select sheets → write SQL → check → run → retry → narrate
core/llm.py       OpenAI-compatible client, rate-limit handling
scripts/          sample data generator and checks
```

## Limits and what I would do next

- Data lives in memory per browser tab: a refresh clears it. Next: persist a DuckDB file per session.
- Sheet picking uses a model call; past a few hundred sheets, switch to embedding-based schema retrieval.
- Header detection handles title rows and blanks, not merged multi-row headers or several tables on one sheet.
- A scored evaluation set (question → expected answer) to measure accuracy when changing prompts or models.
- Charts cover one label + one measure; multi-series results fall back to the table.
