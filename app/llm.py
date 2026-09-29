"""LLM access layer.

Instructor wraps the provider client so the model is *forced* into the exact
:class:`~app.models.AnalysisReportCore` JSON contract; validation failures are
retried with the pydantic error appended to the conversation.

Providers:

* ``openai``    - OpenAI or any OpenAI-compatible endpoint (vLLM, Ollama, TEI,
                  Azure-style gateways) via ``OPENAI_BASE_URL``.
* ``anthropic`` - Claude via ``instructor.from_anthropic``.
* ``mock``      - deterministic, offline stub. The rule engine still runs, so
                  the full API/persistence path is exercisable without a key.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel

from app.config import Settings

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)


class LLMError(RuntimeError):
    """Raised when the LLM call cannot be made or keeps failing validation."""


@dataclass(slots=True)
class LLMResult:
    data: BaseModel
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    model: str = ""


def _usage_from(raw: Any) -> tuple[int | None, int | None]:
    usage = getattr(raw, "usage", None)
    if usage is None:
        return None, None
    prompt = getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", None)
    completion = getattr(usage, "completion_tokens", None) or getattr(usage, "output_tokens", None)
    return prompt, completion


class StructuredLLM:
    """Async structured-output client."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.provider = settings.llm_provider
        self.model = settings.llm_model
        self._client: Any | None = None

    # ------------------------------------------------------------------ #
    @property
    def available(self) -> bool:
        return self.settings.llm_configured

    def _build_client(self) -> Any:
        if self._client is not None:
            return self._client
        provider = self.settings.llm_provider

        if provider == "mock":
            self._client = None
            return None

        try:
            import instructor
        except ImportError as exc:  # pragma: no cover - dependency issue
            raise LLMError("instructor is not installed; run `pip install instructor`") from exc

        mode = {
            "tools": instructor.Mode.TOOLS,
            "json": instructor.Mode.JSON,
            "md_json": instructor.Mode.MD_JSON,
        }[self.settings.llm_response_mode]

        if provider == "openai":
            if not self.settings.openai_api_key:
                raise LLMError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
            from openai import AsyncOpenAI

            self._client = instructor.from_openai(
                AsyncOpenAI(
                    api_key=self.settings.openai_api_key,
                    base_url=self.settings.openai_base_url or None,
                    timeout=self.settings.llm_timeout_seconds,
                ),
                mode=mode,
            )
        elif provider == "anthropic":
            if not self.settings.anthropic_api_key:
                raise LLMError("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic")
            from anthropic import AsyncAnthropic

            self._client = instructor.from_anthropic(
                AsyncAnthropic(api_key=self.settings.anthropic_api_key)
            )
        else:  # pragma: no cover - guarded by settings Literal
            raise LLMError(f"unsupported LLM provider: {provider}")
        return self._client

    # ------------------------------------------------------------------ #
    async def extract(
        self,
        prompt: str,
        response_model: type[ModelT],
        *,
        system: str | None = None,
        max_retries: int | None = None,
    ) -> LLMResult:
        """Run one structured completion against ``response_model``."""
        if self.settings.llm_provider == "mock":
            return self._mock_extract(response_model)

        client = self._build_client()
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        retries = self.settings.llm_max_retries if max_retries is None else max_retries
        try:
            if self.settings.llm_provider == "anthropic":
                # Anthropic takes the system prompt as a top-level argument.
                kwargs: dict[str, Any] = {
                    "model": self.model,
                    "max_tokens": self.settings.llm_max_tokens,
                    "temperature": self.settings.llm_temperature,
                    "messages": [m for m in messages if m["role"] != "system"],
                    "response_model": response_model,
                    "max_retries": retries,
                }
                if system:
                    kwargs["system"] = system
                data, raw = await client.messages.create_with_completion(**kwargs)
            else:
                data, raw = await client.chat.completions.create_with_completion(
                    model=self.model,
                    temperature=self.settings.llm_temperature,
                    max_tokens=self.settings.llm_max_tokens,
                    messages=messages,
                    response_model=response_model,
                    max_retries=retries,
                )
        except Exception as exc:  # noqa: BLE001 - surfaced as a domain error
            logger.exception("structured completion failed")
            raise LLMError(f"LLM comparison failed: {exc}") from exc

        prompt_tokens, completion_tokens = _usage_from(raw)
        return LLMResult(
            data=data,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=self.model,
        )

    # ------------------------------------------------------------------ #
    def _mock_extract(self, response_model: type[ModelT]) -> LLMResult:
        """Offline stub: valid, empty findings - the rule engine stays authoritative."""
        if response_model.__name__ == "AuditReportCore":
            from app.models import AuditReportCore  # noqa: PLC0415 - avoid a cycle at import time

            return LLMResult(
                data=AuditReportCore(
                    package="unknown",
                    target_os="unknown",
                    audit_verdict="MATCH",
                    confidence_score=0.6,
                    summary=(
                        "Deterministic rule comparison only - LLM_PROVIDER=mock, so no "
                        "semantic review was performed."
                    ),
                    discrepancies=[],
                    documentations_to_update=[],
                    scripts_to_update=[],
                    notes="mock provider: LLM findings intentionally omitted",
                ),
                model="rules-only",
            )
        raise LLMError(f"mock provider cannot satisfy {response_model.__name__}")


__all__ = ["LLMError", "LLMResult", "StructuredLLM"]
