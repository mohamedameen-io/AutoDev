"""Verbose: try/except that re-raises or logs-and-raises with no real handling."""
import logging

logger = logging.getLogger(__name__)


def parse_int(text: str) -> int:
    try:
        return int(text)
    except ValueError as e:
        logger.error("Failed to parse: %s", e)
        raise
    except Exception as e:
        logger.error("Unexpected error: %s", e)
        raise


def divide(a: int, b: int) -> float:
    try:
        result = a / b
        return result
    except ZeroDivisionError:
        raise
    except Exception as e:
        logger.exception("Division failed")
        raise e
