"""Providers produce strings only; they never receive a ledger or broker tool."""
from __future__ import annotations
from dataclasses import dataclass
import copy
from typing import Protocol
from .ai_contracts import ROLES
from .ai_validation import strict_json, ValidationError
from .models import canonical

@dataclass(frozen=True)
class AgentRequest:
    role: str
    instructions: str
    input_json: str
    schema_json: str

class Provider(Protocol):
    label: str
    def complete(self, request: AgentRequest, timeout_seconds: float) -> str: ...

class ProviderError(RuntimeError):
    pass

class OfflineMockProvider:
    """Explicit deterministic test double. This class performs NO inference."""
    label = "offline_mock_no_inference"
    def complete(self, request, timeout_seconds):
        data = strict_json(request.input_json)
        packet = data["packet"]
        symbol = packet["sources"][0]["symbol"]
        ids = [s["source_id"] for s in packet["sources"] if s["symbol"] == symbol]
        common = {"role": request.role, "packet_id": packet["packet_id"], "evidence_ids": ids}
        if request.role == "analyst":
            result = {**common, "symbol": symbol, "bias": "bullish", "confidence": 0.6,
                      "rationale": "Deterministic mock fixture; no predictive inference."}
        elif request.role == "critic":
            result = {**common, "verdict": "accept", "concerns": ["Synthetic fixture only; no investment signal."]}
        else:
            result = {**common, "symbol": symbol, "action": "buy", "confidence": 0.6,
                      "rationale": "Test the local deterministic risk gate only; no predictive inference."}
        return canonical(result)

class RecordedReplayProvider:
    """Replay outputs, bound to identical input data. No new LLM inference."""
    label = "recorded_replay_no_new_inference"
    def __init__(self, packet, responses, provenance="unspecified"):
        if type(responses) is not dict or set(responses) != set(ROLES):
            raise ValidationError("Replay must contain exactly three role outputs")
        self._packet = canonical(packet)
        self._responses = copy.deepcopy(responses)
        self.declared_provenance = provenance
    def complete(self, request, timeout_seconds):
        if canonical(strict_json(request.input_json)["packet"]) != self._packet:
            raise ProviderError("Replay packet mismatch")
        return canonical(self._responses[request.role])

class OpenAIResponsesProvider:
    """Optional authenticated SDK client supplied by a trusted runtime.

    No SDK construction, environment reads, key storage, or implicit network
    activation. This adapter sends only validated synthetic data when called.
    """
    label = "openai_responses_runtime"
    def __init__(self, *, client, model: str):
        if type(model) is not str or not 1 <= len(model) <= 100 or any(c.isspace() for c in model):
            raise ValueError("An explicit model identifier is required")
        if str(getattr(client, "base_url", "")).rstrip("/") != "https://api.openai.com/v1":
            raise ValueError("Only the official OpenAI API endpoint is accepted")
        self._client, self._model = client, model
    def complete(self, request, timeout_seconds):
        # No raw error/body/client repr is emitted: those can contain credentials.
        try:
            client = self._client.with_options(timeout=timeout_seconds, max_retries=0)
            response = client.responses.create(
                model=self._model,
                instructions=request.instructions,
                input=[{"role": "user", "content": request.input_json}],
                text={"format": {"type": "json_schema", "name": f"trading_{request.role}",
                                  "strict": True, "schema": strict_json(request.schema_json)}},
                tools=[], tool_choice="none", store=False,
                max_output_tokens=2048,
            )
            if getattr(response, "status", None) != "completed" or getattr(response, "error", None):
                raise ProviderError("Incomplete model response")
            output = getattr(response, "output", None)
            if not isinstance(output, list) or not output:
                raise ProviderError("Missing response output items")
            found_text = False
            for item in output:
                if getattr(item, "type", None) not in ("message", "reasoning"):
                    raise ProviderError("Unexpected model output item")
                for content in getattr(item, "content", []):
                    if getattr(content, "type", None) != "output_text":
                        raise ProviderError("Refused or unexpected model content")
                    found_text = True
            text = getattr(response, "output_text", None)
            if not found_text or not isinstance(text, str) or not text:
                raise ProviderError("Missing model output")
            return text
        except Exception:
            raise ProviderError("Model request failed or returned unusable output") from None
