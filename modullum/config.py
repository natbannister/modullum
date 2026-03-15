# MODELS (A coding model doesn't inherently need to be chosen for coding)
# Add as desired. If the model has thinking, add it to the THINKING_MODELS list for correct usage
THINKING_MODELS = {"qwen3.5:9b", "qwen3.5:0.8b"}
CODING_MODELS = {"qwen2.5-coder"} # Currently only denotes that model is non-thinking

# MODEL OPTIONS
MODEL = "qwen3.5:9b" # Model used for all nodes.
#MODEL_VARIANT = " (7B)" # Only used for specificity in records
STREAM_USER_FACING = True # User-facing nodes stream output
STREAM_CODE = True
STREAM_JSON = True
TOKEN_LIMIT = 2048 # Cuts off node after limit reached (causes bad cutoff on JSON outputs if hit)
THINKING_TOKEN_LIMIT = None  #8192 # Default token limit for thinking
TEMPERATURE = 0 # Determinism option - lower is more deterministic

# CODEGEN MODULE
INPUT_REVIEW = False # Module outputs assessment of input requirements
TESTS_FEEDBACK = False # Enable or disble analysis of tests before code generation
MAX_TEST_ITERATIONS = 2 # Maxmium attempts to generate pytests
MAX_CODE_ITERATIONS = 3 # Maximum attempts to generate code

# REQUIREMENTS MODULE
USER_PROMPT = False # Normally True, set False if a pre-written prompt is desired for faster R&D
AUTO_SKIP = True # Skips any user input stages
INTERVIEW = False # Set to True to prompt user feedback to aid task scoping
INTERVIEW_QUESTION_COUNT = 3 # Prompted questions limit to aid requirements generation
ASSUMPTIONS_USER_REVIEW = False
REQUIREMENTS_CAP = 15 # Prompted requirements limit to prevent requirements degeneration
REQUIREMENTS_GEN_THINK = False
STREAM_REQUIREMENTS_GEN = True