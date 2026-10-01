"""Выбор провайдеров по настройкам."""
from __future__ import annotations

from ..config import Config
from .base import ImageProvider, Media, ProviderError, VideoProvider
from .fal import FalProvider
from .mock import MockProvider
from .openrouter import OpenRouterImageProvider

__all__ = ["Media", "ProviderError", "build_providers"]


def build_providers(cfg: Config) -> tuple[ImageProvider, VideoProvider]:
    fal: FalProvider | None = None

    def get_fal() -> FalProvider:
        nonlocal fal
        if fal is None:
            fal = FalProvider(cfg.fal_key, cfg.fal_image_model, cfg.fal_video_model, cfg.fal_i2v_model)
        return fal

    image: ImageProvider
    if cfg.image_provider == "fal":
        image = get_fal()
    elif cfg.image_provider == "openrouter":
        image = OpenRouterImageProvider(cfg.openrouter_key, cfg.openrouter_image_model)
    elif cfg.image_provider == "mock":
        image = MockProvider()
    else:
        raise ValueError(f"Неизвестный IMAGE_PROVIDER: {cfg.image_provider}")

    video: VideoProvider
    if cfg.video_provider == "fal":
        video = get_fal()
    elif cfg.video_provider == "mock":
        video = MockProvider()
    else:
        raise ValueError(f"Неизвестный VIDEO_PROVIDER: {cfg.video_provider}")
    return image, video
