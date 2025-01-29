import os
import json
import re
from pathlib import Path

def remove_comments_from_json_string(json_str: str) -> str:
    """Remove comments from a JSON string and return valid JSON."""
    # First remove inline comments that are not inside strings
    lines = []
    in_string = False
    escape_next = False

    for line in json_str.splitlines():
        new_line = []
        i = 0
        while i < len(line):
            char = line[i]

            if escape_next:
                new_line.append(char)
                escape_next = False
            elif char == '\\':
                new_line.append(char)
                escape_next = True
            elif char == '"' and not escape_next:
                in_string = not in_string
                new_line.append(char)
            elif char == '/' and i + 1 < len(line) and line[i + 1] == '/' and not in_string:
                break  # Skip rest of line for inline comments
            else:
                new_line.append(char)
            i += 1

        lines.append(''.join(new_line))

    json_str = '\n'.join(lines)

    # Then remove multi-line comments (/* ... */) that are not inside strings
    cleaned = []
    in_comment = False
    in_string = False
    escape_next = False
    i = 0

    while i < len(json_str):
        char = json_str[i]

        if escape_next:
            if not in_comment:
                cleaned.append(char)
            escape_next = False
        elif char == '\\':
            if not in_comment:
                cleaned.append(char)
            escape_next = True
        elif char == '"' and not escape_next:
            if not in_comment:
                in_string = not in_string
                cleaned.append(char)
        elif char == '/' and i + 1 < len(json_str) and json_str[i + 1] == '*' and not in_string:
            in_comment = True
            i += 1  # Skip next char
        elif char == '*' and i + 1 < len(json_str) and json_str[i + 1] == '/' and in_comment:
            in_comment = False
            i += 1  # Skip next char
        elif not in_comment:
            cleaned.append(char)
        i += 1

    json_str = ''.join(cleaned)

    # Remove trailing commas before closing brackets/braces
    json_str = re.sub(r',(\s*[}\]])', r'\1', json_str)

    # Remove any empty lines
    json_str = '\n'.join(line for line in json_str.splitlines() if line.strip())

    return json_str

def clean_json_content(content: str) -> dict:
    """Clean and parse JSON content, handling various edge cases."""
    # Remove BOM if present
    if content.startswith('\ufeff'):
        content = content[1:]

    # Remove null bytes and control characters
    content = content.replace('\x00', '')
    content = ''.join(char for char in content if ord(char) >= 32 or char in '\n\r\t')

    # Remove comments and clean up the JSON
    content = remove_comments_from_json_string(content)

    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        # If still failing, show the problematic line
        lines = content.split('\n')
        error_line = lines[e.lineno - 1] if e.lineno <= len(lines) else "Line not found"
        print(f"Error at line {e.lineno}:")
        print(f"Content: {error_line}")
        print(f"Position: {' ' * e.colno}^")
        raise

def process_json_file(file_path: Path) -> None:
    """Process a single JSON file to remove comments."""
    try:
        # Read the original file
        with open(file_path, 'r', encoding='utf-8-sig') as f:
            content = f.read()

        try:
            # Clean and parse the JSON
            json_data = clean_json_content(content)

            # Write back the cleaned, formatted JSON
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(json_data, f, indent=2, ensure_ascii=False)
            print(f"✓ Successfully processed: {file_path}")

        except json.JSONDecodeError as e:
            print(f"\n✗ Error processing {file_path}")

    except Exception as e:
        print(f"✗ Error reading/writing {file_path}: {str(e)}")

def main():
    # Get the project root directory
    root_dir = Path(__file__).parent.parent

    # Directories to process
    directories = [
        root_dir / 'themes',
        root_dir / 'plugins'
    ]

    total_files = 0
    processed_files = 0
    successful_files = 0

    print("Starting JSON files cleanup...")

    for directory in directories:
        if not directory.exists():
            print(f"Directory not found: {directory}")
            continue

        # Walk through all JSON files in the directory
        for json_file in directory.rglob('*.json'):
            total_files += 1
            try:
                process_json_file(json_file)
                successful_files += 1
            except:
                pass  # Error already printed
            processed_files += 1

    print(f"\nProcessing complete!")
    print(f"Successfully processed: {successful_files}/{total_files} files")

if __name__ == "__main__":
    main()
