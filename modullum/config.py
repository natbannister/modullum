# MODEL OPTIONS
MODEL = "qwen2.5-coder" # Model used for all nodes
MODEL_VARIANT = " (7B)" # Only used for specificity in records
STREAM_USER_FACING = True # User-facing nodes stream output, Ollama says this can slow down processing
STREAM_CODE = False
TOKEN_LIMIT = 1500 # Cuts off node after limit reached (causes bad cutoff on JSON outputs if hit)
TEMPERATURE = 0 # Determinism option - lower is more deterministic

# CODEGEN MODULE
INPUT_REVIEW = False # Module outputs assessment of input requirements
MAX_TEST_ITERATIONS = 2 # Maxmium attempts to generate pytests
MAX_CODE_ITERATIONS = 3 # Maximum attempts to generate code

# REQUIREMENTS MODULE
USER_PROMPT = False # Normally True, set False if a pre-written prompt is desired for faster R&D
AUTO_SKIP = True # Skips any user input stages
INTERVIEW = False # Set to True to prompt user feedback to aid task scoping
INTERVIEW_QUESTION_COUNT = 3 # Prompted questions limit to aid requirements generation
REQUIREMENTS_CAP = 10 # Prompted requirements limit to prevent requirements degeneration
GROQ_REVIEW = False # [Not implemented] Set to True to enable cloud-based Groq review (larger model, better automated review)
