"""Fixtures the unit tests share: the deployment mode."""
import pytest

from src import runtime


@pytest.fixture
def container(monkeypatch):
	monkeypatch.setenv(runtime.DEPLOYMENT_ENV, "docker")


@pytest.fixture
def dev(monkeypatch):
	monkeypatch.delenv(runtime.DEPLOYMENT_ENV, raising=False)
