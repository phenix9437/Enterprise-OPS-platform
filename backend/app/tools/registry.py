"""Turns raw MCP tool definitions into ToolSpecs, deriving risk from the tool's own annotations."""
from collections import defaultdict

from app.schemas.plan import ActionType, RiskLevel, ToolCategory
from app.tools.types import ToolNotFound, ToolSpec


def spec_from_mcp_tool(server: str, category: ToolCategory, tool) -> ToolSpec:
    """Risk is derived from the MCP annotations the tool author declared.

    Missing annotations fall back to the MCP spec defaults, which are the *pessimistic* ones
    (not read-only, destructive, open-world). So a tool nobody annotated is treated as HIGH risk.
    """
    a = tool.annotations
    read_only = bool(a.read_only_hint) if a and a.read_only_hint is not None else False
    destructive = a.destructive_hint if a and a.destructive_hint is not None else True
    idempotent = bool(a.idempotent_hint) if a and a.idempotent_hint is not None else False
    open_world = a.open_world_hint if a and a.open_world_hint is not None else True

    if read_only:
        action, risk = ActionType.READ, RiskLevel.LOW
    else:
        action = ActionType.WRITE
        risk = RiskLevel.HIGH if (destructive or open_world) else RiskLevel.MEDIUM

    return ToolSpec(
        name=f"{server}.{tool.name}",
        server=server,
        remote_name=tool.name,
        category=category,
        description=(tool.description or "").strip(),
        input_schema=tool.input_schema or {"type": "object"},
        action=action,
        risk=risk,
        safe_to_retry=read_only or idempotent,
    )


class ToolRegistry:
    def __init__(self):
        self._specs: dict[str, ToolSpec] = {}

    def add(self, spec: ToolSpec) -> None:
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError:
            raise ToolNotFound(f"Unknown tool '{name}'") from None

    def all(self) -> list[ToolSpec]:
        return sorted(self._specs.values(), key=lambda s: s.name)

    def __len__(self) -> int:
        return len(self._specs)

    @staticmethod
    def render_catalog(specs: list[ToolSpec]) -> str:
        """Text for the planner prompt, generated from what is ACTUALLY available."""
        by_category: dict[ToolCategory, list[ToolSpec]] = defaultdict(list)
        for s in specs:
            by_category[s.category].append(s)

        lines = []
        for category in ToolCategory:
            if category == ToolCategory.REASONING:
                continue
            tools = by_category.get(category)
            if not tools:
                continue
            lines.append(f"- {category.value}:")
            for s in sorted(tools, key=lambda x: x.name):
                first_line = s.description.splitlines()[0][:200] if s.description else ""
                lines.append(f"    * {s.name} [{s.action.value}, {s.risk.value} risk]: {first_line}")
        lines.append("- reasoning: pure LLM work - analyse, summarise, compare, draft text. No external system is touched.")
        return "\n".join(lines)
