"""Выбор провайдеров по настройкам."""
from __future__ import annotations

from ..config import Config
from .base import ImageProvider, Media, ProviderError, VideoProvider
from .fal import FalProvider
from .mage import MageProvider
from .mock import MockProvider
from .openrouter import OpenRouterImageProvider
from .vilva import VilvaProvider

__all__ = ["Media", "ProviderError", "build_providers"]


def build_providers(cfg: Config) -> tuple[ImageProvider, VideoProvider]:
    fal: FalProvider | None = None
    mage: MageProvider | None = None
    vilva: VilvaProvider | None = None

    def get_fal() -> FalProvider:
        nonlocal fal
        if fal is None:
            fal = FalProvider(cfg.fal_key, cfg.fal_image_model, cfg.fal_video_model, cfg.fal_i2v_model)
        return fal

    def get_mage() -> MageProvider:
        nonlocal mage
        if mage is None:
            mage = MageProvider(
                cfg.mage_key, cfg.mage_base_url, cfg.mage_submit_path, cfg.mage_status_path,
                cfg.mage_image_arch, cfg.mage_image_model, None, cfg.mage_video_arch, cfg.mage_video_model,
            )
        return mage

    def get_vilva() -> VilvaProvider:
        nonlocal vilva
        if vilva is None:
            vilva = VilvaProvider(cfg.vilva_key, cfg.vilva_url, cfg.vilva_image_model, cfg.vilva_video_model)
        return vilva

    makers = {"fal": get_fal, "mage": get_mage, "vilva": get_vilva}

    image: ImageProvider
    if cfg.image_provider in makers:
        image = makers[cfg.image_provider]()
    elif cfg.image_provider == "openrouter":
        image = OpenRouterImageProvider(cfg.openrouter_key, cfg.openrouter_image_model)
    elif cfg.image_provider == "mock":
        image = MockProvider()
    else:
        raise ValueError(f"Неизвестный IMAGE_PROVIDER: {cfg.image_provider}")

    video: VideoProvider
    if cfg.video_provider in makers:
        video = makers[cfg.video_provider]()
    elif cfg.video_provider == "mock":
        video = MockProvider()
    else:
        raise ValueError(f"Неизвестный VIDEO_PROVIDER: {cfg.video_provider}")
    return image, video
