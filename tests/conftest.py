"""Safety net for every test: nothing may ever reach the real Telegram."""

import pytest
import requests


@pytest.fixture(autouse=True)
def no_real_telegram(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to make a real web request through requests.post")
    monkeypatch.setattr(requests, "post", refuse)  # tests that send use their own fake session instead
