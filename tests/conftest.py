from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

# Make sure no real keys or Vercel flags leak into tests.
for var in (
    "GROQ_API_KEY",
    "GEMINI_API_KEY",
    "TAVILY_API_KEY",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_KEY",
    "VERCEL",
):
    os.environ.pop(var, None)
os.environ["DOTENV_DISABLED"] = "1"

from fakes import make_services  # noqa: E402

from docpilot.services import set_services  # noqa: E402

SESSION = "test-session-0001"


@pytest.fixture
def services():
    svc = make_services()
    set_services(svc)
    yield svc
    set_services(None)


@pytest.fixture
def client(services):
    from fastapi.testclient import TestClient

    import app as app_module

    with TestClient(app_module.app) as c:
        yield c
