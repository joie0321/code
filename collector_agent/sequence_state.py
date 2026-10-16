"""Durable, non-secret collector upload sequence state."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile


class SequenceStateError(ValueError):
    """Raised when durable sequence state cannot be safely used."""


class SequenceStateStore:
    """Atomically retain counters without retaining credentials or telemetry."""

    _VERSION = 1
    _MAX_FILE_BYTES = 1_048_576

    def __init__(self, path: Path) -> None:
        self._path = path
        self._sources = self._load()

    def get(self, collector_id: str) -> tuple[int, int]:
        state = self._sources.get(collector_id, {})
        return int(state.get("observation_sequence", 0)), int(state.get("inventory_sequence", 0))

    def update(self, collector_id: str, observation_sequence: int, inventory_sequence: int) -> None:
        if observation_sequence < 0 or inventory_sequence < 0:
            raise SequenceStateError("Collector sequence state must not be negative")
        self._sources[collector_id] = {
            "observation_sequence": observation_sequence,
            "inventory_sequence": inventory_sequence,
        }
        self._write()

    def remove(self, collector_id: str) -> None:
        if collector_id in self._sources:
            del self._sources[collector_id]
            self._write()

    def _load(self) -> dict[str, dict[str, int]]:
        if not self._path.exists():
            return {}
        try:
            if self._path.stat().st_size > self._MAX_FILE_BYTES:
                raise SequenceStateError("Collector sequence state file is too large")
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SequenceStateError(
                "Collector sequence state file cannot be read safely"
            ) from error
        if not isinstance(raw, dict) or raw.get("version") != self._VERSION:
            raise SequenceStateError("Collector sequence state file has an unsupported format")
        sources = raw.get("sources")
        if not isinstance(sources, dict):
            raise SequenceStateError("Collector sequence state file has an invalid sources section")
        validated: dict[str, dict[str, int]] = {}
        for collector_id, state in sources.items():
            if (
                not isinstance(collector_id, str)
                or not isinstance(state, dict)
                or type(state.get("observation_sequence")) is not int
                or type(state.get("inventory_sequence")) is not int
                or state["observation_sequence"] < 0
                or state["inventory_sequence"] < 0
            ):
                raise SequenceStateError("Collector sequence state file contains invalid values")
            validated[collector_id] = {
                "observation_sequence": state["observation_sequence"],
                "inventory_sequence": state["inventory_sequence"],
            }
        return validated

    def _write(self) -> None:
        temporary_path: Path | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self._path.parent, delete=False
            ) as temporary:
                json.dump(
                    {"version": self._VERSION, "sources": self._sources},
                    temporary,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, self._path)
        except OSError as error:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise SequenceStateError(
                "Collector sequence state file cannot be written safely"
            ) from error
