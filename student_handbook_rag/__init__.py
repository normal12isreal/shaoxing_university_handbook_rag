"""Public Python interface for the existing student handbook knowledge base."""
from scripts.embedding_api import ApiFailure as ModelAPIError
from .client import HandbookRAG

__all__ = ['HandbookRAG', 'ModelAPIError']
