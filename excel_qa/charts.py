"""Decides when a result deserves a chart, builds it, and formats numbers for display."""

from __future__ import annotations

import altair as alt
import pandas as pd

# one accent colour for the data, quiet greys for axes and grid
ACCENT = "#4F46E5"
TEXT = "#1E1B4B"
MUTED_TEXT = "#6B7280"
GRID = "#E8EAF6"

MAX_CHART_ROWS = 30
MAX_LABELLED_BARS = 15
MAX_SERIES = 6
MAX_SERIES_ROWS = 400

# series colours in a fixed order, checked for colour-blind separation; the Table tab backs up the light ones
SERIES_COLOURS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]


def is_id_or_year(name: str, values: pd.Series) -> bool:
    """Ids and years are numbers that must not get separators or be plotted as quantities."""
    lowered = name.lower()
    if lowered == "id" or lowered.endswith(("_id", "_year", "year", "_no", "_code")):
        return True
    return pd.api.types.is_integer_dtype(values) and bool(values.between(1900, 2100).all())


def quantity_columns(rows: pd.DataFrame) -> list[str]:
    """Numeric columns that hold real quantities (not ids or years)."""
    return [c for c in rows.columns if pd.api.types.is_numeric_dtype(rows[c]) and not is_id_or_year(c, rows[c])]


def format_compact(number: float) -> str:
    """14526 -> 14.5K, so a number always fits in a narrow tile."""
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(number) >= limit:
            return f"{number / limit:.1f}".rstrip("0").rstrip(".") + suffix
    return f"{number:,.0f}"


def format_tile_value(value) -> str:
    """One result cell as text for a big number tile."""
    if isinstance(value, pd.Timestamp):
        return value.strftime("%d %b %Y")
    if value is None or isinstance(value, bool) or not pd.api.types.is_number(value) or pd.isna(value):
        return str(value)
    number = float(value)
    return f"{int(number):,}" if number.is_integer() else f"{number:,.2f}"


def date_display_format(values: pd.Series) -> str | None:
    """'MMM YYYY' for month starts, 'DD MMM YYYY' for plain dates, None when the time of day matters."""
    dates = values.dropna()
    if dates.empty or (dates.dt.normalize() != dates).any():
        return None
    return "MMM YYYY" if (dates.dt.day == 1).all() else "DD MMM YYYY"


def _readable(column: str) -> str:
    return column.replace("_", " ")


def _bar_chart(rows: pd.DataFrame, label: str, value: str) -> alt.Chart:
    data = rows.assign(**{label: rows[label].astype(str)})
    top_value = float(data[value].max())
    base = alt.Chart(data).encode(
        # sort=None keeps the order the query returned, e.g. "highest first"
        y=alt.Y(f"{label}:N", sort=None, title=None, axis=alt.Axis(grid=False)),
        # the scale runs 18% past the biggest bar so its value label is never cut off
        x=alt.X(f"{value}:Q", title=_readable(value), axis=alt.Axis(format="~s", tickCount=5),
                scale=alt.Scale(domainMax=top_value * 1.18) if top_value > 0 else alt.Undefined),
        tooltip=[alt.Tooltip(f"{label}:N", title=_readable(label)),
                 alt.Tooltip(f"{value}:Q", title=_readable(value), format=",.2~f")],
    )
    chart = base.mark_bar(color=ACCENT, cornerRadiusEnd=4, height={"band": 0.62})
    if len(rows) <= MAX_LABELLED_BARS:
        chart += base.mark_text(align="left", dx=6, color=TEXT, fontSize=12).encode(
            text=alt.Text(f"{value}:Q", format=",.2~f"))
    return chart.properties(height=max(140, 34 * len(rows)))


def _line_chart(rows: pd.DataFrame, label: str, value: str) -> alt.Chart:
    base = alt.Chart(rows).encode(
        x=alt.X(f"{label}:T", title=None, axis=alt.Axis(grid=False)),
        y=alt.Y(f"{value}:Q", title=_readable(value), axis=alt.Axis(format="~s", tickCount=5)),
        tooltip=[alt.Tooltip(f"{label}:T", title=_readable(label)),
                 alt.Tooltip(f"{value}:Q", title=_readable(value), format=",.2~f")],
    )
    fade = alt.Gradient(gradient="linear", x1=1, x2=1, y1=1, y2=0, stops=[
        alt.GradientStop(color="#FFFFFF", offset=0), alt.GradientStop(color="#C7D2FE", offset=1)])
    layers = (base.mark_area(color=fade, opacity=0.7)
              + base.mark_line(color=ACCENT, strokeWidth=2)
              + base.mark_point(color=ACCENT, filled=True, size=70, opacity=1))
    return layers.properties(height=300)


def _multi_line_chart(rows: pd.DataFrame, time: str, series: str, value: str) -> alt.Chart:
    """One line per category over time, e.g. monthly totals split by group."""
    data = rows.assign(**{series: rows[series].astype(str)})
    names = list(dict.fromkeys(data[series]))                   # first-seen order keeps colours stable
    return alt.Chart(data).mark_line(strokeWidth=2, point=alt.OverlayMarkDef(size=55, filled=True)).encode(
        x=alt.X(f"{time}:T", title=None, axis=alt.Axis(grid=False)),
        y=alt.Y(f"{value}:Q", title=_readable(value), axis=alt.Axis(format="~s", tickCount=5)),
        color=alt.Color(f"{series}:N", title=_readable(series), sort=names,
                        scale=alt.Scale(domain=names, range=SERIES_COLOURS[:len(names)]),
                        legend=alt.Legend(orient="top", labelColor=TEXT, titleColor=MUTED_TEXT)),
        tooltip=[alt.Tooltip(f"{time}:T", title=_readable(time)), alt.Tooltip(f"{series}:N", title=_readable(series)),
                 alt.Tooltip(f"{value}:Q", title=_readable(value), format=",.2~f")],
    ).properties(height=320)


def _pick_chart(rows: pd.DataFrame):
    quantities = quantity_columns(rows)
    # a label with a single value (e.g. after "now only X") is a filter, not a dimension to plot
    labels = [c for c in rows.columns if c not in quantities and rows[c].nunique(dropna=False) > 1]
    if len(quantities) != 1 or len(rows) < 2:
        return None
    value = quantities[0]
    time_labels = [c for c in labels if pd.api.types.is_datetime64_any_dtype(rows[c])]

    if len(labels) == 1 and len(rows) <= MAX_CHART_ROWS:
        return _line_chart(rows, labels[0], value) if time_labels else _bar_chart(rows, labels[0], value)

    # a date column + one category column with few values: one line per category
    if len(labels) == 2 and len(time_labels) == 1 and len(rows) <= MAX_SERIES_ROWS:
        series = next(c for c in labels if c != time_labels[0])
        if 2 <= rows[series].nunique() <= MAX_SERIES:
            return _multi_line_chart(rows, time_labels[0], series, value)
    return None


def build_chart(rows: pd.DataFrame):
    """Bars for label + quantity, a line for date + quantity, lines per category for date + category + quantity."""
    chart = _pick_chart(rows)
    if chart is None:
        return None
    return chart.configure_view(strokeWidth=0).configure_axis(
        gridColor=GRID, domain=False, ticks=False, labelColor=MUTED_TEXT, titleColor=MUTED_TEXT,
        labelFontSize=12, titleFontSize=12, labelPadding=8, labelLimit=240)
