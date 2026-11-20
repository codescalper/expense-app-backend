"""Collects every endpoint module automatically.

Each module in app/api/v1/endpoints/ exposes a module-level ``router``; adding a new feature means
adding a file there — no shared registry to edit.
"""

import importlib
import pkgutil

from fastapi import APIRouter

from app.api.v1 import endpoints

api_router = APIRouter()

for module_info in sorted(pkgutil.iter_modules(endpoints.__path__), key=lambda m: m.name):
    module = importlib.import_module(f"{endpoints.__name__}.{module_info.name}")
    router = getattr(module, "router", None)
    if isinstance(router, APIRouter):
        api_router.include_router(router)
