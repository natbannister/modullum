# MODELS (A coding model doesn't inherently need to be chosen for coding)
# Add as desired. If the model has thinking, add it to the THINKING_MODELS list for correct usage
ALL_MODELS = {"qwen3.5:9b", "qwen3.5:0.8b", "granite4:tiny-h", "qwen2.5-coder", "nemotron-3-nano:4b", "granite3.1-moe:3b"}
THINKING_MODELS = {"qwen3.5:9b", "qwen3.5:0.8b", "nemotron-3-nano:4b"}
CODING_MODELS = {"granite3.1-moe:3b"} # Currently only denotes that model is non-thinking

# MODEL OPTIONS
MODEL = "granite4:tiny-h" # Model used for all nodes.
#MODEL_VARIANT = " (7B)" # Only used for specificity in records
STREAM_USER_FACING = True # User-facing nodes stream output
STREAM_CODE = True
STREAM_JSON = True
TOKEN_LIMIT = 2048 # Cuts off node after limit reached (causes bad cutoff on JSON outputs if hit)
BIG_TOKEN_LIMIT = 8192 # Used on more intensive nodes
THINKING_TOKEN_LIMIT = None  #8192 # Default token limit for thinking
TEMPERATURE = 0.6 # Determinism option - lower is more deterministic

# CODEGEN MODULE
SKIP_REQUIREMENTS = False # Straight to codegen on default requiremnets set (developer)
INPUT_REVIEW = False # Module outputs assessment of input requirements
TESTS_FEEDBACK = False # Enable or disble analysis of tests before code generation
MAX_TEST_ITERATIONS = 2 # Maxmium attempts to generate pytests
MAX_CODE_ITERATIONS = 3 # Maximum attempts to generate code
OUTPUT_PYTEST_RESULTS = False # Print Pytest results to terminal

# REQUIREMENTS MODULE
USER_PROMPT = False # Normally True, set False if a pre-written prompt is desired for faster R&D
AUTO_SKIP = False # Skips any user input stages
INTERVIEW = False # Set to True to prompt user feedback to aid task scoping
INTERVIEW_QUESTION_COUNT = 3 # Prompted questions limit to aid requirements generation
ASSUMPTIONS_USER_REVIEW = False
REQUIREMENTS_CAP = 15 # Prompted requirements limit to prevent requirements degeneration
REQUIREMENTS_GEN_THINK = False
STREAM_REQUIREMENTS_GEN = True

# RESEARCH MODULE (NOTE: NOT YET IMPLEMENTED!)
RESEARCH_ENABLED = False # Set to True to enable the research module (analytical hemisphere)
RESEARCH_PROVIDER = "tavily" # Search provider to use (e.g., tavily, duckduckgo, etc.)
RESEARCH_MAX_RESULTS = 5 # Maximum number of search results to fetch per query
RESEARCH_API_KEY = "" # API key for the search provider (if required)
RESEARCH_RELEVANCE_GATE_MODEL = "claude-haiku-4-5-20251001" # Model for relevance gate (binary classification)
RESEARCH_STRUCTURED_EXTRACTION_MODEL = "claude-sonnet-4-6" # Model for structured field extraction
RESEARCH_USER_APPROVAL = True # Require user approval before proceeding to requirements generation