# =============================================================================
# services/gitlab/gitlab_fetch/scope.py — Se niega a operar con un token que no sea de solo lectura
# Autor: Jesús Montalvo <jmontalvo_m@outlook.com>
# =============================================================================

from .errors import FetchError

# Único conjunto de alcances aceptado: lectura. `api`, `write_*` y cualquier otro se rechazan.
READ_ONLY_SCOPES = frozenset({"read_api", "read_repository"})
SELF_PATH = "/personal_access_tokens/self"


def ensure_read_only(client):
    """Consulta los alcances del token (solo GET) y falla si puede escribir o no se puede comprobar."""
    try:
        data = client.get_json(client.api_target(SELF_PATH))
    except FetchError as exc:
        if exc.code in ("E_AUTH", "E_NETWORK", "E_RATE_LIMITED"):
            raise
        raise FetchError("E_CONFIG", "No se pudo comprobar que el token de GitLab sea de solo lectura; se rechaza por seguridad")
    scopes = data.get("scopes") if isinstance(data, dict) else None
    if not isinstance(scopes, list) or not scopes or not all(isinstance(s, str) for s in scopes):
        raise FetchError("E_CONFIG", "No se pudo comprobar que el token de GitLab sea de solo lectura; se rechaza por seguridad")
    extra = sorted(set(scopes) - READ_ONLY_SCOPES)
    if extra:
        raise FetchError(
            "E_CONFIG",
            "El token de GitLab tiene permisos que permiten escribir (" + ", ".join(extra)[:80] + "). "
            "Crea uno nuevo solo con el alcance read_api",
        )
    return sorted(scopes)
