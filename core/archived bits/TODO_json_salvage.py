import json
import re
from pydantic import ValidationError

def salvage_truncated_json(content: str) -> str:
    """
    Attempts to recover a truncated JSON string by closing open structures.
    Works inward from the truncation point.
    """
    # Trim any trailing partial token garbage (mid-word, mid-escape)
    content = content.rstrip()
    
    # Remove a trailing incomplete string value if present
    # e.g. "...of S=999, E=1, I=0, R=   <-- cut here
    content = re.sub(r',?\s*"[^"]*$', '', content)
    
    # Count unclosed structures
    depth_brace = 0
    depth_bracket = 0
    in_string = False
    escape_next = False
    
    for char in content:
        if escape_next:
            escape_next = False
            continue
        if char == '\\' and in_string:
            escape_next = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == '{':
            depth_brace += 1
        elif char == '}':
            depth_brace -= 1
        elif char == '[':
            depth_bracket += 1
        elif char == ']':
            depth_bracket -= 1
    
    # Close open structures in reverse order
    closing = ''
    # If we're mid-object, close the current requirement object first
    if depth_brace > depth_bracket:
        closing += '}'
        depth_brace -= 1
    # Close the requirements array
    if depth_bracket > 0:
        closing += ']' * depth_bracket
    # Close the root object
    if depth_brace > 0:
        closing += '}' * depth_brace
    
    return content + closing

############# MAKE A WAY TO GET THIS INSIDE THE CHAT CALL
def chat_json(model, messages, list_schema, temperature=0):
    """Queries model, structured (JSON) output. With truncation recovery."""
    response = ollama.chat(
        model=model,
        messages=messages,
        format=list_schema.model_json_schema(),
        options={
            'temperature': temperature,
            'num_predict': token_limit,
            'repeat_penalty': 1.15,
            'stop': ['}\n}', '}}\n']
        }
    )

    content = response.message.content

    # --- Attempt 1: clean parse
    try:
        return list_schema.model_validate_json(content)
    except ValidationError as e:
        if 'EOF' not in str(e) and 'json_invalid' not in str(e):
            raise  # not a truncation error, surface it

    # --- Attempt 2: salvage truncated JSON
    print("[WARN] JSON truncated, attempting salvage...")
    salvaged = salvage_truncated_json(content)
    
    try:
        result = list_schema.model_validate_json(salvaged)
        print(f"[WARN] Salvaged {len(result.requirements)} requirements from truncated output")
        return result
    except ValidationError:
        pass

    # --- Attempt 3: extract whatever complete objects we can
    print("[WARN] Salvage failed, extracting partial requirements...")
    partial = extract_complete_requirements(content, list_schema)
    if partial:
        print(f"[WARN] Recovered {len(partial.requirements)} complete requirements")
        return partial

    raise ValueError(
        f"Could not recover valid JSON from model output. "
        f"Content length: {len(content)} chars. "
        f"Consider raising token_limit (currently {token_limit})."
    )


def extract_complete_requirements(content: str, list_schema):
    """
    Last-resort: regex-extract fully closed requirement objects
    from a broken JSON blob.
    """
    # Find all {...} blobs that look like complete requirement objects
    pattern = re.compile(r'\{[^{}]*"id"\s*:\s*"[^"]*"[^{}]*\}', re.DOTALL)
    matches = pattern.findall(content)
    
    valid_reqs = []
    for match in matches:
        try:
            obj = json.loads(match)
            valid_reqs.append(obj)
        except json.JSONDecodeError:
            continue
    
    if not valid_reqs:
        return None
    
    try:
        return list_schema.model_validate({'requirements': valid_reqs})
    except ValidationError:
        return None