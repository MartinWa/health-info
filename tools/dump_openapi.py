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
    path.write_text(
        json.dumps(api.get_openapi_schema(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
