from app.providers.base import detect_routing, yandex_model_tag
from app.providers.yandex import YandexProvider


def test_canonical_model_routing_and_tags() -> None:
    assert detect_routing("yandex/general:rc") == "yandex"
    assert yandex_model_tag("yandex/general:rc", "general") == "general:rc"


def test_legacy_model_routing_remains_supported() -> None:
    assert detect_routing("yandex-general") == "yandex"


def test_model_catalogue_uses_cli_ids() -> None:
    ids = {model.id for model in YandexProvider.list_models()}

    assert "yandex/general" in ids
