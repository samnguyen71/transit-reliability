import argparse
from .load import process_zip

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "zips",
        nargs="+",
        help="ZIP files to process",
    )

    args = parser.parse_args()

    for zip_path in args.zips:
        process_zip(zip_path)

if __name__ == "__main__":
    main()