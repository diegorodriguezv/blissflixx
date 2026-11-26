class ApiError(Exception):
    """
    An error whose message is meant for whoever is using the interface.

    Distinct from any other exception because it carries its own HTTP status and
    its message is written to be read, not diagnosed. The dispatcher returns it
    as-is; anything else comes back as a 500 with a traceback, which is right for
    a bug and wrong for "the Reddit archive is busy".

    The status defaults to 400 because most of these are a request that cannot be
    honoured as written -- a missing channel ID, an unknown feed. A dependency
    being unavailable is a different thing and says so: see _SERVICE_UNAVAILABLE.
    """

    #: The dependency answered, but cannot answer usefully right now.
    _SERVICE_UNAVAILABLE = 503

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status
