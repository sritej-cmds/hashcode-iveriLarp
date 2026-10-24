from pathlib import Path

import pytest
from pydantic import ValidationError

from adaptive_restler.selection.config import ModelConfig, load_model_config


def test_defaults():
    c = ModelConfig()
    assert c.small_model == "qwen2.5:3b-instruct" and c.large_model == "qwen2.5:14b-instruct"
    assert c.cache_enabled is False and c.fallback_strategy == "deterministic"


@pytest.mark.parametrize(
    "bad",
    [
        {"unknown": 1},
        {"candidate_limit": 0},
        {"llm_timeout_seconds": 0.0},
        {"base_url": "ftp://x"},
        {"log_level": "TRACE"},
        {"fallback_strategy": "random"},
        {"small_model": " "},
    ],
)
def test_rejects_bad_values(bad):
    with pytest.raises(ValidationError):
        ModelConfig(**bad)


def test_config_is_frozen():
    with pytest.raises(ValidationError):
        ModelConfig().seed = 1


def test_load_from_yaml(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("selection_model:\n  small_model: tiny\n  candidate_limit: 3\n", encoding="utf-8")
    c = load_model_config(path)
    assert c.small_model == "tiny" and c.candidate_limit == 3


def test_missing_section_or_empty_file_gives_defaults(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("planner:\n  provider: mock\n", encoding="utf-8")
    assert load_model_config(path) == ModelConfig()
    path.write_text("", encoding="utf-8")
    assert load_model_config(path) == ModelConfig()


def test_non_mapping_section_rejected(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("selection_model: 5\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_model_config(path)


def test_repo_default_yaml_loads():
    path = Path(__file__).resolve().parents[2] / "config" / "default.yaml"
    assert isinstance(load_model_config(path), ModelConfig)