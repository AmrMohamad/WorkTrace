class WorkTraceError(Exception):
    """Base error with a user-safe message."""


class ConfigurationError(WorkTraceError):
    pass


class DatabaseError(WorkTraceError):
    pass


class SourceError(WorkTraceError):
    pass


class InvalidCredentials(SourceError):
    pass


class PermissionDenied(SourceError):
    pass


class SourceObjectUnavailable(SourceError):
    pass


class PermanentSourceError(SourceError):
    pass


class RetryExhausted(SourceError):
    pass


class ScopeViolation(WorkTraceError):
    pass


class NotFound(WorkTraceError):
    pass


class VaultError(WorkTraceError):
    """Base error for encrypted Jira vault and recovery operations."""


class VaultFormatError(VaultError):
    pass


class VaultIntegrityError(VaultError):
    pass


class KeychainError(VaultError):
    pass


class RecoveryError(VaultError):
    pass
