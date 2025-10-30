"""
Reusable logging utility for uv run scripts.

Usage:
    from script_logger import setup_logger

    logger = setup_logger(
        script_name="my_script",
        log_dir="./logs"
    )

    logger.info("Processing started")
    logger.warning("Something might be wrong")
    logger.error("Something failed")
"""

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


def setup_logger(
        script_name: str,
        log_dir: str | Path = "./logs",
        log_level: int = logging.INFO,
        console_level: int = logging.INFO,
        file_level: int = logging.DEBUG,
        include_timestamp_in_filename: bool = True,
) -> logging.Logger:
    """
    Set up logging to both file and console with sensible defaults.

    Args:
        script_name: Name of the script (used in logger name and log filename)
        log_dir: Directory to store log files (default: ./logs)
        log_level: Overall log level (default: INFO)
        console_level: Log level for console output (default: INFO)
        file_level: Log level for file output (default: DEBUG)
        include_timestamp_in_filename: Add timestamp to log filename (default: True)

    Returns:
        Configured logger instance

    """
    # Create log directory if it doesn't exist
    log_dir_path = Path(log_dir)
    log_dir_path.mkdir(parents=True, exist_ok=True)

    # Create log filename
    if include_timestamp_in_filename:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        log_file = log_dir_path / f"{script_name}_{timestamp}.log"
    else:
        log_file = log_dir_path / f"{script_name}.log"

    # Create logger
    logger = logging.getLogger(script_name)
    logger.setLevel(log_level)

    # Remove existing handlers to avoid duplicates if called multiple times
    logger.handlers.clear()

    # File handler - detailed logs with timestamps
    file_handler = logging.FileHandler(log_file, mode='w', encoding='utf-8')
    file_handler.setLevel(file_level)
    file_formatter = logging.Formatter(
        '%(asctime)s - %(message)s',  # No level names
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(file_formatter)

    # Console handler - cleaner output without timestamps
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(console_level)
    console_formatter = logging.Formatter('%(message)s')
    console_handler.setFormatter(console_formatter)

    # Add handlers
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    # Log the start
    logger.info(f"Log file: {log_file}")
    logger.info("=" * 80)

    return logger


def log_section(logger: logging.Logger, title: str, separator: str = "-") -> None:
    """
    Log a section header for better readability.

    Args:
        logger: Logger instance
        title: Section title
        separator: Character to use for separator line

    Example:
        log_section(logger, "Processing Dataset")

        ──────────────── Processing Dataset ────────────────
    """
    width = 80
    title_with_spaces = f" {title} "
    sep_length = (width - len(title_with_spaces)) // 2
    line = separator * sep_length + title_with_spaces + separator * sep_length
    logger.info(f"\n{line}")


def log_dict(logger: logging.Logger, data: dict, title: Optional[str] = None, indent: str = "  ") -> None:
    """
    Log a dictionary in a readable format.

    Args:
        logger: Logger instance
        data: Dictionary to log
        title: Optional title for the dictionary
        indent: Indentation string for values

    Example:
        log_dict(logger, {"name": "test", "value": 123}, title="Config")
        Config:
          name: test
          value: 123
    """
    if title:
        logger.info(f"{title}:")
    for key, value in data.items():
        logger.info(f"{indent}{key}: {value}")


class LogContext:
    """
    Context manager for logging sections with automatic success/failure reporting.

    Example:
        with LogContext(logger, "Creating folder"):
        ...     # do something
        ...     pass
        ✓ Creating folder - completed successfully

        with LogContext(logger, "Processing data"):
        ...     raise ValueError("Something went wrong")
        ✗ Processing data - failed: Something went wrong
    """

    def __init__(self, logger: logging.Logger, operation: str):
        self.logger = logger
        self.operation = operation

    def __enter__(self):
        self.logger.info(f"Starting: {self.operation}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self.logger.info(f"✓ {self.operation} - completed successfully")
        else:
            self.logger.error(f"✗ {self.operation} - failed: {exc_val}", exc_info=True)
        return False  # Don't suppress exceptions