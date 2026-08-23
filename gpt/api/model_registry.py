"""Backward-compatible API imports for the protocol-neutral model registry."""

from gpt.model_registry import ModelRegistry, ModelResolution, load_model_aliases

__all__ = ["ModelRegistry", "ModelResolution", "load_model_aliases"]
