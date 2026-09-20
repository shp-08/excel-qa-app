"""
app.py - the Streamlit UI. Run:  streamlit run app.py

Streamlit reruns this whole file top to bottom on every click or message.
Anything that must survive a rerun (the DuckDB tables, the chat) lives in
st.session_state, which is private to one browser tab.

All the real work is in core/. This file only draws things.
"""

from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
from openai import RateLimitError

from core import llm, log
from core.ingest import DataStore, compute_join_hints, ingest_file, remove_file
from core.query import (
    QueryResult, answer_question, narration_messages, suggest_questions, ungrounded_numbers,
)

SAMPLES_DIR = Path(__file__).parent / "samples"
SAMPLE_PREFIX = "sample:"          # marks files loaded by the "sample data" button

# chart colours: one accent for the data, quiet greys for everything else
ACCENT = "#4F46E5"
INK = "#1E1B4B"
MUTED = "#6B7280"
GRID = "#E8EAF6"

st.set_page_config(page_title="Excel Q&A", page_icon="📊", layout="centered")

# ----------------------------------------------------------------------------
# Session state: created once per browser tab
# ----------------------------------------------------------------------------

if "store" not in st.session_state:
    st.session_state.store = DataStore()      # DuckDB connection + table metadata
    st.session_state.loaded = {}              # file key -> file name, files already ingested
    st.session_state.messages = []            # everything shown in the chat
    st.session_state.history = []             # successful {"question","sql","tables"} for follow-ups
    st.session_state.suggestions = []         # clickable example questions for the loaded data
    st.session_state.suggested_for = ()       # which set of tables the suggestions were made for

store: DataStore = st.session_state.store


# ----------------------------------------------------------------------------
# Loading data
# ----------------------------------------------------------------------------

def sync_uploads(files) -> None:
    """Make the DuckDB tables match the uploader: ingest new files, drop removed ones."""
    current = {f"{f.name}:{f.size}": f for f in files}
    loaded = st.session_state.loaded
    changed = False

    for key in [k for k in loaded if k not in current and not k.startswith(SAMPLE_PREFIX)]:
        remove_file(store, loaded.pop(key))
        changed = True

    for key, f in current.items():
        if key in loaded:
            continue
        try:
            with st.spinner(f"Reading {f.name}…"):
                created = ingest_file(store, f.name, f.getvalue())
            if not created:
                st.sidebar.warning(f"{f.name}: no usable sheets found.")
        except Exception as e:
            log.exception("UPLOAD FAILED  %s", f.name)
            st.sidebar.error(f"Could not read {f.name}: {e}")
        loaded[key] = f.name                  # remember it even if it failed, so we do not retry every rerun
        changed = True

    if changed:
        compute_join_hints(store)


def load_samples() -> None:
    for path in sorted(SAMPLES_DIR.glob("*.xlsx")):
        key = SAMPLE_PREFIX + path.name
        if key not in st.session_state.loaded:
            ingest_file(store, path.name, path.read_bytes())
            st.session_state.loaded[key] = path.name
    compute_join_hints(store)


def remove_samples() -> None:
    for key in [k for k in st.session_state.loaded if k.startswith(SAMPLE_PREFIX)]:
        remove_file(store, st.session_state.loaded.pop(key))
    compute_join_hints(store)


def refresh_suggestions() -> None:
    """Ask the model for example questions once per set of loaded tables."""
    signature = tuple(store.tables)
    if signature == st.session_state.suggested_for:
        return
    st.session_state.suggested_for = signature
    st.session_state.suggestions = []
    if store.tables:
        with st.spinner("Looking at your data…"):
            st.session_state.suggestions = suggest_questions(store)


def compact(n: float) -> str:
    """14526 -> 14.5K, so a number always fits in a narrow tile."""
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= limit:
            return f"{n / limit:.1f}".rstrip("0").rstrip(".") + suffix
    return f"{n:,.0f}"


# ----------------------------------------------------------------------------
# Sidebar
# ----------------------------------------------------------------------------

with st.sidebar:
    st.header("📁 Your data")
    files = st.file_uploader(
        "Upload Excel files", type=["xlsx"], accept_multiple_files=True,
        help="Every sheet becomes a table you can ask about. Upload several files to ask questions across them.",
    )
    sync_uploads(files or [])

    has_samples = any(k.startswith(SAMPLE_PREFIX) for k in st.session_state.loaded)
    if has_samples:
        if st.button("Remove sample data", width="stretch"):
            remove_samples()
            st.rerun()
    elif not files and SAMPLES_DIR.exists() and st.button("✨ Try with sample data", width="stretch"):
        load_samples()
        st.rerun()

    if store.tables:
        total_rows = sum(t.row_count for t in store.tables.values())
        a, b, c = st.columns(3)
        a.metric("Files", len({t.file for t in store.tables.values()}))
        b.metric("Sheets", len(store.tables))
        c.metric("Rows", compact(total_rows), help=f"{total_rows:,} rows in total")

        # sheets grouped under the file they came from
        for file_name in dict.fromkeys(t.file for t in store.tables.values()):
            sheets = [t for t in store.tables.values() if t.file == file_name]
            with st.expander(f"📄 {file_name} · {len(sheets)} sheet{'s' if len(sheets) != 1 else ''}"):
                for t in sheets:
                    st.markdown(f"**{t.sheet}**  \n:gray[{t.row_count:,} rows × {len(t.columns)} columns]")

        with st.expander("👀 Preview a sheet"):
            labels = {f"{t.file} → {t.sheet}": t.name for t in store.tables.values()}
            choice = st.selectbox("Sheet", list(labels), label_visibility="collapsed")
            st.dataframe(
                store.con.execute(f'SELECT * FROM "{labels[choice]}" LIMIT 10').df(),
                width="stretch", hide_index=True,
            )

        if store.join_hints:
            with st.expander(f"🔗 Links found between sheets ({len(store.join_hints)})"):
                st.caption("Detected automatically from the data. They let questions combine sheets and files.")
                for h in store.join_hints[:60]:
                    left, right = store.tables[h.left_table], store.tables[h.right_table]
                    st.markdown(f":gray[{left.sheet}.]{h.left_col} → :gray[{right.sheet}.]{h.right_col}")

    if st.session_state.messages and st.button("🗑️ Clear chat", width="stretch"):
        st.session_state.messages = []
        st.session_state.history = []
        st.rerun()

    st.divider()
    st.caption(
        f"Model: `{llm.model_name()}` (open weights).  \n"
        "Every number is computed by running SQL on your data. The model writes the query and "
        "words the answer, it never does the arithmetic."
    )


# ----------------------------------------------------------------------------
# Drawing a result: metric, chart, table, download, and how it was computed
# ----------------------------------------------------------------------------

def looks_like_id_or_year(name: str, s: pd.Series) -> bool:
    """Ids and years are numbers we must not add separators to or plot as values."""
    if name.lower() == "id" or name.lower().endswith(("_id", "_year", "year", "_no", "_code")):
        return True
    return pd.api.types.is_integer_dtype(s) and bool(s.between(1900, 2100).all())


def number_format(df: pd.DataFrame) -> dict:
    """1234567.5 -> 1,234,567.5 in the table, for real quantities only."""
    return {
        col: st.column_config.NumberColumn(format="localized")
        for col in df.columns
        if pd.api.types.is_numeric_dtype(df[col]) and not looks_like_id_or_year(col, df[col])
    }


def make_chart(df: pd.DataFrame):
    """
    A chart only when the shape of the result clearly calls for one:
    one label column + one numeric column, 2 to 30 rows.
    Dates on the label side give a line, anything else gives bars in the result's own order.

    Styling: one accent colour for the data, grey for axes and grid so they stay
    in the background, the value written at the end of each bar so nobody has to
    read it off the axis.
    """
    if not 2 <= len(df) <= 30:
        return None
    numeric = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c]) and not looks_like_id_or_year(c, df[c])]
    labels = [c for c in df.columns if c not in numeric]
    if len(numeric) != 1 or len(labels) != 1:
        return None
    label, value = labels[0], numeric[0]
    nice = value.replace("_", " ")
    tip_value = alt.Tooltip(f"{value}:Q", title=nice, format=",.2~f")

    if pd.api.types.is_datetime64_any_dtype(df[label]):
        base = alt.Chart(df).encode(
            x=alt.X(f"{label}:T", title=None, axis=alt.Axis(grid=False)),
            y=alt.Y(f"{value}:Q", title=nice, axis=alt.Axis(format="~s", tickCount=5)),
            tooltip=[alt.Tooltip(f"{label}:T", title=label.replace("_", " ")), tip_value],
        )
        fill = alt.Gradient(gradient="linear", x1=1, x2=1, y1=1, y2=0, stops=[
            alt.GradientStop(color="#FFFFFF", offset=0), alt.GradientStop(color="#C7D2FE", offset=1)])
        chart = (base.mark_area(color=fill, opacity=0.7)
                 + base.mark_line(color=ACCENT, strokeWidth=2)
                 + base.mark_point(color=ACCENT, filled=True, size=70, opacity=1)).properties(height=300)
    else:
        data = df.assign(**{label: df[label].astype(str)})
        base = alt.Chart(data).encode(
            y=alt.Y(f"{label}:N", sort=None, title=None, axis=alt.Axis(grid=False)),   # sort=None keeps the result's order
            # the scale runs 18% past the biggest bar so its value label is never cut off
            x=alt.X(f"{value}:Q", title=nice, axis=alt.Axis(format="~s", tickCount=5),
                    scale=alt.Scale(domainMax=float(data[value].max()) * 1.18) if data[value].max() > 0 else alt.Undefined),
            tooltip=[alt.Tooltip(f"{label}:N", title=label.replace("_", " ")), tip_value],
        )
        chart = base.mark_bar(color=ACCENT, cornerRadiusEnd=4, height={"band": 0.62})
        if len(df) <= 15:                                           # value at the end of each bar
            chart = chart + base.mark_text(align="left", dx=6, color=INK, fontSize=12).encode(
                text=alt.Text(f"{value}:Q", format=",.2~f"))
        chart = chart.properties(height=max(140, 34 * len(df)))

    return (chart
            .configure_view(strokeWidth=0)
            .configure_axis(gridColor=GRID, domain=False, ticks=False, labelColor=MUTED, titleColor=MUTED,
                            labelFontSize=12, titleFontSize=12, labelPadding=8, labelLimit=240))


def draw_table(df: pd.DataFrame, msg: dict, key: str) -> None:
    st.dataframe(df, width="stretch", hide_index=True, column_config=number_format(df))
    note = f"Showing the first {len(df):,} rows. " if msg.get("truncated") else ""
    left, right = st.columns([3, 1])
    left.caption(f"{note}{len(df):,} row{'s' if len(df) != 1 else ''}")
    right.download_button("⬇ CSV", df.to_csv(index=False), file_name="result.csv", mime="text/csv",
                          key=f"csv_{key}", width="stretch")


def tile_value(value) -> str:
    if isinstance(value, bool) or value is None or (isinstance(value, float) and pd.isna(value)):
        return str(value)
    if isinstance(value, pd.Timestamp):
        return value.strftime("%d %b %Y")
    if pd.api.types.is_number(value):
        number = float(value)
        return f"{int(number):,}" if number.is_integer() else f"{number:,.2f}"
    return str(value)


def draw_details(msg: dict, key: str) -> None:
    """Everything under the answer sentence. key must be unique per message (for the buttons)."""
    df = msg.get("df")
    if df is not None and not df.empty:
        if len(df) == 1 and len(df.columns) <= 4:           # one row of values: big number tiles, not a table
            for column, name in zip(st.columns(len(df.columns)), df.columns):
                column.metric(str(name).replace("_", " ").capitalize(), tile_value(df[name].iloc[0]), border=True)
        else:
            chart = make_chart(df)
            if chart is not None:
                tab_chart, tab_table = st.tabs(["📈 Chart", "🔢 Table"])
                with tab_chart:
                    st.altair_chart(chart, width="stretch", theme=None)
                with tab_table:
                    draw_table(df, msg, key)
            else:
                draw_table(df, msg, key)

    if msg.get("warning"):
        st.caption(f"⚠️ {msg['warning']}")

    if msg.get("sql") or msg.get("attempts"):
        with st.expander("🔍 How this was computed"):
            if msg.get("sql"):
                st.code(msg["sql"], language="sql")
            if msg.get("tables_used"):
                used = [f"{store.tables[n].file} → {store.tables[n].sheet}" for n in msg["tables_used"] if n in store.tables]
                st.caption("Sheets used: " + " · ".join(used or msg["tables_used"]))
            if msg.get("selection_ran"):
                st.caption(f"Many sheets are loaded, so the {len(msg['selected_tables'])} relevant ones were picked first.")
            attempts = msg.get("attempts", [])
            if len(attempts) > 1:
                st.caption(f"Took {len(attempts)} attempts:")
                for i, a in enumerate(attempts, 1):
                    st.caption(f"{i}. {'✅ worked' if a.error is None else '❌ ' + a.error.splitlines()[0]}")
            st.caption(f"⏱ {msg.get('seconds', 0)}s")


# ----------------------------------------------------------------------------
# Main area
# ----------------------------------------------------------------------------

st.html("""
<div style="background:linear-gradient(120deg,#4F46E5 0%,#7C3AED 55%,#DB2777 130%);
            border-radius:18px;padding:26px 30px;color:#fff;margin-bottom:6px;
            box-shadow:0 10px 30px -12px rgba(79,70,229,.55)">
  <div style="font-size:1.85rem;font-weight:700;letter-spacing:-.02em;line-height:1.2">📊 Ask your Excel files</div>
  <div style="opacity:.92;margin-top:6px;font-size:1rem">
    Upload spreadsheets, ask questions in plain English, get answers computed from your data.</div>
</div>
""")

if not store.tables:
    # welcome screen
    cards = [
        ("📤", "Upload", "One or more Excel files. Every sheet becomes a table.", "#EEF2FF", "#4F46E5"),
        ("💬", "Ask", "Totals, averages, top items, trends. Questions can span files.", "#F5F3FF", "#7C3AED"),
        ("🔍", "Verify", "Each answer shows the query and the sheets it came from.", "#FDF2F8", "#DB2777"),
    ]
    for column, (icon, title, text, background, colour) in zip(st.columns(3), cards):
        column.html(f"""
        <div style="background:{background};border-radius:16px;padding:18px 18px 20px;
                    border:1px solid {colour}22;min-height:150px">
          <div style="font-size:1.6rem">{icon}</div>
          <div style="font-weight:700;color:{colour};font-size:1.05rem;margin-top:4px">{title}</div>
          <div style="color:#4B5563;font-size:.92rem;margin-top:4px;line-height:1.45">{text}</div>
        </div>""")
    st.info("⬅️ Upload files in the sidebar, or press **Try with sample data** to explore right away.")
else:
    refresh_suggestions()

for i, msg in enumerate(st.session_state.messages):
    with st.chat_message(msg["role"], avatar="🧑‍💻" if msg["role"] == "user" else "📊"):
        st.markdown(msg["content"])
        if msg["role"] == "assistant":
            draw_details(msg, key=str(i))

# the input box is pinned to the bottom of the page wherever this line sits, so we can
# read it first and know whether a question is already on its way
typed = st.chat_input("Ask a question about your data", disabled=not store.tables)
question = typed or st.session_state.pop("pending_question", None)

# suggestion chips: only before the first question, so they never clutter a conversation
if store.tables and not st.session_state.messages and not question and st.session_state.suggestions:
    st.markdown("###### Try asking")
    for n, suggestion in enumerate(st.session_state.suggestions):
        if st.button(suggestion, key=f"suggest_{n}", width="stretch"):
            st.session_state.pending_question = suggestion
            st.rerun()

if question:
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user", avatar="🧑‍💻"):
        st.markdown(question)

    with st.chat_message("assistant", avatar="📊"):
        msg = {"role": "assistant", "content": ""}
        step = st.empty()                     # a status line that changes as the pipeline moves on
        llm.on_wait = lambda seconds: step.caption(f"⏳ Free model tier is rate-limited, continuing in {seconds}s…")
        try:
            result: QueryResult = answer_question(
                store, question, st.session_state.history,
                progress=lambda text: step.caption(f"⏳ {text}"),
            )
            step.empty()

            msg.update(
                sql=result.sql, attempts=result.attempts, seconds=result.seconds,
                tables_used=result.tables_used, selection_ran=result.selection_ran,
                selected_tables=result.selected_tables,
            )

            if result.status == "ok":
                msg.update(df=result.df, truncated=result.truncated)
                # the sentence is written from the result rows and streamed word by word
                msg["content"] = st.write_stream(llm.chat_stream(narration_messages(result)))
                bad = ungrounded_numbers(msg["content"], result.df)
                if bad:
                    msg["warning"] = f"The summary mentions {', '.join(bad)}, which is not in the result. Trust the table."
                st.session_state.history.append(
                    {"question": question, "sql": result.sql, "tables": result.tables_used}
                )
            elif result.status == "chat":
                msg["content"] = result.message
                st.markdown(msg["content"])
            elif result.status == "refused":
                msg["content"] = f"I can't answer that from the uploaded data. {result.message}"
                st.markdown(msg["content"])
            else:
                msg["content"] = result.message
                st.markdown(msg["content"])

        except RateLimitError:
            log.info("RATE LIMIT hit on the model provider")
            step.empty()
            msg["content"] = ("The free model tier allows a limited number of tokens per minute and we just hit it. "
                              "Please wait about a minute and ask again.")
            st.warning(msg["content"])
        except Exception as e:                # missing key, network
            log.exception("FAILED    %s", question)
            step.empty()
            msg["content"] = f"Something went wrong talking to the model: {e}"
            st.error(msg["content"])

        draw_details(msg, key=str(len(st.session_state.messages)))
        st.session_state.messages.append(msg)
