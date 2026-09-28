"""Загрузчик локализаций: load("ru") / load("en"). Опечатка в языке — откат на ru."""

import importlib


def load(lang: str):
    try:
        return importlib.import_module(f".{lang}", __name__)
    except ImportError:
        return importlib.import_module(".ru", __name__)
