"""One JSON object per line. Callers pass ids and outcomes, never member details."""

from __future__ import annotations

import json
import logging
import socket
from datetime import UTC, datetime

_STANDARD = set(vars(logging.makeLogRecord({}))) | {"message", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
            "host": socket.gethostname(),
            **{k: v for k, v in vars(record).items() if k not in _STANDARD},
        }
        if record.exc_info and record.exc_info[0] is not None:
            # The type only: a database error's text can quote row values.
            line["error"] = record.exc_info[0].__name__
        return json.dumps(line, default=str)


def configure() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    # Its request lines repeat what the attempt logs already say.
    logging.getLogger("httpx2").setLevel(logging.WARNING)
