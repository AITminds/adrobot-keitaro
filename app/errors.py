class AppError(Exception):
    def __init__(
        self, message: str, status: int = 400, code: str = "validation_error", **details
    ):
        self.message = message
        self.status = status
        self.code = code
        self.details = details
        super().__init__(message)


class TrackerError(AppError):
    def __init__(self, message: str, *, uncertain=False, status=502):
        super().__init__(message, status, "tracker_error", uncertain=uncertain)
        self.uncertain = uncertain
