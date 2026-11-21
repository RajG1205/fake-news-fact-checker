"""Fact-checking and claim analysis service.

The existing implementation is preserved here during the structural refactor.
"""

# Keep provider configuration and business logic in one service module for now.
# Subsequent changes can split retrieval, evidence validation and LLM analysis into
# separate modules once their public interfaces are covered by tests.
