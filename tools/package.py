"""Build an AstrBot upload ZIP from an explicit list of runtime files."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]


def main():
    version = next(
        line.split(":", 1)[1].strip()
        for line in (ROOT / "metadata.yaml").read_text(encoding="utf-8").splitlines()
        if line.startswith("version:")
    )
    files = [
        ROOT / name
        for name in (
            "__init__.py",
            "main.py",
            "metadata.yaml",
            "_conf_schema.json",
            "requirements.txt",
            "README.md",
            "LICENSE",
        )
    ]
    files += sorted((ROOT / "services").glob("*.py"))
    destination = ROOT / "dist" / f"astrbot_plugin_tixing-{version}.zip"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, "astrbot_plugin_tixing/" + path.relative_to(ROOT).as_posix())
    with ZipFile(destination) as archive:
        assert archive.testzip() is None
        assert "astrbot_plugin_tixing/main.py" in archive.namelist()
        assert "astrbot_plugin_tixing/metadata.yaml" in archive.namelist()
    print(destination)


if __name__ == "__main__":
    main()
