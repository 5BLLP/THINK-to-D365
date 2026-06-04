from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

try:
    from .converter import describe_table_mapping, get_supported_tables, load_json, transform_records
    from .d365 import D365Client, load_config
except ImportError:
    from converter import describe_table_mapping, get_supported_tables, load_json, transform_records
    from d365 import D365Client, load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crm-json-transform",
        description="Generate per-table CRM mappings and transform JSON records with fixed table definitions.",
    )
    parser.add_argument("--table", choices=get_supported_tables(), help="Source table to describe or transform.")
    parser.add_argument("--input", help="Path to the source JSON file when transforming records.")
    parser.add_argument("--output", help="Optional output file path. Prints to stdout when omitted.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print the output JSON.")
    parser.add_argument("--list-tables", action="store_true", help="List the supported source tables.")
    parser.add_argument("--push-d365", action="store_true", help="Push sanitized records to Dynamics 365 after writing output.")
    parser.add_argument("--debug-http", action="store_true", help="Print D365 request and response details on HTTP errors.")
    parser.add_argument(
        "--describe",
        action="store_true",
        help="Emit the per-table mapping definition without transforming any records.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.list_tables:
        text = json.dumps({"tables": get_supported_tables()}, indent=2 if args.pretty else None) + "\n"
        write_output(text, args.output)
        return 0

    if not args.table:
        parser.error("--table is required unless --list-tables is used.")

    if args.describe:
        if args.push_d365 or args.debug_http:
            parser.error("--push-d365 and --debug-http cannot be used with --describe.")
        payload = describe_table_mapping(args.table)
        errors: list[str] = []
        push_logs: list[str] = []
        output_path = args.output
    else:
        if not args.input:
            parser.error("--input is required unless --describe is used.")
        payload, errors = transform_records(args.table, load_json(args.input))
        output_path = args.output or default_output_path(args.table)
        push_logs = []
        if args.push_d365:
            client = D365Client(load_config(), debug_http=args.debug_http)
            push_logs = client.upsert_table_records(args.table, payload)
        elif args.debug_http:
            parser.error("--debug-http requires --push-d365.")

    text = json.dumps(payload, indent=2 if args.pretty else None) + "\n"
    write_output(text, output_path)
    for error in errors:
        print(f"[crm-json-transform] {error}", file=sys.stderr)
    for log_line in push_logs:
        print(log_line, file=sys.stderr)
    return 0


def write_output(text: str, output_path: str | None) -> None:
    if output_path:
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def default_output_path(table_name: str) -> str:
    return str(Path("output") / f"{table_name}_mapped.json")


if __name__ == "__main__":
    raise SystemExit(main())
