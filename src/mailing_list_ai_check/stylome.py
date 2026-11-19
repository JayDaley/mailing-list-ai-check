"""Stylome 1.2 public API client. No Pangram fraction conversion is applied."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import requests

API_URL = "https://stylome.io/api/v1/score"
MODEL = "stylome-1.2-large"
DEFAULT_EVALUATION = "eval_12_002"
OPERATING_POINTS = ("0.01%", "0.1%")
LABELS = ("human_written", "ai_edited", "ai_generated")


def normalize_text(text: str) -> str:
    """Match 1.2 input normalization and Unicode-character metering."""
    return " ".join(text.encode("utf-8", "replace").decode("utf-8").split())


class StylomeError(Exception):
    """A request failed or the response did not match the selected contract."""


@dataclass(frozen=True)
class StylomeResult:
    label: str
    confidence: str
    model: str
    operating_point: str
    evaluation_version: str
    raw: dict[str, Any]

    @property
    def any_ai(self) -> bool:
        return self.label != "human_written"


class StylomeClient:
    """One billed POST per predict call, with a stable replay key.

    No automatic retry follows an ambiguous transport failure. A manual rerun
    reuses the key; standard-retention keys support replay for 24 hours. A key
    with zero content retention cannot use this API's idempotency mechanism.
    """

    def __init__(
        self,
        api_key: str,
        *,
        operating_point: str = "0.01%",
        evaluation_version: str = DEFAULT_EVALUATION,
        session: requests.Session | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("STYLOME_API_KEY is required")
        if operating_point not in OPERATING_POINTS:
            raise ValueError("unsupported Stylome operating point")
        if not evaluation_version:
            raise ValueError("an explicit evaluation version is required")
        self.operating_point = operating_point
        self.evaluation_version = evaluation_version
        self.session = session if session is not None else requests.Session()
        self._headers = {"Authorization": f"Bearer {api_key}"}

    def close(self) -> None:
        self.session.close()

    def predict(self, text: str) -> StylomeResult:
        text = normalize_text(text)
        if not 200 <= len(text) <= 10_000:
            raise StylomeError("Stylome requires 200–10,000 normalized characters")
        identity = f"{MODEL}\0{self.operating_point}\0{self.evaluation_version}\0{text}"
        key = "mlac-stylome-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        try:
            response = self.session.post(
                API_URL,
                headers={**self._headers, "Idempotency-Key": key},
                json={"text": text, "model": MODEL, "operating_point": self.operating_point},
                timeout=(10, 120),
                allow_redirects=False,
            )
        except requests.RequestException:
            # Exception strings and error bodies can contain credentials or text.
            raise StylomeError(
                "Stylome transport failure; completion/billing is unknown. "
                "No retry was sent; retain this database before a manual rerun."
            ) from None
        if response.status_code != 200:
            raise StylomeError(f"Stylome HTTP {response.status_code}; no retry was sent")
        try:
            data = response.json()
        except ValueError:
            raise StylomeError("Stylome returned invalid JSON") from None
        if not isinstance(data, dict) or (
            data.get("mock") is not False
            or data.get("model_version") != MODEL
            or data.get("operating_point") != self.operating_point
            or data.get("evaluation_version") != self.evaluation_version
            or data.get("response_version") != "stylome-public-v2"
            or data.get("label") not in LABELS
            or data.get("confidence") not in ("low", "medium", "high")
        ):
            raise StylomeError("Stylome response contract mismatch; response was not stored")
        # Receipt share tokens are capabilities. Do not persist or export them.
        retained = {key: value for key, value in data.items() if key != "receipt"}
        return StylomeResult(
            data["label"],
            data["confidence"],
            MODEL,
            self.operating_point,
            self.evaluation_version,
            retained,
        )
