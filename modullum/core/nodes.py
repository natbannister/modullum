import ollama
import re
from pydantic import ValidationError
from modullum import config


class Node:
    """
    Manages a conversation history for a node.

    Stores a system prompt separately from the message history and combines
    them when constructing the final messages payload for API calls.
    """

    def __init__(self, system_prompt: str):
        self.system = system_prompt
        self.history: list[dict] = []

    def add_user(self, content: str):
        self.history.append({"role": "user", "content": content})

    def add_assistant(self, content: str):
        self.history.append({"role": "assistant", "content": content})

    def messages(self) -> list[dict]:
        """Constructs the full messages payload, prepending the system prompt."""
        return [{"role": "system", "content": self.system}] + self.history

    def last_response(self) -> str | None:
        return self.history[-1]["content"] if self.history else None

def strip_code_fences(text: str) -> str:
    match = re.search(r'```(?:python)?\n(.*?)```', text, re.DOTALL)
    return match.group(1) if match else text

def flatten_schema(schema: dict) -> dict:
    """
    Flattens a JSON schema by resolving all $ref references inline.
    Handles nested objects, arrays, and mixed types.
    Compatible with Ollama/llama.cpp structured output.
    """
    defs = schema.get("$defs", {})

    def resolve(node: dict) -> dict:
        if not isinstance(node, dict):
            return node

        # Resolve $ref
        if "$ref" in node:
            ref_path = node["$ref"]  # e.g. "#/$defs/Requirement"
            ref_key = ref_path.split("/")[-1]
            if ref_key in defs:
                return resolve(defs[ref_key])
            return node  # unresolvable ref, leave as-is

        result = {}
        for key, value in node.items():
            if key == "$defs":
                continue  # strip the definitions block from output
            elif key == "properties" and isinstance(value, dict):
                result[key] = {k: resolve(v) for k, v in value.items()}
            elif key == "items":
                result[key] = resolve(value)
            elif key == "anyOf" or key == "oneOf" or key == "allOf":
                result[key] = [resolve(v) for v in value]
            elif isinstance(value, dict):
                result[key] = resolve(value)
            elif isinstance(value, list):
                result[key] = [resolve(v) if isinstance(v, dict) else v for v in value]
            else:
                result[key] = value

        return result

    return resolve(schema)

def schema_to_prompt_hint(schema: dict) -> str:
    """
    Returns a text description of a flattened JSON schema.
    Required to aid structured output enforcement in 'looser' models.
    """
    if hasattr(schema, "model_json_schema"):
        schema = schema.model_json_schema()

    flat = flatten_schema(schema)
    props = flat.get("properties", {})

    lines = ["Respond with a JSON object with these exact fields:"]
    for name, field in props.items():
        field_type = field.get("type", "")
        desc = field.get("description", "")
        if field_type == "array":
            item_props = field.get("items", {}).get("properties", {})
            lines.append(f"- {name}: array of objects with fields:")
            for fname, fval in item_props.items():
                fdesc = fval.get("description", "")
                lines.append(f"    - {fname}: {fval.get('type', 'string')}{f' — {fdesc}' if fdesc else ''}")
        else:
            lines.append(f"- {name}: {field_type}{f' — {desc}' if desc else ''}")
    lines.append("Output must contain exactly these fields and no others.")

    return "\n".join(lines)

def salvage_truncated_json(content: str) -> str:
    """Closes unclosed JSON structures, discarding the last incomplete entry."""
    content = re.sub(r',\s*\{[^}]*$', '', content).rstrip()

    depth_brace = depth_bracket = 0
    in_string = escape_next = False

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
        if char == '{':   depth_brace += 1
        elif char == '}': depth_brace -= 1
        elif char == '[':  depth_bracket += 1
        elif char == ']':  depth_bracket -= 1

    return content + (']' * depth_bracket) + ('}' * depth_brace)

def supports_thinking(model: str) -> bool:
    return any(model.startswith(m) or m in model for m in config.THINKING_MODELS)

def _stream_response(response, thinking_enabled: bool) -> str:
    """Handles streaming output, with optional thinking block display."""
    content = ""
    in_thinking = False

    for chunk in response:
        if thinking_enabled:
            if chunk.message.thinking:
                if not in_thinking:
                    print("Thinking:\n", end="")
                    in_thinking = True
                print(chunk.message.thinking, end="", flush=True)
            elif chunk.message.content:
                if in_thinking:
                    print("\n\nAnswer:\n", end="")
                    in_thinking = False
                print(chunk.message.content, end="", flush=True)
                content += chunk.message.content
        else:
            #print(repr(chunk)) # Debug only
            print(chunk.message.content, end="", flush=True)
            content += chunk.message.content

    print()
    return content


def call_node(
    node: Node,
    schema=None,
    think: bool = False,
    stream: bool = False,
    temperature: float = config.TEMPERATURE,
    token_limit: int = None,
    model: str = config.MODEL,
):
    """Queries the model with optional JSON schema enforcement and streaming."""

    thinking_enabled = think and supports_thinking(model)
    token_limit = token_limit or (config.THINKING_TOKEN_LIMIT if thinking_enabled else config.TOKEN_LIMIT)

    response = ollama.chat(
        model=model,
        messages=node.messages(),
        format=schema.model_json_schema() if schema else None,
        think=thinking_enabled if supports_thinking(model) else None,
        stream=stream,
        options={"temperature": temperature, "num_predict": token_limit},
    )

    if stream:
        content = _stream_response(response, thinking_enabled)
    else:
        content = response.message.content

    #content = strip_code_fences(content) # TODO: Might want to remove this and the method and put it back in the code gen module

    if not schema:
        return content

    try:
        return schema.model_validate_json(content)
    except ValidationError as e:
        if "EOF" not in str(e) and "json_invalid" not in str(e):
            raise

    print("[WARN] JSON truncated, attempting salvage...")
    salvaged = salvage_truncated_json(content)

    try:
        return schema.model_validate_json(salvaged)
    except ValidationError:
        print("[WARN] Failed to salvage JSON, returning truncated output")
        return(content)