"""Module util: các hàm phụ trợ nhỏ, dùng chung."""

import re

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_TRIM_DASHES = re.compile(r"^-+|-+$")


def slugify(s: str) -> str:
    """Chuyển một chuỗi bất kỳ thành slug an toàn cho URL/tên thư mục."""
    s = s.strip().lower()
    s = _NON_ALNUM.sub("-", s)
    s = _TRIM_DASHES.sub("", s)
    if s == "":
        return "item"
    return s