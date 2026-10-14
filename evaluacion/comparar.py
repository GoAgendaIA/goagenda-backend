"""
Compara dos reportes de evaluacion (antes / despues de un cambio).

    python -m evaluacion.comparar evaluacion/reportes/<base>.json evaluacion/reportes/<nuevo>.json
"""

import json
import sys

from evaluacion.ejecutor import comparar


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    with open(argv[1], encoding="utf-8") as f:
        base = json.load(f)
    with open(argv[2], encoding="utf-8") as f:
        nuevo = json.load(f)
    c = comparar(base, nuevo)
    print(json.dumps(c, ensure_ascii=False, indent=2))
    print(f"\nconfiguracion antes:   {base['configuracion']} ({base['modelo']})")
    print(f"configuracion despues: {nuevo['configuracion']} ({nuevo['modelo']})")
    return 1 if c["regresiones"] else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
