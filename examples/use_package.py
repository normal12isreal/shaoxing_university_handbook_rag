"""Run from any working directory after installing the package in your Python environment."""
import argparse
import json

from student_handbook_rag import HandbookRAG, ModelAPIError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-dir', required=True, help='Existing data/config/index project directory')
    parser.add_argument('--question', required=True)
    parser.add_argument('--no-web', action='store_true')
    args = parser.parse_args()
    rag = HandbookRAG(args.project_dir)
    try:
        result = rag.ask(args.question, allow_web=not args.no_web)
    except ModelAPIError as error:
        raise SystemExit(f'Model request failed: {error}') from None
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
