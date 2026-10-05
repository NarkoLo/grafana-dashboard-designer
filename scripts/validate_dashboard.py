#!/usr/bin/env python3
"""Проверка Grafana dashboard JSON на структурные и UX-проблемы."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class Issue:
    """Одна найденная проблема с уровнем серьёзности."""

    severity: str
    location: str
    message: str


SEVERITY_ORDER = {"ERROR": 0, "WARN": 1, "INFO": 2}
EXPECTED_VARIABLE_ORDER = (
    "environment",
    "env",
    "cluster",
    "namespace",
    "service",
    "instance",
    "dependency",
)
HIGH_CARDINALITY_LABELS = {
    "pod",
    "instance",
    "path",
    "uri",
    "url",
    "request_id",
    "trace_id",
    "user_id",
}


def load_dashboard(path: Path) -> dict[str, Any]:
    """Загрузить JSON и извлечь dashboard из API-обёртки при её наличии."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"файл не найден: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"некорректный JSON: строка {exc.lineno}, столбец {exc.colno}: {exc.msg}"
        ) from exc

    if not isinstance(raw, dict):
        raise ValueError("корневое значение JSON должно быть объектом")

    dashboard = raw.get("dashboard", raw)
    if not isinstance(dashboard, dict):
        raise ValueError("поле dashboard должно быть объектом")
    return dashboard


def iter_panels(panels: Iterable[Any], prefix: str = "panels") -> Iterable[tuple[dict[str, Any], str]]:
    """Обойти обычные панели и вложенные панели свёрнутых rows."""

    for index, panel in enumerate(panels):
        location = f"{prefix}[{index}]"
        if not isinstance(panel, dict):
            continue
        yield panel, location
        nested = panel.get("panels")
        if isinstance(nested, list):
            yield from iter_panels(nested, f"{location}.panels")


def expression_from_target(target: dict[str, Any]) -> str:
    """Получить текст запроса для распространённых datasource."""

    for key in ("expr", "query", "rawSql", "rawQuery"):
        value = target.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def variable_rank(name: str) -> int:
    """Определить рекомендуемый порядок переменной."""

    lowered = name.lower()
    for index, expected in enumerate(EXPECTED_VARIABLE_ORDER):
        if lowered == expected or lowered.endswith(f"_{expected}"):
            return index
    return len(EXPECTED_VARIABLE_ORDER)


def check_query(query: str, location: str) -> list[Issue]:
    """Найти частые ошибки PromQL/LogQL без попытки заменить парсер."""

    issues: list[Issue] = []
    compact = " ".join(query.split())

    # Counter без rate/increase обычно даёт вводящий в заблуждение график.
    if re.search(r"(?:_total|_count)\s*(?:\{|\[|$)", compact) and not re.search(
        r"\b(?:rate|irate|increase)\s*\(", compact
    ):
        issues.append(
            Issue("WARN", location, "похожий на counter запрос используется без rate/increase")
        )

    # Жёстко заданные области затрудняют повторное использование дашборда.
    for label in ("environment", "cluster", "namespace", "service", "pod", "instance"):
        pattern = rf'\b{label}\s*(?:=|=~)\s*"(?!\$)'
        if re.search(pattern, compact):
            issues.append(
                Issue("WARN", location, f"лейбл {label} задан константой вместо переменной")
            )

    by_match = re.search(r"\bby\s*\(([^)]*)\)", compact)
    if by_match and "topk(" not in compact.lower():
        labels = {item.strip() for item in by_match.group(1).split(",")}
        risky = sorted(labels & HIGH_CARDINALITY_LABELS)
        if risky:
            issues.append(
                Issue(
                    "WARN",
                    location,
                    "агрегация по потенциально высококардинальным лейблам без topk: "
                    + ", ".join(risky),
                )
            )

    if "rate(" in compact and "$__rate_interval" not in compact:
        windows = re.findall(r"\brate\s*\([^\[]*\[([^]]+)\]", compact)
        if windows:
            issues.append(
                Issue("INFO", location, "проверить фиксированное окно rate; обычно лучше $__rate_interval")
            )
    return issues


def check_overlap(panel_entries: list[tuple[dict[str, Any], str]]) -> list[Issue]:
    """Проверить пересечения панелей верхнего уровня."""

    issues: list[Issue] = []
    rectangles: list[tuple[int, int, int, int, str]] = []
    for panel, location in panel_entries:
        grid = panel.get("gridPos")
        if not isinstance(grid, dict):
            continue
        try:
            x, y, width, height = (int(grid[key]) for key in ("x", "y", "w", "h"))
        except (KeyError, TypeError, ValueError):
            continue
        rectangles.append((x, y, x + width, y + height, location))

    for index, first in enumerate(rectangles):
        ax1, ay1, ax2, ay2, aloc = first
        for bx1, by1, bx2, by2, bloc in rectangles[index + 1 :]:
            if ax1 < bx2 and bx1 < ax2 and ay1 < by2 and by1 < ay2:
                issues.append(Issue("ERROR", aloc, f"панель пересекается с {bloc}"))
    return issues


def validate_dashboard(dashboard: dict[str, Any]) -> list[Issue]:
    """Проверить структуру и основные UX-инварианты дашборда."""

    issues: list[Issue] = []
    title = dashboard.get("title")
    if not isinstance(title, str) or not title.strip():
        issues.append(Issue("ERROR", "dashboard.title", "не задан заголовок дашборда"))

    panels = dashboard.get("panels")
    if not isinstance(panels, list) or not panels:
        issues.append(Issue("ERROR", "dashboard.panels", "дашборд не содержит панелей"))
        return issues

    refresh = dashboard.get("refresh")
    if isinstance(refresh, str):
        match = re.fullmatch(r"(\d+)(ms|s|m|h)", refresh)
        if match:
            value, unit = int(match.group(1)), match.group(2)
            seconds = value * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
            if seconds < 10:
                issues.append(Issue("WARN", "dashboard.refresh", "refresh меньше 10 секунд"))

    templating = dashboard.get("templating", {})
    variables = templating.get("list", []) if isinstance(templating, dict) else []
    if not isinstance(variables, list):
        issues.append(Issue("ERROR", "dashboard.templating.list", "список переменных некорректен"))
        variables = []

    variable_names: list[str] = []
    for index, variable in enumerate(variables):
        location = f"dashboard.templating.list[{index}]"
        if not isinstance(variable, dict):
            issues.append(Issue("ERROR", location, "переменная должна быть объектом"))
            continue
        name = variable.get("name")
        if not isinstance(name, str) or not name.strip():
            issues.append(Issue("ERROR", location, "у переменной отсутствует name"))
            continue
        variable_names.append(name)
        if variable.get("includeAll") and variable.get("multi"):
            issues.append(
                Issue("INFO", location, "проверить стоимость сочетания multi и All")
            )

    ranks = [variable_rank(name) for name in variable_names]
    known_ranks = [rank for rank in ranks if rank < len(EXPECTED_VARIABLE_ORDER)]
    if known_ranks != sorted(known_ranks):
        issues.append(
            Issue("WARN", "dashboard.templating.list", "переменные нарушают рекомендуемый порядок")
        )

    all_entries = list(iter_panels(panels))
    top_entries = [entry for entry in all_entries if ".panels" not in entry[1]]
    issues.extend(check_overlap(top_entries))

    ids: dict[int, str] = {}
    first_screen_signal = False
    for panel, location in all_entries:
        panel_id = panel.get("id")
        if not isinstance(panel_id, int):
            issues.append(Issue("ERROR", f"{location}.id", "panel id должен быть целым числом"))
        elif panel_id in ids:
            issues.append(
                Issue("ERROR", f"{location}.id", f"дублирует panel id из {ids[panel_id]}")
            )
        else:
            ids[panel_id] = location

        panel_type = panel.get("type")
        if not isinstance(panel_type, str) or not panel_type:
            issues.append(Issue("ERROR", f"{location}.type", "не указан тип панели"))

        panel_title = panel.get("title")
        if not isinstance(panel_title, str) or not panel_title.strip():
            issues.append(Issue("ERROR", f"{location}.title", "не задан заголовок панели"))
        elif re.fullmatch(r"(?i)(panel|graph|http|metrics?)(?:\s*\d+)?", panel_title.strip()):
            issues.append(Issue("WARN", f"{location}.title", "заголовок не объясняет смысл панели"))

        if panel_type not in {"row", "text"} and not str(panel.get("description", "")).strip():
            issues.append(Issue("WARN", f"{location}.description", "нет подсказки по интерпретации"))

        grid = panel.get("gridPos")
        if not isinstance(grid, dict):
            issues.append(Issue("ERROR", f"{location}.gridPos", "не задано положение панели"))
        else:
            try:
                x, width, y = int(grid["x"]), int(grid["w"]), int(grid["y"])
                height = int(grid["h"])
                if x < 0 or width <= 0 or x + width > 24 or y < 0 or height <= 0:
                    issues.append(Issue("ERROR", f"{location}.gridPos", "панель выходит за сетку 24 колонки"))
                if y < 8 and panel_type in {"stat", "gauge", "timeseries"}:
                    first_screen_signal = True
            except (KeyError, TypeError, ValueError):
                issues.append(Issue("ERROR", f"{location}.gridPos", "координаты должны быть целыми числами"))

        field_config = panel.get("fieldConfig", {})
        defaults = field_config.get("defaults", {}) if isinstance(field_config, dict) else {}
        if panel_type in {"stat", "gauge", "timeseries", "bargauge"}:
            unit = defaults.get("unit") if isinstance(defaults, dict) else None
            if not isinstance(unit, str) or not unit:
                issues.append(Issue("WARN", f"{location}.fieldConfig", "не задана единица измерения"))

        targets = panel.get("targets", [])
        if panel_type not in {"row", "text", "dashlist", "alertlist"}:
            if not isinstance(targets, list) or not targets:
                issues.append(Issue("ERROR", f"{location}.targets", "панель не содержит запросов"))
            else:
                for target_index, target in enumerate(targets):
                    target_location = f"{location}.targets[{target_index}]"
                    if not isinstance(target, dict):
                        issues.append(Issue("ERROR", target_location, "target должен быть объектом"))
                        continue
                    if target.get("hide"):
                        continue
                    query = expression_from_target(target)
                    if not query:
                        issues.append(Issue("ERROR", target_location, "нет текста запроса"))
                    else:
                        issues.extend(check_query(query, target_location))

    if not first_screen_signal:
        issues.append(
            Issue("WARN", "dashboard.panels", "на первом экране нет обзорного сигнала здоровья")
        )
    return issues


def render(issues: list[Issue], json_output: bool) -> None:
    """Вывести результат в человекочитаемом или JSON-формате."""

    ordered = sorted(issues, key=lambda item: (SEVERITY_ORDER[item.severity], item.location))
    if json_output:
        print(json.dumps([item.__dict__ for item in ordered], ensure_ascii=False, indent=2))
        return
    if not ordered:
        print("[OK] Ошибок и предупреждений не найдено")
        return
    for item in ordered:
        print(f"[{item.severity}] {item.location}: {item.message}")
    counts = {severity: sum(item.severity == severity for item in ordered) for severity in SEVERITY_ORDER}
    print(
        "Итог: "
        + ", ".join(f"{name.lower()} — {count}" for name, count in counts.items())
    )


def main() -> int:
    """Запустить CLI-валидатор."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dashboard", type=Path, help="путь к dashboard JSON")
    parser.add_argument("--strict", action="store_true", help="считать предупреждения ошибками")
    parser.add_argument("--json", action="store_true", help="вывести найденные проблемы как JSON")
    args = parser.parse_args()

    try:
        dashboard = load_dashboard(args.dashboard)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    issues = validate_dashboard(dashboard)
    render(issues, args.json)
    has_error = any(item.severity == "ERROR" for item in issues)
    has_warning = any(item.severity == "WARN" for item in issues)
    return 1 if has_error or (args.strict and has_warning) else 0


if __name__ == "__main__":
    raise SystemExit(main())
