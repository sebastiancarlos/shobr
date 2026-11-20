"""Render progress charts with matplotlib.

Importing this module requires the optional charts extra. Data-only callers
should import `shobr.viz.data` instead.
"""

import calendar
import os
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import matplotlib
import matplotlib.dates as mdates
from matplotlib.axes import Axes
from matplotlib.patches import Patch
from matplotlib.ticker import FuncFormatter, MaxNLocator

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from ..color import BLUE, RESET
from ..core import SHOBR_DATA_DIR
from ..providers.base import get_provider
from .data import (
    SERIES_ORDER,
    ChartSpec,
    SeriesSpec,
    add_months,
    bin_granularity,
    progress_specs,
)

_CM = 1 / 2.54

_RANGE_LABELS = {
    "last-week": "Last week",
    "last-month": "Last month",
    "6months": "Last 6 months",
    "year": "Last 12 months",
}
_SERIES_COLORS = {
    "Discovered": "#9E9E9E",
    "Enriched": "#64B5F6",
    "Screened": "#FFB74D",
    "Tailored": "#BA68C8",
    "Tracked": "#0D47A1",
    "Applied": "#66BB6A",
}
_SERIES_LABELS = {
    "Discovered": "Discovered",
    "Enriched": "Enriched",
    "Screened": "Screened (human)",
    "Tailored": "Tailored",
    "Tracked": "Tracked",
    "Applied": "Applied",
}
_PASSING_LABELS = {
    "Discovered": "Discovered passing",
    "Enriched": "Enriched passing",
    "Screened": "Screened passing",
}
_PASSING_DENOMINATORS = {
    "Discovered": None,
    "Enriched": "enriched",
    "Screened": "screened",
}
_PENDING_COLOR = "#C62828"


def application_count_label(total: int) -> str:
    """Describe the number of applications in the selected time range."""
    if total == 0:
        return "No applications in time range"
    if total == 1:
        return "1 application in time range"
    return f"{total} applications in time range"


def _month_formatter(value: float, _pos: int) -> str:
    """Label months, adding the year at January."""
    stamp = mdates.num2date(value)
    if stamp.month == 1:
        return f"{stamp:%b}\n{stamp:%Y}"
    return f"{stamp:%b}"


def format_axis(ax: Axes, range_key: str) -> None:
    """Apply range-appropriate date ticks: days for week/month, months otherwise."""
    if range_key in {"week", "last-week"}:
        ax.xaxis.set_major_locator(mdates.DayLocator(interval=1))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%a\n%b %d"))
    elif range_key in {"month", "last-month"}:
        start = mdates.num2date(ax.get_xlim()[0]).date()
        last_day = calendar.monthrange(start.year, start.month)[1]
        ax.xaxis.set_major_locator(mdates.DayLocator(bymonthday=[1, 15, last_day]))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        ax.xaxis.set_minor_locator(mdates.DayLocator())
        ax.tick_params(axis="x", which="minor", length=3.0)
    else:
        ax.xaxis.set_major_locator(mdates.MonthLocator())
        ax.xaxis.set_major_formatter(FuncFormatter(_month_formatter))


def _series_total(series: SeriesSpec) -> int:
    """Return a series total, treating an absent total as zero."""
    return series.total or 0


def _series_label(name: str, series: SeriesSpec, totals: dict[str, int]) -> str:
    """Build the legend label for one cumulative series."""
    total = _series_total(series)
    discovered = totals["discovered"]
    if name == "Tracked":
        tailored = totals["tailored"]
        suffix = f"{total}, {total / tailored:.0%} of tailored" if tailored else f"{total}"
    elif not discovered or name == "Discovered":
        suffix = f"{total}"
    else:
        suffix = f"{total}, {total / discovered:.0%}"
    return f"{_SERIES_LABELS[name]} ({suffix})"


def _passing_label(name: str, series: SeriesSpec, totals: dict[str, int]) -> str:
    """Build the legend label for one passing series."""
    total = _series_total(series)
    series_total = totals[name.lower()]
    passing_pct = total / series_total if series_total else 0
    denominator = _PASSING_DENOMINATORS[name]
    percentage = (
        f"{passing_pct:.0%} of {denominator}" if denominator is not None else f"{passing_pct:.0%}"
    )
    return f"{_PASSING_LABELS[name]} ({total}, {percentage})"


def _range_label(spec: ChartSpec) -> str:
    """Build the human-readable range label for one chart."""
    start = spec.start.date()
    end = spec.end.date()
    if spec.range_key == "week":
        return f"{start:%b %d} - {(end - timedelta(days=1)):%b %d, %Y}"
    if spec.range_key in {"last-week", "last-month"}:
        return (
            f"{_RANGE_LABELS[spec.range_key]} "
            f"({start:%b %d} - {(end - timedelta(days=1)):%b %d, %Y})"
        )
    if spec.range_key == "month":
        return f"{start:%B %Y}"
    return f"{_RANGE_LABELS[spec.range_key]} ({start:%b %Y} - {(end - timedelta(days=1)):%b %Y})"


def _highlight(spec: ChartSpec) -> tuple[datetime, datetime, str] | None:
    """Return the highlighted interval and its legend label, if any."""
    start = spec.start.date()
    end = spec.end.date()
    if spec.range_key in {"week", "month"} and start <= spec.today < end:
        return (
            datetime.combine(spec.today, time.min),
            datetime.combine(spec.today + timedelta(days=1), time.min),
            "Today",
        )
    if spec.range_key in {"6months", "year"} and start <= spec.today < end:
        month_start = spec.today.replace(day=1)
        return (
            datetime.combine(month_start, time.min),
            datetime.combine(add_months(month_start, 1), time.min),
            "This month",
        )
    return None


def _open_file(path: Path) -> None:
    """Open a file with the system default application."""
    if os.environ.get("CI"):
        return
    if not path.exists():
        print(f"warning: {path} not found, nothing to open", file=sys.stderr)
        return
    command = "open" if sys.platform == "darwin" else "xdg-open"
    try:
        subprocess.Popen(
            [command, str(path)],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        print(f"warning: could not open {path}: {exc}", file=sys.stderr)


def render_chart(
    spec: ChartSpec,
    out_dir: Path,
    *,
    open_image: bool = True,
) -> Path:
    """Render one chart spec and return the PNG path."""
    plt.style.use("ggplot")
    fig, (ax_top, ax_bottom) = plt.subplots(
        2,
        1,
        figsize=(30 * _CM, 18 * _CM),
        sharex=True,
        gridspec_kw={"height_ratios": [3, 2]},
        layout="constrained",
    )
    highlight = _highlight(spec)
    if highlight is not None:
        highlight_start, highlight_end, _ = highlight
        for ax in (ax_top, ax_bottom):
            ax.axvspan(
                highlight_start,
                highlight_end,
                facecolor="#FFE082",
                alpha=0.35,
                edgecolor="none",
                zorder=0.5,
            )

    totals = {key: _series_total(series) for key, series in spec.series.items()}
    for name in SERIES_ORDER:
        key = name.lower()
        series = spec.series[key]
        ax_top.step(
            series.x,
            series.y,
            where="post",
            color=_SERIES_COLORS[name],
            linewidth=2.0,
            label=_series_label(name, series, totals),
        )
        passing = spec.series.get(f"{key}_passing")
        if passing is not None:
            ax_top.step(
                passing.x,
                passing.y,
                where="post",
                color=_SERIES_COLORS[name],
                linewidth=2.0,
                linestyle="--",
                label=_passing_label(name, passing, totals),
            )

    pending = spec.series["pending"]
    pending_current = pending.y[-1]
    ax_top.step(
        pending.x,
        pending.y,
        where="post",
        color=_PENDING_COLOR,
        linewidth=2.0,
        linestyle=(0, (2, 1)),
        label=f"Pending on you ({pending_current})",
    )

    display_name = get_provider(spec.provider).display_name
    range_label = _range_label(spec)
    ax_top.set_ylabel("Postings by type")
    ax_top.set_ylim(bottom=0)
    ax_top.set_title(
        f"{display_name} pipeline progress\n{range_label}",
        loc="left",
        fontsize=12,
        fontweight="bold",
        pad=10,
    )
    handles = ax_top.get_legend_handles_labels()[0]
    if highlight is not None:
        handles.append(
            Patch(
                facecolor="#FFE082",
                alpha=0.5,
                edgecolor="none",
                label=highlight[2],
            )
        )
    ax_top.legend(
        handles=handles,
        loc="best",
        frameon=True,
        framealpha=0.9,
        edgecolor="#BDBDBD",
        facecolor="white",
        fontsize=8,
        labelspacing=0.3,
        handlelength=1.5,
    )

    applications = [bin_.count for bin_ in spec.application_bins]
    total_applications = sum(applications)
    x_values = [bin_.start for bin_ in spec.application_bins]
    widths = [(bin_.end - bin_.start).days for bin_ in spec.application_bins]
    ax_bottom.bar(
        x_values,
        applications,
        width=widths,
        align="edge",
        color=_SERIES_COLORS["Applied"],
        alpha=0.85,
        edgecolor="white",
        linewidth=0.5,
    )
    ax_bottom.set_ylim(bottom=0)
    if not any(applications):
        ax_bottom.set_ylim(0, 1)
    ax_bottom.set_ylabel("Applications sent")
    granularity = bin_granularity(spec.range_key)
    ax_bottom.set_title(
        f"New applications per {granularity}",
        loc="left",
        fontsize=10,
        fontweight="bold",
    )
    ax_bottom.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax_bottom.text(
        0.99,
        0.95,
        application_count_label(total_applications),
        transform=ax_bottom.transAxes,
        ha="right",
        va="top",
        fontsize=8,
        color="#333333",
        bbox={
            "boxstyle": "round,pad=0.3",
            "facecolor": "white",
            "edgecolor": "none",
            "alpha": 0.85,
        },
    )

    xlim_start = spec.start
    xlim_end = spec.end - timedelta(seconds=1)
    ax_bottom.set_xlim(xlim_start, xlim_end)
    format_axis(ax_bottom, spec.range_key)
    ax_bottom.set_xlabel("")

    out_dir.mkdir(parents=True, exist_ok=True)
    png = out_dir / f"progress-{spec.provider}-{spec.range_key}.png"
    old_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    os.environ["SOURCE_DATE_EPOCH"] = "0"
    try:
        fig.savefig(
            png,
            dpi=200,
            bbox_inches="tight",
            pad_inches=0.3,
        )
    finally:
        if old_epoch is None:
            del os.environ["SOURCE_DATE_EPOCH"]
        else:
            os.environ["SOURCE_DATE_EPOCH"] = old_epoch
    plt.close(fig)

    print(f"{spec.provider} {spec.range_key}: {BLUE}{png}{RESET}")
    if open_image:
        _open_file(png)
    return png


def default_out_dir() -> Path:
    """The default directory for rendered charts."""
    return Path("/tmp/shobr")


def render_progress(
    ranges: list[str] | None = None,
    out_dir: Path | None = None,
    *,
    data_home: Path = SHOBR_DATA_DIR,
    today: date | None = None,
    open_image: bool = True,
) -> list[Path]:
    """Render progress charts for the current provider and return their paths."""
    specs = progress_specs(ranges, data_home=data_home, today=today)
    output_dir = out_dir or default_out_dir()
    return [render_chart(spec, output_dir, open_image=open_image) for spec in specs]
