"""Shared primitives для multi-workspace shell.

Сюда переезжают сетенезависимые слои (modules registry, workspace session,
compliance primitives, rate-limit state, AI primitives). Существующий код
`backend/services/*` остаётся на месте — core только экспортирует
абстракции, которые модули (facebook, reddit, …) реализуют.
"""
