"""Model/runtime settings for the LLM stages. Loaded from config/default.yaml.

Request timeout for real runs is ExecutionBudget.request_timeout_seconds;
``llm_timeout_seconds`` bounds a single model call.
"""

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints, field_validator

from adaptive_restler.models import NonNegFloat, PosFloat, PosInt

__all__ = ["CONFIG_SECTION", "ModelConfig", "load_model_config"]

CONFIG_SECTION = "selection_model"
_Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["ollama"] = "ollama"
    base_url: _Name = "http://localhost:11434"
    small_model: _Name = "qwen2.5:3b-instruct"
    large_model: _Name = "qwen2.5:14b-instruct"
    llm_timeout_seconds: PosFloat = 30.0
    candidate_limit: PosInt = 20
    ranking_batch_size: PosInt = 10
    max_output_tokens: PosInt = 1000
    max_prompt_chars: PosInt = 12000
    context_tokens: PosInt = 8192
    temperature: NonNegFloat = 0.0
    seed: Annotated[int, Field(strict=True)] = 0
    cache_enabled: StrictBool = False
    fallback_strategy: Literal["deterministic", "feedback"] = "deterministic"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("base_url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return value


def load_model_config(path: str | Path = "config/default.yaml") -> ModelConfig:
    """Read the ``selection_model`` section. A missing section gives the defaults."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("config file must contain a mapping")
    section = data.get(CONFIG_SECTION, {})
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ValueError(f"'{CONFIG_SECTION}' must be a mapping")
    return ModelConfig(**section)