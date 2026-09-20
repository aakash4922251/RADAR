"""Compliant public procurement source acquisition."""

from acquisition.registry import get_connector, register_connector
from acquisition.scanner import scan_source

__all__ = ["get_connector", "register_connector", "scan_source"]
