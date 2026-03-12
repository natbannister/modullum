import ollama
import re
from pydantic import ValidationError


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


def call_node(
    node: Node,
    list_schema=None,
    stream: bool = False,
    temperature: float = 0,
    token_limit: int = 1024,
    model: str = "qwen2.5-coder",
):
    """Queries the model with optional JSON schema enforcement and streaming."""
    response = ollama.chat(
        model=model,
        messages=node.messages(),          # ← was node.messages (missing call)
        format=list_schema.model_json_schema() if list_schema else None,
        stream=stream,
        options={"temperature": temperature, "num_predict": token_limit},
    )

    if stream:
        content = ""
        for chunk in response:
            piece = chunk["message"]["content"]
            print(piece, end="", flush=True)
            content += piece
        print()
    else:
        content = response.message.content

    if not list_schema:
        return content

    try:
        return list_schema.model_validate_json(content)
    ### Handling malformed JSON outputs if token limit hit (truncated response)
    except ValidationError as e:
        if "EOF" not in str(e) and "json_invalid" not in str(e):
            raise

    print("[WARN] JSON truncated, attempting salvage...")
    salvaged = salvage_truncated_json(content)

    try:
        return list_schema.model_validate_json(salvaged)
    except ValidationError:
        raise ValueError(
            f"Could not recover valid JSON after salvage. "
            f"Content length: {len(content)} chars. "
            f"Consider raising token_limit (currently {token_limit})."
        )