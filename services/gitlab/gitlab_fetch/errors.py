# =============================================================================
# services/gitlab/gitlab_fetch/errors.py — Error operativo con código del contrato source-result
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

class FetchError(Exception):
    def __init__(self, code, message, exit_code=1):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


class ReadOnlyError(FetchError):
    def __init__(self, method):
        super().__init__("E_INTERNAL", f"Método {method} no permitido: el cliente es solo lectura")
