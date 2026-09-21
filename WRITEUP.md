# AI-Powered Data Q&A — Write-up

**Live app:** _link_ · **Repo:** https://github.com/shp-08/excel-qa-app

## Approach

The brief asks for *correct* answers, so the central decision was: **the model never calculates.** An
open-weight LLM reads the schema plus three sample rows per sheet and writes one SQL query; DuckDB runs
it; the model words the answer from the result rows. Every uploaded sheet (Excel or CSV) becomes a table
in one in-memory DuckDB per session, so a cross-file question is simply a SQL join. I scoped to what the
criteria name and spent the remaining time on reliability rather than features.

## Key decisions

- **Text-to-SQL, not RAG or "data in the prompt".** Retrieval returns similar rows, so a total would be an
  estimate from a sample. SQL is exact, scales past the context window, and can be shown to the user.
- **DuckDB.** In-process (nothing to host), columnar, reads DataFrames directly, Postgres-like dialect.
- **Open models, provider-neutral.** Qwen 3 27B writes SQL, GPT-OSS 20B picks tables, via an
  OpenAI-compatible client on Groq's free tier; two env vars switch to local Ollama. No LangChain.
- **Streamlit** for a small working app; all logic sits in plain modules a FastAPI front end could reuse.

## Delta on top of the AI

Each item came from a real failure I hit while testing:

- **Link detection from data** (unique target + value containment + name agreement for integer ids). My
  first version compared values only and found 2,610 "links" in a real 22-sheet workbook; this finds the 77 real ones.
- **Join verification.** Asked for "orders per employee" when no such column existed, the model joined
  employee id to customer id and returned a tidy table of zeros. Every join is now tested against the
  data; an empty one is rejected and the model refuses instead.
- **SQL guardrails.** DuckDB's parser must report exactly one SELECT; `EXPLAIN` catches invented columns
  and the precise error is fed back for up to two retries; timeout, row cap, file access disabled.
- **Honest analytics.** "Best versus target" first ranked by raw difference, which favours small groups;
  rules now force percentages and return the top 5 so the ranking is visible.
- **Messy input.** Header-row detection, `$1,200` → number, and day-first vs month-first decided per
  column — per-value parsing had silently turned 1 August into 8 January.
- **Trust.** Refusals with a reason; every number in the answer sentence is checked against the result
  and the sentence rewritten if misquoted (it caught 3,205 written as 2,005); SQL and sheets shown.

## Testing

23 automated checks force each failure case with a scripted fake model. **50 questions** were run against
the real model, each compared with an answer computed independently in pandas: **50 / 50 correct, all
first attempt** (single file, two and three files, CSV + Excel, trends, comparisons, refusals). The UI was
checked in a real browser. A perfect score on my own data is a floor, not a guarantee: every bug above
came from data or phrasing I had not yet tried.

## What I would build next

Persisted sessions; embedding-based schema retrieval beyond a few hundred sheets; merged headers and
several tables per sheet; European number formats; the 50-question set as a CI gate for prompt or model
changes; a clarifying question when a request is ambiguous instead of silently picking one reading.
