"""Engine adapters: each is an isolated module implementing the EngineAdapter port."""

from .ddgs_engine import DdgsAdapter
from .searxng import SearxngAdapter

__all__ = ["SearxngAdapter", "DdgsAdapter"]
