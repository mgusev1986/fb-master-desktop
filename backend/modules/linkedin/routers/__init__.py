"""LinkedIn Master — отдельные routers с реальной логикой.

В Beta-релизе единственный «не-placeholder» router — это
`compliance.py`, который рендерит capability matrix + safety-лимиты.
Подключается ДО основного `router.py` в app_factory, чтобы перекрывать
placeholder.
"""
