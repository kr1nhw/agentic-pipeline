#!/usr/bin/env python3
"""docpipe — agentic pipeline for extracting content from documents.

The public entry point is :func:`docpipe.cli.main`. Everything the agent can
choose between lives in the operator registry (``docpipe.api.registry``); the
static pipeline catalog and the dynamic planner both lower to the same
:class:`docpipe.schema.Plan`.
"""

__all__ = ["__version__", "API_VERSION"]

# Runtime version of the docpipe distribution.
__version__ = "0.1.0"

# Version of the *plugin* API. A plugin manifest declares the major it targets
# and the loader refuses a mismatch; see docs/plugin-spec.md.
API_VERSION = "1"
