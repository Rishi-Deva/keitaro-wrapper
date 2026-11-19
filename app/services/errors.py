class ServiceError(Exception):
    """Business error with HTTP status and stable code, safe to show to the user."""

    def __init__(self, status: int, code: str, message: str, details: object = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details
