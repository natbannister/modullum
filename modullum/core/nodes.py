import ollama
import re
import time
from typing import NamedTuple, Any
from pydantic import ValidationError
from modullum import config


class NodeResult(NamedTuple):
    """
    Return value of call_node(). Carries the parsed output alongside
    the raw metrics needed by NodeRecord.finish().

    Fields:
        output        — parsed Pydantic model, or raw string if no schema
        tokens_in     — prompt token count (prompt_eval_count from Ollama)
        tokens_out    — completion token count (eval_count from Ollama)
        llm_duration_s — wall time of the blocking ollama.chat() call only,
                         excluding any user-facing streaming print overhead
    """
    output: Any
    tokens_in: int
    tokens_out: int
    llm_duration_s: float


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
    """Strips markdown fences for python and json outputs."""
    # NOTE: '|json' can be removed, this function is not sufficient for JSON enforcement.
    match = re.search(r'```(?:python|json)?\n(.*?)```', text, re.DOTALL)
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
            ref_key = node["$ref"].split("/")[-1]
            resolved = resolve(defs[ref_key]) if ref_key in defs else node
            extras = {k: v for k, v in node.items() if k != "$ref"}
            return {**resolved, **extras} if extras else resolved

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

def schema_to_prompt_hint(schema: dict, indent: int = 0) -> str:
    """
    Recursively renders a flattened JSON schema into a prompt-ready description.
    Handles nested objects, nullable fields, enums, and arrays of objects.
    """
    if hasattr(schema, "model_json_schema"):
        schema = schema.model_json_schema()

    flat = flatten_schema(schema)
    props = flat.get("properties", {})
    pad = "    " * indent
    lines = []

    if indent == 0:
        lines.append("You MUST under ALL circumstances output a JSON object, with NO markdown fences, with these EXACT fields:")

    for name, field in props.items():
        field_type, nullable = _resolve_field_type(field)
        null_suffix = " or null" if nullable else ""
        desc = field.get("description", "")
        desc_suffix = f" — {desc}" if desc else ""

        if field_type == "object":
            lines.append(f"{pad}- {name}: object{null_suffix}{desc_suffix}")
            lines.append(_render_object(field, indent + 1))

        elif field_type == "array":
            items = field.get("items", {})
            item_type, _ = _resolve_field_type(items)
            if item_type == "object":
                lines.append(f"{pad}- {name}: array of objects{null_suffix}{desc_suffix}, each with fields:")
                lines.append(_render_object(items, indent + 1))
            else:
                lines.append(f"{pad}- {name}: array of {item_type or 'any'}{null_suffix}{desc_suffix}")

        elif field.get("enum"):
            enum_vals = ", ".join(f'"{v}"' for v in field["enum"])
            lines.append(f"{pad}- {name}: one of [{enum_vals}]{null_suffix}{desc_suffix}")

        else:
            lines.append(f"{pad}- {name}: {field_type or 'any'}{null_suffix}{desc_suffix}")

    if indent == 0:
        lines.append("Output must contain exactly these fields and no others.")

    return "\n".join(lines)

def _resolve_field_type(field: dict) -> tuple[str, bool]:
    """
    Returns (type_string, is_nullable) for a field, handling anyOf/oneOf patterns
    produced by Optional[X] and Union[X, None].
    """
    if "anyOf" in field or "oneOf" in field:
        variants = field.get("anyOf") or field.get("oneOf")
        non_null = [v for v in variants if v.get("type") != "null"]
        nullable = len(non_null) < len(variants)
        primary = non_null[0] if non_null else {}
        # Merge back so callers can inspect items/properties/enum on the resolved type
        merged = {**primary, **{k: v for k, v in field.items() if k not in ("anyOf", "oneOf")}}
        field_type, _ = _resolve_field_type(merged)
        return field_type, nullable

    return field.get("type", ""), False

def _render_object(field: dict, indent: int) -> str:
    """Renders the properties of an object field recursively."""
    return schema_to_prompt_hint(field, indent=indent)

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

def _stream_response(response, thinking_enabled: bool) -> tuple[str, int, int]:
    """
    Handles streaming output, with optional thinking block display.

    Returns:
        (content, tokens_in, tokens_out)
        Token counts are taken from the final chunk's done_reason response,
        which Ollama populates once generation is complete.
    """
    content = ""
    in_thinking = False
    tokens_in = 0
    tokens_out = 0
    last_chunk = None

    for chunk in response:
        last_chunk = chunk
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

    # Final chunk carries the usage stats once done
    if last_chunk is not None:
        tokens_in = getattr(last_chunk, "prompt_eval_count", 0) or 0
        tokens_out = getattr(last_chunk, "eval_count", 0) or 0

    return content, tokens_in, tokens_out


def call_node(
    node: Node,
    schema=None,
    think: bool = False,
    stream: bool = False,
    temperature: float = config.TEMPERATURE,
    token_limit: int = None,
    model: str = config.MODEL,
) -> NodeResult:
    """
    Queries the model with optional JSON schema enforcement and streaming.

    Returns a NodeResult(output, tokens_in, tokens_out, llm_duration_s).
    output is a parsed Pydantic model when schema is provided, otherwise a string.
    llm_duration_s covers only the ollama.chat() call; user-input wait time
    is measured externally by ModuleContext / NodeRecord.
    """

    thinking_enabled = think and supports_thinking(model)
    token_limit = token_limit or (config.THINKING_TOKEN_LIMIT if thinking_enabled else config.TOKEN_LIMIT)

    t0 = time.monotonic()
    response = ollama.chat(
        model=model,
        messages=node.messages(),
        format=schema.model_json_schema() if schema else None,
        think=thinking_enabled if supports_thinking(model) else None,
        stream=stream,
        options={"temperature": temperature, "num_predict": token_limit},
    )

    if stream:
        content, tokens_in, tokens_out = _stream_response(response, thinking_enabled)
    else:
        content = response.message.content
        tokens_in = getattr(response, "prompt_eval_count", 0) or 0
        tokens_out = getattr(response, "eval_count", 0) or 0

    llm_duration_s = round(time.monotonic() - t0, 3)

    content = strip_code_fences(content) # TODO: Might want to remove this and the method and put it back in the code gen module

    if not schema:
        return NodeResult(
            output=content,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            llm_duration_s=llm_duration_s,
        )

    try:
        parsed = schema.model_validate_json(content)
        return NodeResult(
            output=parsed,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            llm_duration_s=llm_duration_s,
        )
    except ValidationError as e:
        if "EOF" not in str(e) and "json_invalid" not in str(e):
            raise

    print("[WARN] JSON truncated, attempting salvage...")
    salvaged = salvage_truncated_json(content)

    try:
        parsed = schema.model_validate_json(salvaged)
        return NodeResult(
            output=parsed,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            llm_duration_s=llm_duration_s,
        )
    except ValidationError:
        print("[WARN] Failed to salvage JSON, returning truncated output")
        return NodeResult(
            output=content,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            llm_duration_s=llm_duration_s,
        )