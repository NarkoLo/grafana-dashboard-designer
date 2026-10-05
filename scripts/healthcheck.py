#!/usr/bin/env python3
"""Комплексная проверка готовности skill к применению."""

from __future__ import annotations

import importlib.util
import json
import py_compile
import re
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
REQUIRED = (
    "SKILL.md",
    "agents/openai.yaml",
    "references/ux-rules.md",
    "references/query-rules.md",
    "references/grafana-mcp.md",
    "assets/service-overview.dashboard.json",
    "scripts/validate_dashboard.py",
    "scripts/healthcheck.py",
)


def report(ok: bool, name: str, detail: str) -> bool:
    """Вывести результат отдельной проверки."""

    print(f"[{'OK' if ok else 'ERROR'}] {name}: {detail}")
    return ok


def load_validator():
    """Загрузить валидатор непосредственно из skill."""

    path = ROOT / "scripts" / "validate_dashboard.py"
    spec = importlib.util.spec_from_file_location("grafana_dashboard_validator", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("не удалось создать спецификацию валидатора")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    """Выполнить все проверки и вернуть ненулевой код при ошибке."""

    results: list[bool] = []

    missing = [path for path in REQUIRED if not (ROOT / path).is_file()]
    results.append(report(not missing, "структура", "все обязательные файлы найдены" if not missing else ", ".join(missing)))

    markdown = [ROOT / "SKILL.md", *sorted((ROOT / "references").glob("*.md"))]
    todo_files = [path.name for path in markdown if re.search(r"\bTODO\b|\[TODO", path.read_text(encoding="utf-8"))]
    results.append(report(not todo_files, "заглушки", "TODO отсутствуют" if not todo_files else ", ".join(todo_files)))

    cyrillic_ok = all(re.search(r"[А-Яа-яЁё]", path.read_text(encoding="utf-8")) for path in markdown)
    results.append(report(cyrillic_ok, "русский язык", f"проверено файлов: {len(markdown)}"))

    scripts = sorted((ROOT / "scripts").glob("*.py"))
    syntax_errors: list[str] = []
    # Компилировать во временную директорию, не загрязняя skill файлами __pycache__.
    with tempfile.TemporaryDirectory(prefix="grafana-skill-check-") as temp_dir:
        for script in scripts:
            try:
                target = Path(temp_dir) / f"{script.stem}.pyc"
                py_compile.compile(str(script), cfile=str(target), doraise=True)
            except py_compile.PyCompileError as exc:
                syntax_errors.append(f"{script.name}: {exc.msg}")
    results.append(report(not syntax_errors, "python", "синтаксис корректен" if not syntax_errors else "; ".join(syntax_errors)))

    try:
        validator = load_validator()
        dashboard = json.loads((ROOT / "assets" / "service-overview.dashboard.json").read_text(encoding="utf-8"))
        issues = validator.validate_dashboard(dashboard)
        strict_issues = [issue for issue in issues if issue.severity in {"ERROR", "WARN"}]
        results.append(report(not strict_issues, "эталон", "dashboard JSON проходит строгую проверку" if not strict_issues else "; ".join(issue.message for issue in strict_issues)))

        broken = dict(dashboard)
        broken["panels"] = [dict(dashboard["panels"][0]), dict(dashboard["panels"][0])]
        broken_issues = validator.validate_dashboard(broken)
        catches_duplicate = any("дублирует panel id" in issue.message for issue in broken_issues)
        results.append(report(catches_duplicate, "негативный тест", "дублирующийся panel id обнаруживается"))
    except Exception as exc:  # Диагностика должна продолжиться после ошибки одного теста.
        results.append(report(False, "валидатор", str(exc)))

    quick_validate = Path("/root/.codex/skills/oai/skill-creator/scripts/quick_validate.py")
    if quick_validate.is_file():
        process = subprocess.run(
            [sys.executable, str(quick_validate), str(ROOT)],
            capture_output=True,
            text=True,
            check=False,
        )
        detail = (process.stdout + process.stderr).strip().replace("\n", "; ")
        results.append(report(process.returncode == 0, "формат skill", detail or "quick_validate выполнен"))
    else:
        print("[WARN] формат skill: quick_validate недоступен, проверка пропущена")

    failed = sum(not result for result in results)
    print(f"Итог: ошибок — {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
