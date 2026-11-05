"""Who may use which tools. Deny-by-default for unknown roles."""
from app.models.user import Role
from app.schemas.plan import ToolCategory
from app.tools.types import ToolPermissionDenied, ToolSpec

_ALL = set(ToolCategory)

ROLE_ALLOWED_CATEGORIES: dict[str, set[ToolCategory]] = {
    Role.ADMIN.value: _ALL,
    Role.MANAGER.value: _ALL,
    Role.OPERATOR.value: _ALL,
    Role.VIEWER.value: set(),  # viewers never trigger tools
}


class ToolPolicy:
    def __init__(self, disabled: set[str] | None = None):
        self.disabled: set[str] = disabled or set()  # kill switch for individual tools

    def allowed(self, role: str, spec: ToolSpec) -> bool:
        if spec.name in self.disabled:
            return False
        return spec.category in ROLE_ALLOWED_CATEGORIES.get(role, set())

    def check(self, role: str, spec: ToolSpec) -> None:
        if not self.allowed(role, spec):
            raise ToolPermissionDenied(f"Role '{role}' may not use tool '{spec.name}'")
