class DuplicateLeadError(Exception):
    """A lead with the same (source, external_id) already exists."""

    def __init__(self, source: str, external_id: str | None) -> None:
        super().__init__(f"A lead with source={source!r} and external_id={external_id!r} already exists")
        self.source = source
        self.external_id = external_id
