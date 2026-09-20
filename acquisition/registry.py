from __future__ import annotations

from collections.abc import Callable

from acquisition.base import SourceConnector
from acquisition.connectors.cppp import CPPPConnector

_CONNECTORS: dict[str, Callable[..., SourceConnector]] = {"cppp": CPPPConnector}


def register_connector(source_id: str, factory: Callable[..., SourceConnector]):
    _CONNECTORS[source_id] = factory


def get_connector(source_id: str, **kwargs) -> SourceConnector:
    try:
        return _CONNECTORS[source_id](**kwargs)
    except KeyError as exc:
        raise ValueError(f"No connector registered for source '{source_id}'") from exc


def list_connectors() -> list[str]:
    return sorted(_CONNECTORS)
