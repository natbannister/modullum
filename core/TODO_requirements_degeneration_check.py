def check_for_degeneration(content: str, threshold: int = 3) -> bool:
    """
    Detects repetitive requirement generation before JSON parse.
    Returns True if degeneration is suspected.
    """
    # Extract all description strings
    descriptions = re.findall(r'"description"\s*:\s*"([^"]*)"', content)
    if len(descriptions) < 4:
        return False
    
    # Check for near-duplicate endings (the "compartments... and compartments" pattern)
    endings = [d[-40:] for d in descriptions]
    unique_endings = set(endings)
    if len(unique_endings) / len(endings) < 0.6:
        print(f"[WARN] Degeneration detected: {len(unique_endings)} unique endings from {len(endings)} requirements")
        return True
    
    return False