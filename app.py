"""The Streamlit page. Run with:  streamlit run app.py   (all logic lives in excel_qa/)"""

from html import escape
from pathlib import Path

import pandas as pd
import streamlit as st
from openai import RateLimitError

from excel_qa import llm_client
from excel_qa.answer_writer import (
    build_summary_conversation, numbers_not_in_result, rewrite_summary, suggest_questions,
)
from excel_qa.charts import build_chart, date_display_format, format_compact, format_tile_value, quantity_columns
from excel_qa.file_loader import DataStore, load_data_file, remove_data_file
from excel_qa.link_detector import detect_links
from excel_qa.logger import log
from excel_qa.question_pipeline import QuestionResult, answer_question

SAMPLES_FOLDER = Path(__file__).parent / "samples"
SAMPLE_KEY_PREFIX = "sample:"          # marks files loaded by the "sample data" button
USER_AVATAR, APP_AVATAR = "🧑‍💻", "📊"

st.set_page_config(page_title="Excel Q&A", page_icon="📊", layout="centered")

# Streamlit reruns this file on every interaction, so anything that must survive lives in session_state.
if "store" not in st.session_state:
    st.session_state.store = DataStore()
    st.session_state.loaded_files = {}         # file key -> file name
    st.session_state.chat_messages = []        # everything shown in the chat
    st.session_state.previous_turns = []       # answered {"question", "sql", "tables"}, for follow-ups
    st.session_state.suggestions = []
    st.session_state.suggestions_made_for = () # the set of tables the suggestions were written for

store: DataStore = st.session_state.store


# --- Page header and loader ---

# Streamlit fades the old screen until a rerun ends, which looks frozen. Turn that off and use our own loader.
st.html("""
<style>
  [data-stale="true"] { opacity: 1 !important; transition: none !important; }
  /* charts: no hover menu (PNG / Vega-Lite spec); the Table tab and CSV button cover the data */
  *:has(> [data-testid="stVegaLiteChart"]) [data-testid="stElementToolbar"] { display: none !important; }
  @keyframes excelqa-spin { to { transform: rotate(360deg); } }
</style>
""")

st.html("""
<div style="background:linear-gradient(120deg,#4F46E5 0%,#7C3AED 55%,#DB2777 130%);
            border-radius:18px;padding:26px 30px;color:#fff;margin-bottom:6px;
            box-shadow:0 10px 30px -12px rgba(79,70,229,.55)">
  <div style="font-size:1.85rem;font-weight:700;letter-spacing:-.02em;line-height:1.2">📊 Ask your data files</div>
  <div style="opacity:.92;margin-top:6px;font-size:1rem">
    Upload Excel or CSV files, ask questions in plain English, get answers computed from your data.</div>
</div>
""")

loader_slot = st.empty()
welcome_slot = st.empty()              # created before the sidebar so loading can clear the welcome cards at once


def show_loader(title: str, detail: str = "") -> None:
    loader_slot.html(f"""
    <div style="display:flex;align-items:center;gap:16px;background:#EEF2FF;border:1px solid #C7D2FE;
                border-radius:16px;padding:18px 22px;margin:10px 0 4px">
      <div style="width:30px;height:30px;border-radius:50%;border:3px solid #C7D2FE;flex:none;
                  border-top-color:#4F46E5;animation:excelqa-spin .8s linear infinite"></div>
      <div>
        <div style="font-weight:600;color:#1E1B4B">{title}</div>
        <div style="color:#6B7280;font-size:.9rem;margin-top:2px">{detail}</div>
      </div>
    </div>""")


def hide_loader() -> None:
    loader_slot.empty()


# --- Loading data ---

def sync_uploaded_files(uploaded_files) -> None:
    """Makes the loaded tables match the uploader: loads new files, drops removed ones."""
    uploaded = {f"{f.name}:{f.size}": f for f in uploaded_files}
    loaded = st.session_state.loaded_files
    changed = False

    for key in [k for k in loaded if k not in uploaded and not k.startswith(SAMPLE_KEY_PREFIX)]:
        remove_data_file(store, loaded.pop(key))
        changed = True

    for key, file in uploaded.items():
        if key in loaded:
            continue
        try:
            welcome_slot.empty()
            show_loader(f"Reading {file.name}…", "Cleaning each sheet and loading it so it can be queried.")
            if not load_data_file(store, file.name, file.getvalue()):
                st.sidebar.warning(f"{file.name}: no usable sheets found.")
        except Exception as error:
            log.exception("UPLOAD FAILED  %s", file.name)
            st.sidebar.error(f"Could not read {file.name}: {error}")
        loaded[key] = file.name            # remembered even on failure, so it is not retried every rerun
        changed = True

    if changed:
        show_loader("Finding links between sheets…", "Looking for columns that connect one sheet to another.")
        detect_links(store)
        hide_loader()


def load_sample_files() -> None:
    sample_paths = sorted(p for p in SAMPLES_FOLDER.iterdir() if p.suffix.lower() in (".xlsx", ".csv"))
    welcome_slot.empty()
    show_loader("Loading the sample workbooks…", f"{len(sample_paths)} example files that link to each other.")
    for path in sample_paths:
        key = SAMPLE_KEY_PREFIX + path.name
        if key not in st.session_state.loaded_files:
            load_data_file(store, path.name, path.read_bytes())
            st.session_state.loaded_files[key] = path.name
    detect_links(store)


def remove_sample_files() -> None:
    loaded = st.session_state.loaded_files
    for key in [k for k in loaded if k.startswith(SAMPLE_KEY_PREFIX)]:
        remove_data_file(store, loaded.pop(key))
    detect_links(store)


def refresh_suggestions() -> None:
    """Asks the model for example questions once per set of loaded tables."""
    current_tables = tuple(store.tables)
    if current_tables == st.session_state.suggestions_made_for:
        return
    st.session_state.suggestions_made_for = current_tables
    show_loader("Looking at your data…", "Preparing a few questions you could ask.")
    st.session_state.suggestions = suggest_questions(store)
    hide_loader()


# --- Sidebar ---

with st.sidebar:
    st.header("📁 Your data")
    uploaded_files = st.file_uploader(
        "Upload Excel or CSV files", type=["xlsx", "xls", "csv"], accept_multiple_files=True,
        help="Every sheet becomes a table you can ask about. Upload several files to ask questions across them.",
    )
    sync_uploaded_files(uploaded_files or [])

    samples_are_loaded = any(k.startswith(SAMPLE_KEY_PREFIX) for k in st.session_state.loaded_files)
    if samples_are_loaded:
        if st.button("Remove sample data", width="stretch"):
            remove_sample_files()
            st.rerun()
    elif not uploaded_files and SAMPLES_FOLDER.exists():
        if st.button("✨ Try with sample data", width="stretch"):
            load_sample_files()
            st.rerun()

    if store.tables:
        tables = list(store.tables.values())
        total_rows = sum(t.row_count for t in tables)
        files_tile, sheets_tile, rows_tile = st.columns(3)
        files_tile.metric("Files", len({t.file for t in tables}))
        sheets_tile.metric("Sheets", len(tables))
        rows_tile.metric("Rows", format_compact(total_rows), help=f"{total_rows:,} rows in total")

        for file_name in dict.fromkeys(t.file for t in tables):          # keeps upload order
            sheets = [t for t in tables if t.file == file_name]
            with st.expander(f"📄 {file_name} · {len(sheets)} sheet{'s' if len(sheets) != 1 else ''}"):
                for t in sheets:
                    st.markdown(f"**{t.sheet}**  \n:gray[{t.row_count:,} rows × {len(t.columns)} columns]")

        with st.expander("👀 Preview a sheet"):
            table_by_label = {f"{t.file} → {t.sheet}": t.name for t in tables}
            chosen = st.selectbox("Sheet", list(table_by_label), label_visibility="collapsed")
            preview = store.con.execute(f'SELECT * FROM "{table_by_label[chosen]}" LIMIT 10').df()
            st.dataframe(preview, width="stretch", hide_index=True)

        if store.links:
            with st.expander(f"🔗 Links found between sheets ({len(store.links)})"):
                st.caption("Detected automatically from the data. They let questions combine sheets and files.")
                for link in store.links[:60]:
                    from_sheet, to_sheet = store.tables[link.from_table].sheet, store.tables[link.to_table].sheet
                    st.markdown(f":gray[{from_sheet}.]{link.from_column} → :gray[{to_sheet}.]{link.to_column}")

    if st.session_state.chat_messages and st.button("🗑️ Clear chat", width="stretch"):
        st.session_state.chat_messages = []
        st.session_state.previous_turns = []
        st.rerun()

    st.divider()
    st.caption(
        f"Model: `{llm_client.main_model_name()}` (open weights).  \n"
        "Every number is computed by running SQL on your data. The model writes the query and "
        "words the answer, it never does the arithmetic."
    )


# --- Showing one answer ---

def show_result_table(rows: pd.DataFrame, message: dict, key: str) -> None:
    # thousands separators for quantities only: ids and years stay as they are
    column_format = {c: st.column_config.NumberColumn(format="localized") for c in quantity_columns(rows)}
    for column in rows.select_dtypes(include="datetime").columns:        # "Jan 2024", not "2024-01-01 00:00:00"
        date_format = date_display_format(rows[column])
        if date_format:
            column_format[column] = st.column_config.DateColumn(format=date_format)
    st.dataframe(rows, width="stretch", hide_index=True, column_config=column_format)

    count_text = f"{len(rows):,} row{'s' if len(rows) != 1 else ''}"
    if message.get("rows_were_cut_off"):
        count_text = f"Showing the first {count_text}"
    left, right = st.columns([3, 1])
    left.caption(count_text)
    right.download_button("⬇ CSV", rows.to_csv(index=False), file_name="result.csv", mime="text/csv",
                          key=f"csv_{key}", width="stretch")


def show_value_tiles(rows: pd.DataFrame) -> None:
    """Big number tiles for a one-row result. They wrap onto new lines, so nothing is ever cut off."""
    tiles = "".join(
        f"""<div style="flex:1 1 150px;background:#fff;border:1px solid #E0E3F5;border-radius:14px;padding:14px 16px">
              <div style="color:#6B7280;font-size:.85rem">{escape(str(column).replace("_", " ").capitalize())}</div>
              <div style="color:#1E1B4B;font-size:1.35rem;font-weight:600;margin-top:4px;overflow-wrap:anywhere">
                {escape(format_tile_value(rows[column].iloc[0]))}</div>
            </div>"""
        for column in rows.columns)
    st.html(f'<div style="display:flex;flex-wrap:wrap;gap:10px;margin:4px 0 8px">{tiles}</div>')


def show_result(rows: pd.DataFrame, message: dict, key: str) -> None:
    """One row of values -> big number tiles. A label + a quantity -> chart and table tabs. Otherwise a table."""
    if len(rows) == 1 and len(rows.columns) <= 4:
        show_value_tiles(rows)
        return

    chart = build_chart(rows)
    if chart is None:
        show_result_table(rows, message, key)
        return
    chart_tab, table_tab = st.tabs(["📈 Chart", "🔢 Table"])
    with chart_tab:
        st.altair_chart(chart, width="stretch", theme=None)
    with table_tab:
        show_result_table(rows, message, key)


def show_how_it_was_computed(message: dict) -> None:
    with st.expander("🔍 How this was computed"):
        if message.get("sql"):
            st.code(message["sql"], language="sql")
        sheets_used = [f"{store.tables[n].file} → {store.tables[n].sheet}"
                       for n in message.get("tables_used", []) if n in store.tables]
        if sheets_used:
            st.caption("Sheets used: " + " · ".join(sheets_used))
        if message.get("tables_were_picked"):
            st.caption(f"Many sheets are loaded, so the {len(message['picked_tables'])} relevant ones were picked first.")
        attempts = message.get("attempts", [])
        if len(attempts) > 1:
            st.caption(f"Took {len(attempts)} attempts:")
            for number, attempt in enumerate(attempts, 1):
                outcome = "✅ worked" if attempt.error is None else "❌ " + attempt.error.splitlines()[0]
                st.caption(f"{number}. {outcome}")
        st.caption(f"⏱ {message.get('seconds', 0)}s")


def show_answer_details(message: dict, key: str) -> None:
    """Everything under the answer sentence. key must be unique per message, for the download button."""
    rows = message.get("rows")
    if rows is not None and not rows.empty:
        show_result(rows, message, key)
    if message.get("warning"):
        st.caption(f"⚠️ {message['warning']}")
    if message.get("sql") or message.get("attempts"):
        show_how_it_was_computed(message)


# --- Main area ---

if not store.tables:
    with welcome_slot.container():
        welcome_cards = [
            ("📤", "Upload", "One or more Excel or CSV files. Every sheet becomes a table.", "#EEF2FF", "#4F46E5"),
            ("💬", "Ask", "Totals, averages, top items, trends. Questions can span files.", "#F5F3FF", "#7C3AED"),
            ("🔍", "Verify", "Each answer shows the query and the sheets it came from.", "#FDF2F8", "#DB2777"),
        ]
        for column, (icon, title, text, background, colour) in zip(st.columns(3), welcome_cards):
            column.html(f"""
            <div style="background:{background};border-radius:16px;padding:18px 18px 20px;
                        border:1px solid {colour}22;min-height:150px">
              <div style="font-size:1.6rem">{icon}</div>
              <div style="font-weight:700;color:{colour};font-size:1.05rem;margin-top:4px">{title}</div>
              <div style="color:#4B5563;font-size:.92rem;margin-top:4px;line-height:1.45">{text}</div>
            </div>""")
        st.info("⬅️ Upload files in the sidebar, or press **Try with sample data** to explore right away.")
else:
    welcome_slot.empty()
    refresh_suggestions()

for index, message in enumerate(st.session_state.chat_messages):
    is_user = message["role"] == "user"
    with st.chat_message(message["role"], avatar=USER_AVATAR if is_user else APP_AVATAR):
        st.markdown(message["content"])
        if not is_user:
            show_answer_details(message, key=str(index))

# The input is pinned to the page bottom wherever this line sits, so read it before drawing the suggestions.
typed_question = st.chat_input("Ask a question about your data", disabled=not store.tables)
question = typed_question or st.session_state.pop("clicked_suggestion", None)

# Suggestions sit in a slot so they can be wiped the moment a question is asked.
suggestions_slot = st.empty()
show_suggestions = (store.tables and st.session_state.suggestions
                    and not st.session_state.chat_messages and not question)
if show_suggestions:
    with suggestions_slot.container():
        st.markdown("###### Try asking")
        for number, suggestion in enumerate(st.session_state.suggestions):
            if st.button(suggestion, key=f"suggest_{number}", width="stretch"):
                st.session_state.clicked_suggestion = suggestion
                st.rerun()
else:
    suggestions_slot.empty()


def answer_in_chat(question: str) -> dict:
    """Runs the pipeline for one question, draws the reply as it arrives, and returns the chat message."""
    message = {"role": "assistant", "content": ""}
    progress_line = st.empty()
    llm_client.on_rate_limit_wait = lambda seconds: progress_line.caption(
        f"⏳ Free model tier is rate-limited, continuing in {seconds}s…")
    try:
        result: QuestionResult = answer_question(
            store, question, st.session_state.previous_turns,
            show_progress=lambda text: progress_line.caption(f"⏳ {text}"),
        )
        progress_line.empty()
        message.update(
            sql=result.sql, attempts=result.attempts, seconds=result.seconds, tables_used=result.tables_used,
            tables_were_picked=result.tables_were_picked, picked_tables=result.picked_tables,
        )

        if result.status == "ok":
            message.update(rows=result.rows, rows_were_cut_off=result.rows_were_cut_off)
            # the sentence is written from the result rows and typed out as it arrives
            summary_slot = st.empty()
            with summary_slot:
                message["content"] = st.write_stream(llm_client.stream_model_reply(build_summary_conversation(result)))
            unmatched = numbers_not_in_result(message["content"], result.rows)
            if unmatched:                  # the model misquoted a number: rewrite the sentence once
                message["content"], unmatched = rewrite_summary(result, message["content"], unmatched)
                summary_slot.markdown(message["content"])
            if unmatched:
                message["warning"] = f"The summary mentions {', '.join(unmatched)}, which is not in the result. Trust the table."
            st.session_state.previous_turns.append(
                {"question": question, "sql": result.sql, "tables": result.tables_used})
        else:
            prefix = "I can't answer that from the uploaded data. " if result.status == "refused" else ""
            message["content"] = prefix + result.message
            st.markdown(message["content"])

    except RateLimitError:
        log.info("RATE LIMIT hit on the model provider")
        progress_line.empty()
        message["content"] = ("The free model tier allows a limited number of tokens per minute and we just hit it. "
                              "Please wait about a minute and ask again.")
        st.warning(message["content"])
    except Exception as error:             # missing key, network problem
        log.exception("FAILED    %s", question)
        progress_line.empty()
        message["content"] = f"Something went wrong talking to the model: {error}"
        st.error(message["content"])
    return message


if question:
    st.session_state.chat_messages.append({"role": "user", "content": question})
    with st.chat_message("user", avatar=USER_AVATAR):
        st.markdown(question)
    with st.chat_message("assistant", avatar=APP_AVATAR):
        reply = answer_in_chat(question)
        show_answer_details(reply, key=str(len(st.session_state.chat_messages)))
    st.session_state.chat_messages.append(reply)
