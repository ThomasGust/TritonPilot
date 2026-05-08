from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Species:
    common_name: str
    scientific_name: str

    @property
    def display_name(self) -> str:
        return f"{self.common_name} ({self.scientific_name})"


@dataclass(frozen=True)
class FrequencyRow:
    species: Species
    count: int
    percent_frequency: float


DEFAULT_SPECIES: tuple[Species, ...] = (
    Species("Snow crab", "Chionoecetes opilio"),
    Species("Acadian hermit crab", "Pagurus acadianus"),
    Species("Western Atlantic hairy hermit crab", "Pagurus arcuatus"),
    Species("European green crab", "Carcinus maenas"),
    Species("Rock crab", "Cancer pagurus"),
    Species("Jonah crab", "Cancer borealis"),
    Species("Spiny sunstar", "Crossaster papposus"),
    Species("Sea urchin", "Strongylocentrotus droebachiensis"),
    Species("Boreal sea star", "Boreal asterias"),
    Species("Daisy brittle star", "Ophiopholis aculeata"),
)

SAMPLE_COUNTS: tuple[int, ...] = (19, 3, 1, 9, 10, 5, 8, 10, 12, 7)


def calculate_frequency_rows(
    counts: Sequence[int],
    species: Sequence[Species] = DEFAULT_SPECIES,
) -> list[FrequencyRow]:
    if len(counts) != len(species):
        raise ValueError(f"expected {len(species)} counts, received {len(counts)}")

    normalized_counts: list[int] = []
    for value in counts:
        count = int(value)
        if count < 0:
            raise ValueError("species counts must be non-negative")
        normalized_counts.append(count)

    total = sum(normalized_counts)
    rows: list[FrequencyRow] = []
    for item, count in zip(species, normalized_counts):
        percent = (count / total * 100.0) if total else 0.0
        rows.append(FrequencyRow(item, count, percent))
    return rows


def total_seen(rows: Sequence[FrequencyRow]) -> int:
    return sum(row.count for row in rows)


def format_percent(value: float, precision: int = 2) -> str:
    precision = max(0, min(int(precision), 6))
    return f"{float(value):.{precision}f}%"


def build_judge_report(rows: Sequence[FrequencyRow], *, precision: int = 2) -> str:
    total = total_seen(rows)
    species_width = max(
        [len("Species"), *(len(row.species.display_name) for row in rows)],
        default=len("Species"),
    )
    count_width = max(len("Number Seen"), *(len(str(row.count)) for row in rows))
    percent_width = max(len("% Frequency"), *(len(format_percent(row.percent_frequency, precision)) for row in rows))

    lines = [
        "MATE ROV 2026 eDNA Frequency Analysis",
        f"Total organisms seen: {total}",
        "",
        f"{'Species':<{species_width}}  {'Number Seen':>{count_width}}  {'% Frequency':>{percent_width}}",
        f"{'-' * species_width}  {'-' * count_width}  {'-' * percent_width}",
    ]
    for row in rows:
        lines.append(
            f"{row.species.display_name:<{species_width}}  "
            f"{row.count:>{count_width}}  "
            f"{format_percent(row.percent_frequency, precision):>{percent_width}}"
        )
    if total:
        lines.extend(
            [
                "",
                f"Formula: percent frequency = number seen / {total} * 100",
            ]
        )
    return "\n".join(lines)


def build_csv_text(rows: Sequence[FrequencyRow], *, precision: int = 2) -> str:
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["Species", "Scientific name", "Number Seen", "% Frequency"])
    for row in rows:
        writer.writerow(
            [
                row.species.common_name,
                row.species.scientific_name,
                row.count,
                format_percent(row.percent_frequency, precision),
            ]
        )
    writer.writerow([])
    writer.writerow(["Total", "", total_seen(rows), format_percent(100.0 if total_seen(rows) else 0.0, precision)])
    return output.getvalue()
