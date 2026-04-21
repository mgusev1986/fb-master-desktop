"""Разбор списка ID людей из полей формы (рассылка, прогрев, сценарии)."""


def parse_person_ids_field(raw: str) -> list[int]:
    """
    Поддерживаются:
    - через запятую / точку с запятой: 1, 2, 3
    - столбиком (перенос строки): одно число на строку
    - смешанный ввод
    """
    if raw is None:
        return []
    text = str(raw).replace("\r\n", "\n").replace("\r", "\n")
    out: list[int] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        for part in line.replace(";", ",").split(","):
            p = part.strip()
            if p.isdigit():
                out.append(int(p))
    return list(dict.fromkeys(out))
