"""Mail-app policy adapter over the maintained Stylome Python SDK."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import httpx
from stylome_client import AuthenticatedClient
from stylome_client.api.default import score_text
from stylome_client.errors import UnexpectedStatus
from stylome_client.models import (
    ApiScore12,
    ApiScoreRequest,
    ApiScoreRequestModel,
    ApiScoreRequestOperatingPointType0,
)

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
        session: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("STYLOME_API_KEY is required")
        if operating_point not in OPERATING_POINTS:
            raise ValueError("unsupported Stylome operating point")
        if not evaluation_version:
            raise ValueError("an explicit evaluation version is required")
        self.operating_point = operating_point
        self.evaluation_version = evaluation_version
        self.sdk = AuthenticatedClient(
            base_url="https://stylome.io",
            token=api_key,
            timeout=httpx.Timeout(150, connect=10),
            follow_redirects=False,
            raise_on_unexpected_status=True,
        )
        if session is not None:
            self.sdk.set_httpx_client(session)

    def close(self) -> None:
        self.sdk.get_httpx_client().close()

    def predict(self, text: str) -> StylomeResult:
        text = normalize_text(text)
        if not 200 <= len(text) <= 10_000:
            raise StylomeError("Stylome requires 200–10,000 normalized characters")
        identity = f"{MODEL}\0{self.operating_point}\0{self.evaluation_version}\0{text}"
        key = "mlac-stylome-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        try:
            response = score_text.sync_detailed(
                client=self.sdk,
                body=ApiScoreRequest(
                    text=text,
                    model=ApiScoreRequestModel(MODEL),
                    operating_point=ApiScoreRequestOperatingPointType0(self.operating_point),
                ),
                idempotency_key=key,
            )
        except UnexpectedStatus as exc:
            raise StylomeError(f"Stylome HTTP {exc.status_code}; no retry was sent") from None
        except httpx.RequestError:
            raise StylomeError(
                "Stylome transport failure; completion/billing is unknown. "
                "No retry was sent; retain this database before a manual rerun."
            ) from None
        except ValueError, TypeError, KeyError, AttributeError:
            raise StylomeError(
                "Stylome response contract mismatch; response was not stored"
            ) from None
        if response.status_code != 200:
            raise StylomeError(f"Stylome HTTP {response.status_code}; no retry was sent")
        if not isinstance(response.parsed, ApiScore12):
            raise StylomeError("Stylome response contract mismatch; response was not stored")
        data = response.parsed.to_dict()
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
