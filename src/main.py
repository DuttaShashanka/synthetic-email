import argparse
import json
from dataclasses import asdict
from pathlib import Path
from .pipeline import synthesize


def main():
    parser = argparse.ArgumentParser(description="Privacy-first synthetic email generator")
    parser.add_argument("input_file", type=Path)
    parser.add_argument("--industry", default="renewable energy")
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--entity-graph", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--save-sanitized", type=Path, default=None)
    args = parser.parse_args()

    source = args.input_file.read_text(encoding="utf-8")
    candidate, report, sanitized, attempts = synthesize(
        source, args.industry, args.model, args.max_attempts, args.entity_graph
    )
    payload = {"attempts": attempts, **asdict(report)}

    if args.output:
        args.output.write_text(candidate, encoding="utf-8")
    else:
        print(candidate)
    if args.save_sanitized:
        args.save_sanitized.write_text(sanitized, encoding="utf-8")
    if args.report:
        args.report.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print("\nValidation report:")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
