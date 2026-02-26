"""
providers/__init__.py

Public API for the providers package.

Usage:
    from providers import get_provider
    provider = get_provider("azure")   # returns AzureProvider()
    provider = get_provider("aws")     # returns AWSProvider()
"""

from .base import CloudProvider, ProvisionConfig, VMStatus
from .azure_provider import AzureProvider
from .aws_provider import AWSProvider
from typing import Dict, Type

# Registry — add new providers here, nothing else needs to change
_PROVIDERS: Dict[str, Type[CloudProvider]] = {
    "azure": AzureProvider,
    "aws": AWSProvider,
}


def get_provider(name: str) -> CloudProvider:
    """
    Factory function. Returns an instantiated provider by name.

    Args:
        name: "azure" | "aws"  (case-insensitive)

    Raises:
        ValueError: if the provider name is not registered.
    """
    key = name.lower().strip()
    provider_class = _PROVIDERS.get(key)
    if not provider_class:
        supported = ", ".join(_PROVIDERS.keys())
        raise ValueError(
            f"Unknown provider '{name}'. Supported providers: {supported}"
        )
    return provider_class()


__all__ = ["get_provider", "CloudProvider", "ProvisionConfig", "VMStatus"]
