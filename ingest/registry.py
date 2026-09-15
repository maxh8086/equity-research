"""Adapter discovery, the extracted_by → source class registry, and startup checks."""

import importlib
import inspect
import pkgutil
from collections.abc import Iterable, Iterator, Mapping

import ingest
from core.config import Settings
from core.sources import DeploymentMode, SourceClass
from ingest.base import NAME_RE, Adapter

# Shared machinery, not sources. tests/test_architecture.py mirrors this list.
INFRA_MODULES = frozenset(
    {"ingest.__main__", "ingest.base", "ingest.http", "ingest.registry", "ingest.schema"}
)


class AdapterDeclarationError(Exception):
    pass


class StartupRefused(Exception):
    pass


def validate_declaration(cls: type[Adapter]) -> None:
    name = getattr(cls, "name", None)
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise AdapterDeclarationError(f"{cls.__qualname__}: name must be snake_case, got {name!r}")
    if not isinstance(getattr(cls, "source_class", None), SourceClass):
        raise AdapterDeclarationError(f"{cls.__qualname__}: source_class must be a SourceClass")
    stores = getattr(cls, "target_stores", None)
    if not (
        isinstance(stores, tuple) and stores and all(isinstance(s, str) and s for s in stores)
    ):
        raise AdapterDeclarationError(
            f"{cls.__qualname__}: target_stores must be a non-empty tuple of table names"
        )


def index_adapters(classes: Iterable[type[Adapter]]) -> dict[str, type[Adapter]]:
    adapters: dict[str, type[Adapter]] = {}
    for cls in classes:
        validate_declaration(cls)
        if adapters.get(cls.name, cls) is not cls:
            raise AdapterDeclarationError(f"two adapters are named {cls.name!r}")
        adapters[cls.name] = cls
    return adapters


def _concrete_subclasses(cls: type) -> Iterator[type]:
    for sub in cls.__subclasses__():
        if not inspect.isabstract(sub):
            yield sub
        yield from _concrete_subclasses(sub)


def discover() -> dict[str, type[Adapter]]:
    """Import every adapter module under ingest/ and index the concrete adapters."""
    for module in pkgutil.walk_packages(ingest.__path__, "ingest."):
        if module.name not in INFRA_MODULES:
            importlib.import_module(module.name)
    return index_adapters(
        cls for cls in _concrete_subclasses(Adapter) if cls.__module__.startswith("ingest.")
    )


def source_class_by_extracted_by(adapters: Mapping[str, type[Adapter]]) -> dict[str, SourceClass]:
    """For read-time filtering: which source class produced rows tagged with `extracted_by`."""
    return {cls.extracted_by(): cls.source_class for cls in adapters.values()}


def check_startup(settings: Settings, adapters: Mapping[str, type[Adapter]]) -> None:
    problems = []
    if unknown := sorted(set(settings.source_switches) - set(adapters)):
        problems.append(
            "switches name no adapter: "
            + ", ".join(f"EQUITY_SOURCE_{n.upper()}_ENABLED" for n in unknown)
        )
    if settings.deployment_mode is DeploymentMode.COMMERCIAL:
        if settings.web_scraping_enabled:
            problems.append("commercial mode with EQUITY_WEB_SCRAPING_ENABLED=true")
        if scrapers := sorted(
            name
            for name, cls in adapters.items()
            if cls.source_class is SourceClass.WEB_SCRAPE and settings.source_switches.get(name)
        ):
            problems.append(f"commercial mode with web_scrape adapters enabled: {scrapers}")
    if problems:
        raise StartupRefused("; ".join(problems))
