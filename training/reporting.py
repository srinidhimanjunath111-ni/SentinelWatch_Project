"""Evaluation figures. Every value plotted comes from a measured run; PNGs stay under 500 KB."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap, ListedColormap

INK = "#0b0b0b"
INK_MUTED = "#52514e"
GRID = "#e4e3df"
SERIES_1 = "#2a78d6"
SERIES_2 = "#eb6834"
SEQUENTIAL = LinearSegmentedColormap.from_list("blue", ["#f4f8fd", "#9cc3ee", "#2a78d6", "#123f78"])
LANDCOVER_COLOURS = (
    "#008300",  # tree
    "#9bc46a",  # shrub_grass
    "#eda100",  # crop
    "#e34948",  # built
    "#c9b48f",  # bare
    "#2a78d6",  # water
    "#1baf7a",  # wetland
    "#6250d6",  # mine
)
NO_LABEL = "#ffffff"
MAX_ARTEFACT_BYTES = 500 * 1024


def _style(axis) -> None:
    axis.grid(color=GRID, linewidth=0.8)
    axis.set_axisbelow(True)
    for side in ("top", "right"):
        axis.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        axis.spines[side].set_color(GRID)
    axis.tick_params(colors=INK_MUTED, labelsize=9, length=0)


def _save(figure, destination: Path, dpi: int = 110) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Satellite panels compress poorly, so step the resolution down until the file fits.
    while True:
        figure.savefig(destination, dpi=dpi, facecolor="white")
        if destination.stat().st_size <= MAX_ARTEFACT_BYTES or dpi <= 40:
            break
        dpi -= 8
    plt.close(figure)
    size = destination.stat().st_size
    if size > MAX_ARTEFACT_BYTES:
        raise ValueError(f"{destination} is {size} bytes; artefacts must stay under 500 KB")


def learning_curve(
    history: list[dict], x_key: str, x_label: str, y_key: str, y_label: str, title: str, destination: Path
) -> None:
    """Training loss and validation score in two panels, so no second y-scale is needed."""

    x = [row[x_key] for row in history]
    figure, axes = plt.subplots(1, 2, figsize=(9, 3.4))
    axes[0].plot(x, [row["train_loss"] for row in history], color=SERIES_1, linewidth=2)
    axes[0].set_title("Training loss", loc="left", fontsize=11, color=INK)
    values = [row[y_key] for row in history]
    axes[1].plot(x, values, color=SERIES_1, linewidth=2)
    best = int(np.argmax(values))
    axes[1].plot(x[best], values[best], "o", color=SERIES_1, markersize=8, markeredgecolor="white")
    axes[1].annotate(
        f"best {values[best]:.3f}",
        (x[best], values[best]),
        textcoords="offset points",
        xytext=(0, 10),
        ha="center",
        fontsize=9,
        color=INK,
    )
    axes[1].set_ylim(0, 1)
    axes[1].set_title(f"Validation {y_label} (not test)", loc="left", fontsize=11, color=INK)
    for axis in axes:
        _style(axis)
        axis.set_xlabel(x_label, fontsize=9, color=INK_MUTED)
    figure.suptitle(title, x=0.01, ha="left", fontsize=12, color=INK)
    figure.tight_layout()
    _save(figure, destination)


def confusion_matrix(
    matrix: np.ndarray, labels: tuple[str, ...], title: str, destination: Path
) -> None:
    """Row-normalised confusion matrix, annotated with percentages. Rows are the reference label."""

    totals = matrix.sum(axis=1, keepdims=True)
    share = np.divide(matrix, totals, out=np.zeros_like(matrix, dtype=float), where=totals > 0)
    side = 2.6 + 0.62 * len(labels)
    figure, axis = plt.subplots(figsize=(side + 0.8, side))
    image = axis.imshow(share, cmap=SEQUENTIAL, vmin=0, vmax=1)
    axis.set_xticks(range(len(labels)), labels, rotation=40, ha="right", fontsize=9, color=INK_MUTED)
    axis.set_yticks(range(len(labels)), labels, fontsize=9, color=INK_MUTED)
    axis.set_xlabel("Predicted", fontsize=10, color=INK_MUTED)
    axis.set_ylabel("Reference", fontsize=10, color=INK_MUTED)
    axis.tick_params(length=0)
    for side_name in axis.spines:
        axis.spines[side_name].set_visible(False)
    for row in range(len(labels)):
        for column in range(len(labels)):
            axis.text(
                column,
                row,
                f"{100 * share[row, column]:.0f}",
                ha="center",
                va="center",
                fontsize=9,
                color="white" if share[row, column] > 0.5 else INK,
            )
    colourbar = figure.colorbar(image, fraction=0.046, pad=0.03)
    colourbar.ax.tick_params(labelsize=8, length=0, colors=INK_MUTED)
    colourbar.outline.set_visible(False)
    colourbar.set_label("Share of reference pixels", fontsize=9, color=INK_MUTED)
    axis.set_title(title, loc="left", fontsize=11, color=INK)
    figure.tight_layout()
    _save(figure, destination)


def true_colour(stack: np.ndarray, valid: np.ndarray | None = None) -> np.ndarray:
    """Percentile-stretched RGB preview from reflectance [6,H,W] for display only."""

    rgb = np.stack([stack[2], stack[1], stack[0]], axis=-1).astype(np.float32)
    pixels = rgb[valid] if valid is not None and valid.any() else rgb.reshape(-1, 3)
    low, high = np.percentile(pixels, (2, 98))
    return np.clip((rgb - low) / max(high - low, 1e-6), 0, 1)


def change_samples(rows: list[dict], title: str, destination: Path, dpi: int = 80) -> None:
    """rows: dicts of name, before, after (RGB), truth (0/1/255), prediction (bool), f1."""

    figure, axes = plt.subplots(len(rows), 4, figsize=(10, 2.6 * len(rows)), squeeze=False)
    mask_colours = ListedColormap(["#f4f3f0", SERIES_2, "#c9c8c2"])
    for axes_row, row in zip(axes, rows, strict=True):
        truth = np.where(row["truth"] == 255, 2, row["truth"])
        panels = (
            (row["before"], {}, f"{row['name']}: before"),
            (row["after"], {}, "after"),
            (truth, {"cmap": mask_colours, "vmin": 0, "vmax": 2}, "reference (grey = ignored)"),
            (
                row["prediction"].astype(np.uint8),
                {"cmap": mask_colours, "vmin": 0, "vmax": 2},
                f"predicted (F1 {row['f1']:.2f})",
            ),
        )
        for axis, (image, options, label) in zip(axes_row, panels, strict=True):
            axis.imshow(image, interpolation="nearest", **options)
            axis.set_title(label, fontsize=9, color=INK, loc="left")
            axis.axis("off")
    figure.suptitle(title, x=0.01, ha="left", fontsize=12, color=INK)
    figure.tight_layout()
    _save(figure, destination, dpi=dpi)


def landcover_samples(
    rows: list[dict], classes: tuple[str, ...], title: str, destination: Path, dpi: int = 80
) -> None:
    """rows: dicts of name, rgb, truth (index or 255), prediction (index or 255)."""

    colours = ListedColormap([*LANDCOVER_COLOURS, NO_LABEL])

    def indexed(array: np.ndarray) -> np.ndarray:
        return np.where(array == 255, len(classes), array)

    figure, axes = plt.subplots(len(rows), 3, figsize=(8, 2.6 * len(rows)), squeeze=False)
    for axes_row, row in zip(axes, rows, strict=True):
        axes_row[0].imshow(row["rgb"])
        axes_row[0].set_title(row["name"], fontsize=9, color=INK, loc="left")
        axes_row[1].imshow(indexed(row["truth"]), cmap=colours, vmin=0, vmax=len(classes), interpolation="nearest")
        axes_row[1].set_title("reference (white = ignored)", fontsize=9, color=INK, loc="left")
        axes_row[2].imshow(indexed(row["prediction"]), cmap=colours, vmin=0, vmax=len(classes), interpolation="nearest")
        axes_row[2].set_title("predicted", fontsize=9, color=INK, loc="left")
        for axis in axes_row:
            axis.axis("off")
    handles = [
        plt.Line2D([], [], marker="s", linestyle="", color=colour, label=name, markersize=8)
        for name, colour in zip(classes, LANDCOVER_COLOURS, strict=True)
    ]
    figure.legend(handles=handles, loc="lower center", ncol=len(classes), fontsize=8, frameon=False)
    figure.suptitle(title, x=0.01, ha="left", fontsize=12, color=INK)
    figure.tight_layout(rect=(0, 0.04, 1, 1))
    _save(figure, destination, dpi=dpi)
