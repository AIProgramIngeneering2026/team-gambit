from .strategy_tool import StrategyTool, ToolError


class Registry:
    """Хранит доступные версии и текущую версию каждой стратегии."""

    def __init__(self) -> None:
        self._tools: dict[tuple[str, str], StrategyTool] = {}
        self._current: dict[str, str] = {}

    def register(self, tool: StrategyTool, *, current: bool = False) -> None:
        """Первая версия становится текущей. Сменить её можно при регистрации."""
        key = (tool.name, tool.version)
        if key in self._tools:
            raise ValueError(
                f"Стратегия {tool.name} версии {tool.version} уже зарегистрирована"
            )
        self._tools[key] = tool
        if current or tool.name not in self._current:
            self._current[tool.name] = tool.version

    def get(self, name: str, version: str | None = None) -> StrategyTool:
        """Без версии выбирает Tool для нового задания. С версией ищет точную пару."""
        current_version = self._current.get(name)
        if current_version is None:
            raise ToolError(
                "unknown_operation",
                f"Стратегия {name} не зарегистрирована",
                "strategy",
                "unknown_operation",
                allowed=tuple(sorted(self._current)),
            )
        if version is None:
            version = current_version
        tool = self._tools.get((name, version))
        if tool is None:
            raise ToolError(
                "strategy_unavailable",
                f"Сохранённая версия {version} стратегии {name} недоступна",
                "strategy.version",
                "strategy_unavailable",
                name=name,
                version=version,
            )
        return tool

    def catalog(self) -> tuple[StrategyTool, ...]:
        """Текущие Tool с собственными метаданными и схемой параметров."""
        return tuple(
            self._tools[(name, self._current[name])] for name in sorted(self._current)
        )
