"""External sample."""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .api import *

__all__ = ["Client", "ping"]
