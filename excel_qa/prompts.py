"""Every instruction the app gives to the model, in one place."""

# The model starts its reply with one of these when it is not writing SQL.
CANNOT_ANSWER_MARKER = "CANNOT_ANSWER:"
CHAT_MARKER = "CHAT:"

MAX_PICKED_TABLES = 6

PICK_TABLES_PROMPT = f"""You pick which database tables are needed to answer a question.
You get a list of tables with their columns, and a question.
Reply with the table names only, comma separated, nothing else.
Two exceptions:
- The message is a greeting, thanks, or asks what data is loaded or what can be
  asked. Reply with:
  {CHAT_MARKER} <a short friendly reply. For a greeting or "what can I ask", say in
  plain words what data is loaded and suggest 3 example questions as a bullet list.>
- The message asks for something that none of these tables could contain. Reply with:
  {CANNOT_ANSWER_MARKER} <one sentence explaining what is missing>
Include every table the query must read, including a table needed only to join
two others. Do not add tables just in case: most questions need 1 to 3.
Maximum {MAX_PICKED_TABLES} tables."""

WRITE_SQL_PROMPT = f"""You write DuckDB SQL to answer questions about tables loaded from Excel files.

You reply in exactly one of three ways.

1. The message is a greeting, thanks, or asks what data is loaded or what can be
   asked. Do NOT write SQL. Reply with:
   {CHAT_MARKER} <a short friendly reply. For a greeting or "what can I ask", say in
   plain words what data is loaded and suggest 3 example questions as a bullet list.
   Otherwise just answer briefly, without suggestions.>

2. The message is a data question but the data cannot answer it (the needed
   column or table does not exist, or it is not about this data). Reply with:
   {CANNOT_ANSWER_MARKER} <one sentence explaining what is missing>

3. Otherwise, write SQL.

Rules for SQL:
- Reply with ONE SELECT statement inside a ```sql code block, and nothing else.
- Use only the tables and columns listed in the schema. Never invent names.
- Text values in filters must match the sample rows exactly (case, spelling).
  For user-typed text prefer case-insensitive matching with ILIKE or lower().
- To combine tables, use the LIKELY JOINS section when it fits the question.
- Never join two tables on id columns that mean different things just because both
  are ids. If the tables the question needs have no real link, use reply type 2
  and say which link is missing.
- Give computed columns readable aliases, e.g. SUM(amount) AS total_amount.
- Round decimal results to 2 places.
- For "which is highest / lowest / most" questions, return the top 5 rows in order, best first,
  so the ranking is visible. Use LIMIT 1 only when the user asks for exactly one.
- To compare a value against a target, budget, plan or quota, or between groups of very
  different size, rank by a percentage (100 * value / target) and also show the difference.
  A raw difference alone favours small groups.
- For trends over time, return the period as a date with DATE_TRUNC (for example
  DATE_TRUNC('month', some_date) AS month), ordered oldest first, so it can be charted.
- If the question is a follow-up ("now by month", "only for 2024"), modify the
  previous SQL shown in the conversation."""

FIX_SQL_PROMPT = "That query failed:\n{error}\n\nFix it. Reply with the corrected SQL only."

EMPTY_RESULT_PROMPT = (
    "That query returned 0 rows. Check the filter values against the sample rows "
    "(spelling, case, date format) and the join columns. If the query is right and "
    "the answer really is 'none', return the same query again."
)

UNRELATED_JOIN_ERROR = (
    "The join {left} = {right} matches no rows at all: these two columns do not hold the same kind "
    "of value, so they are not related. Do not join unrelated id columns. Join only through the "
    "LIKELY JOINS or through columns that clearly mean the same thing. If the data has no real link "
    f"between the tables this question needs, reply with {CANNOT_ANSWER_MARKER} and say which link is missing."
)

WRITE_SUMMARY_PROMPT = """You explain the result of a data query to a business user.
You get the question and the result table that a database computed for it.

Rules:
- Answer the question in 1 to 3 sentences using ONLY values from the result table.
- Copy numbers exactly as they appear. Do not recalculate, add up, or estimate.
- Do not mention SQL, queries, or tables. Do not repeat the whole table, it is shown below your answer.
- Stop after the answer. No suggestions, no follow-up questions, no bullet lists.
- Name the top result, and mention context the table makes obvious, for example that every
  value is negative or below its target.
- If the result is empty, say that nothing in the data matches."""

FIX_SUMMARY_PROMPT = (
    "Your answer used these numbers, which are not in the result table: {numbers}. "
    "Rewrite the answer using only values that appear in the table, copied exactly."
)

SUGGEST_QUESTIONS_PROMPT = """You suggest questions a user could ask about the tables they uploaded.
You get a list of tables with their columns.
Write {count} short analytical questions (at most 15 words each) that this data can answer with a calculation
(totals, averages, counts, top items, trends over time).
If there is more than one table, at least one question must need two tables combined.
Use plain words a business user would use, not column or table names.
Do not name a specific value (a category, a group, a year, a person) unless it appears
in the sample rows you were given. Prefer questions that need no specific value,
such as "per category" or "top 5".
Reply with one question per line. No numbering, no bullets, nothing else."""
