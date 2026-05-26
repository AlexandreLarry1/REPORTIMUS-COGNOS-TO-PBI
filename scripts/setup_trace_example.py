"""Copy edr-demo inputs to edr-demo-trace so tracing runs never overwrite the original."""
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).parent.parent
SRC  = ROOT / "examples" / "edr-demo"
DST  = ROOT / "examples" / "edr-demo-trace"

FILES_TO_COPY = [
    "intermediate/extraction.json",
    "intermediate/visual_extraction.json",
]


def main() -> None:
    if not SRC.exists():
        print(f"Source introuvable : {SRC}")
        sys.exit(1)

    # input/ directory
    src_input = SRC / "input"
    dst_input = DST / "input"
    dst_input.mkdir(parents=True, exist_ok=True)
    for f in src_input.iterdir():
        shutil.copy2(f, dst_input / f.name)
    print(f"  input/ copié ({len(list(src_input.iterdir()))} fichiers)")

    # specific intermediate files
    for rel in FILES_TO_COPY:
        src_f = SRC / rel
        dst_f = DST / rel
        if src_f.exists():
            dst_f.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_f, dst_f)
            print(f"  {rel} copié")
        else:
            print(f"  SKIP {rel} (absent dans source)")

    print(f"\nedr-demo-trace prêt dans {DST}")
    print("Mets EXAMPLE_NAME=edr-demo-trace dans .env avant de lancer la pipeline.")


if __name__ == "__main__":
    main()
