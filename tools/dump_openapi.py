"""Write the committed OpenAPI document; CI fails when the copy drifts."""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "health_info.settings.test")
    sys.path.insert(0, str(ROOT))
    import django

    django.setup()

    from records.api import api

    path = ROOT / "docs" / "api" / "openapi.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    # The repository pins eol=lf; Windows text mode would otherwise write CRLF and dirty the
    # committed copy every time the tool runs.
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(api.get_openapi_schema(), indent=2, sort_keys=True) + "\n")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
