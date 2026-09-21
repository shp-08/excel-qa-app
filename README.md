# Ask your data files

Upload one or more **Excel or CSV** files, ask analytical questions in plain English, and get answers
that are **computed from the data** — including questions that need several sheets or files combined —
with a chart where the question calls for one.

**Live app:** _add the Streamlit link here after deploying_  ·  **Write-up:** [WRITEUP.md](WRITEUP.md)

## How it works

```
Excel / CSV files ──► clean + load every sheet into DuckDB ──► detect links between sheets and files
                                                                   │
question ──► (pick relevant sheets, only if there are many) ───────┤
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
                answer + chart / number tiles / table + the SQL and sheets it came from
```

The model never does arithmetic and never sees the full data. It writes a query; the database
computes the answer. If the data cannot answer a question, the app says so instead of guessing.

## Acceptance criteria

| Criterion | Where |
|---|---|
| Multi-file upload | Sidebar uploader takes several `.xlsx`, `.xls` and `.csv` files per session; every sheet becomes a table |
| Cross-file analysis | Links between files are detected from the data; totals, averages, filters, comparisons and trends across up to three files are covered by the test set |
| Visual insights | Bars for a category breakdown, a line for a trend, one line per group for a trend split by category, big number tiles for single values, a table otherwise |
| Delta on top of the AI | The table below |

## What is engineered on top of the model

| Problem | What the app does |
|---|---|
| Messy spreadsheets | Finds the real header row, drops empty rows/columns, fixes duplicate headers, converts `$1,200.50` and text dates to real types |
| Ambiguous dates | Day-first vs month-first is decided once per column, so `01/08/2024` and `22/08/2024` are never read differently |
| Cross-file questions | Detects foreign key → primary key links from the data itself (unique target, value containment, matching names for integer ids) and gives them to the model as join hints |
| Invented columns | Query is planned with `EXPLAIN` before running; the precise error goes back to the model for a retry |
| Invented joins | Every join condition is tested against the data; a join that matches no rows is rejected, so the user gets a refusal rather than a table of zeros |
| Misleading comparisons | Comparisons against a target use a percentage, not a raw difference; "which is highest" returns the top 5 so the ranking is visible |
| Unsafe SQL | DuckDB's own parser must report exactly one `SELECT`; file and network access are disabled on the connection |
| Runaway queries | 10 second timeout, 5,000 row cap |
| Empty results | One extra attempt with a hint to check filter values |
| Unanswerable questions | The model can refuse with a reason; greetings and "what can I ask?" get a conversational reply |
| Invented numbers in the summary | Every number in the sentence is checked against the result; mismatches are flagged |
| Many sheets | If the schema exceeds a token budget, a cheaper call first picks the relevant sheets (follow-up aware) |
| Free-tier rate limits | Tight token reservations, a second model for sheet picking, and automatic wait-and-continue shown in the UI |
| Trust | Every answer shows its SQL, the sheets used, retries and timing |

## Tech stack

Python 3.11 · Streamlit (UI) · DuckDB (in-process analytical SQL) · pandas + openpyxl (file reading) ·
Altair (charts) · OpenAI-compatible client. No LangChain or agent framework: a few small prompts and a plain client.

**Models** — open weights only, served through any OpenAI-compatible endpoint:

- `qwen/qwen3.8-27b` (Apache 2.0) writes SQL and words answers
- `openai/gpt-oss-20b` (Apache 2.0) picks relevant sheets and suggests questions

Defaults point at Groq's free tier. To run fully local, set `LLM_BASE_URL=http://localhost:11434/v1`
and an Ollama model name in `.env`.

## Run locally

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env            # then put your Groq key in .env  (console.groq.com, free)
streamlit run app.py
```

Press **Try with sample data** in the sidebar to load the files in `samples/`: three Excel workbooks
and one CSV that link to each other.

## Tests

```bash
python scripts/check_file_loading.py        # what the loader makes of the messy sample files
python scripts/check_question_pipeline.py   # 23 checks on the guards and retry loop, using a scripted fake model (no API key needed)
python scripts/ask_in_terminal.py           # real model, real questions, from the terminal
python scripts/generate_sample_data.py      # rebuilds the files in samples/
```

## Layout

```
app.py                          the Streamlit page (drawing only, no logic)
excel_qa/
  file_loader.py                Excel / CSV → cleaned tables in DuckDB
  link_detector.py              finds which columns connect sheets and files
  schema_prompt.py              describes the tables as text for the model
  prompts.py                    every instruction given to the model, in one place
  llm_client.py                 OpenAI-compatible client, rate-limit waiting
  sql_checks.py                 validates the model's SQL, then runs it with limits
  question_pipeline.py          the main loop: pick tables → write SQL → check → run → retry
  answer_writer.py              words the answer from the result, suggests questions
  charts.py                     when to chart a result, and number formatting
  logger.py                     step-by-step log in the terminal
scripts/                        sample data generator and checks (see Tests)
samples/                        three linked Excel workbooks and one CSV
```

## Limits and what I would do next

- Data lives in memory per browser tab: a refresh clears it. Next: persist a DuckDB file per session.
- Sheet picking uses a model call; past a few hundred sheets, switch to embedding-based schema retrieval.
- Header detection handles title rows and blanks, not merged multi-row headers or several tables on one sheet.
- Numbers are read in US/UK/Indian style (`1,200.50`). European style (`1.200,50`) is ambiguous and is not detected.
- Link detection by name assumes English-style key names for integer ids; text ids are matched by value in any language.
- Keep the scored question set in the repo and run it on every prompt or model change.
