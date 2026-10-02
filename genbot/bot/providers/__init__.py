"""Провайдеры генерации по ключам из настроек."""
from __future__ import annotations

from ..config import Config
from .base import GenResult, Media, Provider, ProviderError
from .mage import MageProvider
from .mock import MockProvider
from .vilva import VilvaProvider

__all__ = ["GenResult", "Media", "Provider", "ProviderError", "build_providers"]


def build_providers(cfg: Config) -> dict[str, object]:
    """{"mage": ..., "vilva": ..., "mock": ...} — только те, для которых есть ключи."""
    providers: dict[str, object] = {}
    if cfg.mage_key:
        providers["mage"] = MageProvider(cfg.mage_key, cfg.mage_base_url)
    if cfg.vilva_key:
        providers["vilva"] = VilvaProvider(cfg.vilva_key, cfg.vilva_url)
    if cfg.use_mock:
        providers["mock"] = MockProvider()
    return providers
