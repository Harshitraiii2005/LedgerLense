"""PII masking applied to every agent response, whatever the pipeline returned.

Masking happens on the *result*, not the query, so a pipeline that renames a
field (e.g. {$project: {who: "$customer_id"}}) is caught by value pattern too.
"""

import re
from typing import Any

PII_KEYS = {"customer_id", "user", "username", "email", "name", "phone"}
CUSTOMER_ID = re.compile(r"^C\d{6}$")


def mask_value(value: Any) -> Any:
    if isinstance(value, str) and len(value) > 4:
        return value[:2] + "*" * (len(value) - 4) + value[-2:]
    return "***"


def mask(node: Any, key: str | None = None) -> Any:
    if isinstance(node, dict):
        return {k: mask(v, k) for k, v in node.items()}
    if isinstance(node, list):
        return [mask(v, key) for v in node]
    if (key in PII_KEYS and node is not None) or (isinstance(node, str) and CUSTOMER_ID.match(node)):
        return mask_value(node)
    return node
