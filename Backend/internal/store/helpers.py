def prefix_cols(alias: str, cols: str) -> str:
    """
    Return a comma-separated column list with each column prefixed by the
    given table alias, e.g. prefix_cols("w", "id, name") => "w.id, w.name".
    """
    parts = cols.split(",")
    parts = [f"{alias}.{p.strip()}" for p in parts]
    return ", ".join(parts)